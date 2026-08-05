"""Review command orchestration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactWriter
from .github import GitHubClient
from .models import EXIT_RACE, LooprError, ReviewResult
from .oracle import OracleClient
from .process import CommandRunner

if TYPE_CHECKING:
    from .models import PullRequest


def execute_review(
    *,
    pr_value: str,
    repo_dir: Path,
    artifacts_dir: Path,
    thinking_time: str,
    runner: CommandRunner | None = None,
) -> ReviewResult:
    """Review and post one exact pull-request snapshot."""
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
        _run_directory(repo_dir, artifacts_dir, initial),
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
    event = "APPROVE" if verdict.verdict == "APPROVE" else "REQUEST_CHANGES"
    review_id, posted = github.post_review(initial, event, body)
    writer.json("github-review.json", posted)

    try:
        after_post = github.snapshot()
        verified = github.verify_posted(initial, review_id)
        expected_state = "APPROVED" if event == "APPROVE" else "CHANGES_REQUESTED"
        if verified.get("state") != expected_state or not github.same_snapshot(
            initial, after_post
        ):
            raise LooprError(
                EXIT_RACE,
                "stale_state",
                "posted review became stale or had an unexpected state",
            )
    except LooprError as exc:
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
    writer.json("result.json", result.as_json())
    return result


def _run_directory(
    repo_dir: Path,
    artifacts_dir: Path,
    pull_request: PullRequest,
) -> Path:
    """Return a unique deterministic-prefix run directory."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (
        artifacts_dir
        if artifacts_dir.is_absolute()
        else repo_dir.resolve() / artifacts_dir
    )
    name = f"review-pr-{pull_request.number}-{pull_request.head_sha[:12]}-{stamp}"
    return root / "runs" / name


def _dismiss_stale(
    github: GitHubClient,
    pull_request: PullRequest,
    review_id: int,
    original: LooprError,
) -> None:
    """Neutralize a stale review and preserve failure context if dismissal fails."""
    try:
        github.dismiss(pull_request, review_id)
    except LooprError as dismiss_error:
        message = (
            f"{original}; stale review {review_id} could not be dismissed: "
            f"{dismiss_error}"
        )
        raise LooprError(EXIT_RACE, "stale_state", message) from original
