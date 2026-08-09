"""Review command orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .artifacts import temporary_file_writer
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
    github = GitHubClient(command_runner, repo_dir)
    github.initialize(pr_value)
    initial = github.snapshot()
    github.ensure_objects(initial)
    with temporary_file_writer(
        command_runner,
        prefix=f"pr-review-loop-review-{initial.number}-{initial.head_sha[:12]}-",
    ) as writer:
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
    event = github.review_event(initial, verdict.verdict)
    review_id, _ = github.post_review(initial, event, body)

    try:
        after_post = github.snapshot()
        verified = github.verify_posted(initial, review_id, body)
        _require_fresh_state(github, initial, after_post, verified, event)
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


_EXPECTED_REVIEW_STATE = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


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

    The Oracle verdict stays canonical in the command result, but GitHub's
    persisted review state must still correspond to the selected transport
    event so a formal or commit-anchored publication cannot silently record
    a state other than the one actually written.
    """
    expected_state = _EXPECTED_REVIEW_STATE[event]
    state = verified.get("state") if isinstance(verified, dict) else None
    if state != expected_state or not github.same_snapshot(initial, after_post):
        raise LooprError(
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
        LooprError: Dismissal itself failed; raised from the original error.
    """
    if event == "COMMENT":
        return
    try:
        github.dismiss(pull_request, review_id)
    except LooprError as dismiss_error:
        original_message = str(original) or type(original).__name__
        message = (
            f"{original_message}; stale review {review_id} could not be dismissed: "
            f"{dismiss_error}"
        )
        raise LooprError(EXIT_RACE, "stale_state", message) from original
