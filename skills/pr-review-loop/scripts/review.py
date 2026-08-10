"""Review command orchestration."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .artifacts import temporary_file_writer
from .github import GitHubClient
from .models import (
    EXIT_ORACLE,
    EXIT_RACE,
    JsonValue,
    ReviewComment,
    ReviewLoopError,
    ReviewResult,
)
from .oracle import (
    MAX_ORACLE_ATTACHMENTS,
    PROMPT,
    build_review_bundle,
    invoke_oracle,
    parse_review,
)
from .process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from .artifacts import TemporaryFileWriter
    from .models import (
        BlockingFinding,
        FindingLocation,
        OracleReview,
        PullRequest,
        PullRequestIdentity,
    )


def review_prompt(pull_request: PullRequest) -> str:
    """Return the trusted review prompt with direct GitHub app invocation."""
    return "@GitHub\n" + PROMPT.format(
        repository=pull_request.repository,
        pr_number=pull_request.number,
        base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
    )


def _generate_review(
    command_runner: CommandRunner,
    github: GitHubClient,
    writer: TemporaryFileWriter,
    pull_request: PullRequest,
    thinking_time: str | None,
    model: str | None,
) -> OracleReview:
    """Build evidence, invoke Oracle once, and parse its review.

    Returns:
        The strictly validated Oracle review.
    """
    attachments = build_review_bundle(command_runner, github, writer, pull_request)
    prompt = review_prompt(pull_request)
    slug = (
        f"pr-review-loop-review-{pull_request.number}-"
        f"{pull_request.head_sha[:12]}-{uuid.uuid4().hex[:8]}"
    )
    raw = invoke_oracle(
        command_runner,
        writer,
        github.repo_dir,
        thinking_time,
        prompt,
        attachments,
        slug,
        model=model,
        max_attachments=MAX_ORACLE_ATTACHMENTS,
    )
    return parse_review(raw, pull_request)


def _same_review_identity(
    initial: PullRequest,
    current: PullRequestIdentity,
) -> bool:
    """Return whether the reviewed base/head identity is still current."""
    return initial.base_sha == current.base_sha and initial.head_sha == current.head_sha


def execute_review(
    *,
    pr_value: str,
    repo_dir: Path,
    thinking_time: str | None = None,
    model: str | None = None,
    runner: CommandRunner | None = None,
) -> ReviewResult:
    """Review and post one exact pull-request snapshot.

    Returns:
        The stable review command result.

    Raises:
        ReviewLoopError: The pull request, bundle, or Oracle verdict violated a
            precondition, or the posted review could not be verified fresh.
    """
    command_runner = runner if runner is not None else CommandRunner()
    github = GitHubClient(command_runner, repo_dir)
    github.initialize(pr_value)
    initial = github.snapshot()
    github.ensure_objects(initial)
    with temporary_file_writer(
        command_runner,
        prefix=f"pr-review-loop-review-{initial.number}-{initial.head_sha[:12]}-",
    ) as writer:
        verdict = _generate_review(
            command_runner,
            github,
            writer,
            initial,
            thinking_time,
            model,
        )

    body, comments = _publication(github, initial, verdict)
    before_post = github.identity_snapshot()
    if not _same_review_identity(initial, before_post):
        raise ReviewLoopError(
            EXIT_RACE,
            "stale_state",
            "pull request base or head changed before review posting",
        )
    event = github.review_event(initial, verdict.verdict)
    review_id, _ = github.post_review(initial, event, body, comments)

    try:
        after_post = github.identity_snapshot()
        verified = github.verify_posted(initial, review_id, body)
        _require_fresh_state(initial, after_post, verified, event)
    except BaseException as exc:
        _dismiss_stale(github, initial, review_id, event, exc)
        raise

    result = ReviewResult(
        repository=initial.repository,
        pr_number=initial.number,
        base_sha=initial.base_sha,
        head_sha=initial.head_sha,
        verdict=verdict.verdict,
        github_review_id=review_id,
        blocking_findings=verdict.blocking_findings,
        implementation_prompt=verdict.implementation_prompt,
    )
    return result


def _publication(
    github: GitHubClient,
    pull_request: PullRequest,
    verdict: OracleReview,
) -> tuple[str, tuple[ReviewComment, ...]]:
    """Compose the one bounded review publication for a frozen snapshot.

    Returns:
        The aggregate body and the inline comments to publish with it.

    Raises:
        ReviewLoopError: The composed body or the inline comments exceed
            GitHub's body limit.
    """
    anchors: frozenset[tuple[str, str, int]] = (
        github.diff_anchors(pull_request)
        if any(finding.location is not None for finding in verdict.blocking_findings)
        else frozenset()
    )
    comments, unanchored = _partition_findings(verdict, anchors)
    body = _aggregate_body(pull_request, verdict, unanchored)
    if len(body.encode("utf-8")) > MAX_POSTED_BODY_BYTES:
        raise ReviewLoopError(
            EXIT_ORACLE,
            "oracle_schema",
            "review body with audit footer exceeds GitHub's body limit",
        )
    for comment in comments:
        if len(comment.body.encode("utf-8")) > MAX_POSTED_BODY_BYTES:
            raise ReviewLoopError(
                EXIT_ORACLE,
                "oracle_schema",
                "an inline review comment exceeds GitHub's body limit",
            )
    return body, comments


def _finding_text(finding: BlockingFinding) -> str:
    """Render one blocking finding as the Markdown published for it.

    Returns:
        The finding's Markdown block, used inline or in the aggregate body.
    """
    return (
        f"**{finding.id}: {finding.title}**\n\n"
        f"{finding.description}\n\n"
        f"Required change: {finding.required_change}"
    )


def _validated_anchor(
    location: FindingLocation | None,
    anchors: frozenset[tuple[str, str, int]],
) -> tuple[str, str, int] | None:
    """Match one Oracle-proposed location against the frozen diff's anchors.

    An anchor is accepted only when the reviewed base-to-head diff itself
    contains that exact path, side, and line. An absent, malformed, stale, or
    non-diff location is never relocated to a nearby line; it simply yields no
    anchor, and its finding stays in the aggregate body.

    Returns:
        The validated anchor, or None when the location names no diff line.
    """
    if location is None:
        return None
    anchor = (location.path, location.side, location.line)
    return anchor if anchor in anchors else None


def _partition_findings(
    verdict: OracleReview,
    anchors: frozenset[tuple[str, str, int]],
) -> tuple[tuple[ReviewComment, ...], tuple[BlockingFinding, ...]]:
    """Split blocking findings into inline comments and aggregate-only findings.

    Every finding lands in exactly one of the two collections, so a finding is
    never published both inline and in the aggregate body.

    Returns:
        The inline review comments and the findings left for the body.
    """
    comments: list[ReviewComment] = []
    unanchored: list[BlockingFinding] = []
    for finding in verdict.blocking_findings:
        anchor = _validated_anchor(finding.location, anchors)
        if anchor is None:
            unanchored.append(finding)
        else:
            path, side, line = anchor
            comments.append(
                ReviewComment(
                    path=path,
                    line=line,
                    side=side,
                    body=_finding_text(finding),
                )
            )
    return tuple(comments), tuple(unanchored)


def _aggregate_body(
    pull_request: PullRequest,
    verdict: OracleReview,
    unanchored: tuple[BlockingFinding, ...],
) -> str:
    """Compose the review body, notes, unanchored findings, and audit footer.

    Returns:
        The exact body text posted with the review.
    """
    sections = [verdict.review_body]
    if verdict.non_blocking_notes:
        notes = "\n".join(f"- {note}" for note in verdict.non_blocking_notes)
        sections.append(f"## Non-blocking notes\n\n{notes}")
    if unanchored:
        findings = "\n\n".join(_finding_text(finding) for finding in unanchored)
        sections.append(f"## Findings without a diff anchor\n\n{findings}")
    return (
        "\n\n".join(sections) + "\n\n---\n"
        f"Reviewed base: `{pull_request.base_sha}`\n"
        f"Reviewed head: `{pull_request.head_sha}`\n"
    )


_EXPECTED_REVIEW_STATE = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


def _require_fresh_state(
    initial: PullRequest,
    after_post: PullRequestIdentity,
    verified: JsonValue,
    event: str,
) -> None:
    """Confirm the posted review's state and snapshot are still fresh.

    Raises:
        ReviewLoopError: The verified state or repository snapshot went stale.

    The Oracle verdict stays canonical in the command result, but GitHub's
    persisted review state must still correspond to the selected transport
    event so a formal or commit-anchored publication cannot silently record
    a state other than the one actually written.
    """
    expected_state = _EXPECTED_REVIEW_STATE[event]
    state = verified.get("state") if isinstance(verified, dict) else None
    if state != expected_state or not _same_review_identity(initial, after_post):
        raise ReviewLoopError(
            EXIT_RACE,
            "stale_state",
            "posted review verification or PR snapshot became stale",
        )


MAX_POSTED_BODY_BYTES = 65_000


def _dismiss_stale(
    github: GitHubClient,
    pull_request: PullRequest,
    review_id: int,
    event: str,
    original: BaseException,
) -> None:
    """Neutralize a stale review and preserve failure context if dismissal fails.

    Raises:
        ReviewLoopError: Dismissal itself failed; raised from the original
            error.
    """
    if event == "COMMENT":
        return
    try:
        github.dismiss(pull_request, review_id)
    except ReviewLoopError as dismiss_error:
        original_message = str(original) or type(original).__name__
        message = (
            f"{original_message}; stale review {review_id} could not be dismissed: "
            f"{dismiss_error}"
        )
        raise ReviewLoopError(EXIT_RACE, "stale_state", message) from original
