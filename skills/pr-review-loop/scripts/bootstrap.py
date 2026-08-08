"""Issue bootstrap command orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .artifacts import ArtifactWriter, claim_run_directory
from .github import IssueClient
from .models import EXIT_RACE, BootstrapResult, LooprError
from .oracle import BootstrapOracleClient
from .process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path


def execute_bootstrap(
    *,
    issue_value: str,
    repo_dir: Path,
    artifacts_dir: Path,
    thinking_time: str,
    runner: CommandRunner | None = None,
) -> BootstrapResult:
    """Turn one open GitHub Issue into a bounded implementation prompt.

    Returns:
        The stable bootstrap command result.

    Raises:
        LooprError: The Issue, base branch, or Oracle output violated a
            precondition, or the Issue or base branch changed during prompt
            generation.
    """
    command_runner = runner or CommandRunner()
    issue_client = IssueClient(command_runner, repo_dir)
    issue_client.initialize(issue_value)
    initial = issue_client.snapshot()
    base_ref, base_sha = _base_snapshot(issue_client)
    _ensure_base_available(issue_client, base_ref, base_sha)

    writer = ArtifactWriter(
        claim_run_directory(
            issue_client.repo_dir,
            artifacts_dir,
            f"bootstrap-issue-{initial.number}",
        ),
        command_runner,
    )
    writer.json("initial-issue.json", initial.raw)
    oracle = BootstrapOracleClient(command_runner, issue_client, writer, thinking_time)
    bundle = oracle.build_bundle(initial, base_sha)
    generated = oracle.generate(initial, base_ref, base_sha, bundle)

    after = issue_client.snapshot()
    after_ref, after_sha = _base_snapshot(issue_client)
    if (
        after.updated_at != initial.updated_at
        or after_ref != base_ref
        or after_sha != base_sha
    ):
        raise LooprError(
            EXIT_RACE,
            "stale_state",
            "issue or base branch changed during prompt generation",
        )

    result = BootstrapResult(
        repository=initial.repository,
        issue_number=initial.number,
        issue_url=initial.url,
        issue_updated_at=initial.updated_at,
        base_ref=base_ref,
        base_sha=base_sha,
        implementation_prompt=generated.implementation_prompt,
        artifacts_dir=str(writer.root),
    )
    writer.json("result.json", result.as_json())
    return result


def _base_snapshot(issue_client: IssueClient) -> tuple[str, str]:
    """Return the repository's current default branch name and exact SHA.

    Returns:
        The default branch name and its exact current commit SHA.
    """
    base_ref = issue_client.default_branch()
    base_sha = issue_client.branch_sha(base_ref)
    return base_ref, base_sha


def _ensure_base_available(
    issue_client: IssueClient,
    base_ref: str,
    base_sha: str,
) -> None:
    """Require the base commit to be present locally, with an actionable failure.

    Raises:
        LooprError: base_sha does not name a local commit object.
    """
    try:
        issue_client.ensure_commit_object(base_sha)
    except LooprError as exc:
        message = (
            f"base commit {base_sha} for branch {base_ref} is not available "
            f"locally; run `git fetch origin {base_ref}` and retry"
        )
        raise LooprError(exc.code, exc.category, message) from exc
