"""Review command orchestration."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .artifacts import ArtifactWriter, claim_run_directory
from .github import GitHubClient
from .models import EXIT_ORACLE, EXIT_RACE, JsonValue, LooprError, ReviewResult
from .oracle import OracleClient
from .process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from .models import PullRequest


def execute_review(
    *,
    pr_value: str,
    repo_dir: Path,
    artifacts_dir: Path,
    thinking_time: str,
    runner: CommandRunner | None = None,
) -> ReviewResult:
    """Review and post one exact pull-request snapshot.

    Returns:
        The stable review command result.

    Raises:
        LooprError: The pull request, bundle, or Oracle verdict violated a
            precondition, or the posted review could not be verified fresh.
    """
    command_runner = runner or CommandRunner()
    github = GitHubClient(
        command_runner,
        repo_dir,
        command_runner.source_env.get("GH_REVIEW_TOKEN", ""),
    )
    github.initialize(pr_value)
    initial = github.snapshot()
    github.ensure_objects(initial)
    writer = ArtifactWriter(
        claim_run_directory(
            repo_dir,
            artifacts_dir,
            f"review-pr-{initial.number}-{initial.head_sha[:12]}",
        ),
        command_runner,
    )
    writer.json("initial-snapshot.json", initial.raw)
    oracle = OracleClient(command_runner, github, writer, thinking_time)
    verdict = oracle.review(initial, oracle.build_bundle(initial))

    before_post = github.snapshot()
    if not github.same_snapshot(initial, before_post):
        raise LooprError(
            EXIT_RACE,
            "stale_state",
            "pull request base or head changed before review posting",
        )
    body = (
        f"{verdict.review_body}\n\n---\n"
        f"Reviewed base: `{initial.base_sha}`\n"
        f"Reviewed head: `{initial.head_sha}`\n"
    )
    if len(body.encode("utf-8")) > MAX_POSTED_BODY_BYTES:
        raise LooprError(
            EXIT_ORACLE,
            "oracle_schema",
            "review body with audit footer exceeds GitHub's body limit",
        )
    event = "APPROVE" if verdict.verdict == "APPROVE" else "REQUEST_CHANGES"
    review_id, posted = github.post_review(initial, event, body)

    try:
        _persist_best_effort(writer, "github-review.json", posted)
        after_post = github.snapshot()
        verified = github.verify_posted(initial, review_id)
        _require_fresh_state(github, initial, after_post, verified, event)
    except BaseException as exc:
        _dismiss_stale(github, initial, review_id, exc)
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
        artifacts_dir=str(writer.root),
    )
    _persist_best_effort(
        writer,
        "result.json",
        result.as_json(),
        suppress_interrupts=True,
    )
    return result


def _persist_best_effort(
    writer: ArtifactWriter,
    relative: str,
    value: JsonValue,
    *,
    suppress_interrupts: bool = False,
) -> None:
    """Persist an audit artifact without hiding an already-posted review.

    Before post-write verification, interrupts are re-raised so the surrounding
    compensation block can dismiss the unreported review. After verification,
    the caller may suppress every artifact-write failure so the verified review
    ID is still returned instead of encouraging a duplicate retry.
    """
    try:
        writer.json(relative, value)
    except LooprError as exc:
        message = (
            "pr-review-loop review: warning: failed to persist artifact "
            f"{relative}: {exc}"
        )
        sys.stderr.write(f"{message}\n")
    except BaseException as exc:
        if not suppress_interrupts:
            raise
        detail = str(exc) or type(exc).__name__
        message = (
            "pr-review-loop review: warning: failed to persist artifact "
            f"{relative}: {detail}"
        )
        sys.stderr.write(f"{message}\n")


def _require_fresh_state(
    github: GitHubClient,
    initial: PullRequest,
    after_post: PullRequest,
    verified: JsonValue,
    event: str,
) -> None:
    """Confirm the posted review's state and snapshot are still fresh.

    Raises:
        LooprError: The verified state or repository snapshot went stale.
    """
    expected_state = "APPROVED" if event == "APPROVE" else "CHANGES_REQUESTED"
    state = verified.get("state") if isinstance(verified, dict) else None
    if state != expected_state or not github.same_snapshot(initial, after_post):
        raise LooprError(
            EXIT_RACE,
            "stale_state",
            "posted review became stale or had an unexpected state",
        )


MAX_POSTED_BODY_BYTES = 65_000


def _dismiss_stale(
    github: GitHubClient,
    pull_request: PullRequest,
    review_id: int,
    original: BaseException,
) -> None:
    """Neutralize a stale review and preserve failure context if dismissal fails.

    Raises:
        LooprError: Dismissal itself failed; raised from the original error.
    """
    try:
        github.dismiss(pull_request, review_id)
    except LooprError as dismiss_error:
        original_message = str(original) or type(original).__name__
        message = (
            f"{original_message}; stale review {review_id} could not be dismissed: "
            f"{dismiss_error}"
        )
        raise LooprError(EXIT_RACE, "stale_state", message) from original
