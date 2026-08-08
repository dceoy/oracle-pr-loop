"""Issue bootstrap command orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .artifacts import ArtifactWriter, claim_run_directory
from .github import IssueClient
from .models import EXIT_PRECONDITION, EXIT_RACE, BootstrapResult, LooprError
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
        LooprError: The Issue, base branch, local workspace, or Oracle output
            violated a precondition, or the issue, base branch, or local
            workspace changed during prompt generation.
    """
    command_runner = runner or CommandRunner()
    issue_client = IssueClient(command_runner, repo_dir)
    issue_client.initialize(issue_value)
    initial = issue_client.snapshot()
    base_ref, base_sha = _base_snapshot(issue_client)
    _ensure_base_available(issue_client, base_ref, base_sha)
    _ensure_workspace_bound_to_base(issue_client, base_ref, base_sha, artifacts_dir)

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
    after_head = _local_head(issue_client)
    if (
        after.updated_at != initial.updated_at
        or after.title != initial.title
        or after.body != initial.body
        or after.comments != initial.comments
        or after_ref != base_ref
        or after_sha != base_sha
        or after_head != base_sha
        or _worktree_is_dirty(issue_client, artifacts_dir)
    ):
        raise LooprError(
            EXIT_RACE,
            "stale_state",
            "issue, base branch, or local workspace changed during prompt generation",
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


def _local_head(issue_client: IssueClient) -> str:
    """Return the local checkout's exact current commit SHA.

    Returns:
        The 40-character commit SHA that local `HEAD` currently names.
    """
    return (
        issue_client
        .git_bytes(["rev-parse", "HEAD"], max_output=1024)
        .decode("utf-8", "strict")
        .strip()
    )


def _ensure_workspace_bound_to_base(
    issue_client: IssueClient,
    base_ref: str,
    base_sha: str,
    artifacts_dir: Path,
) -> None:
    """Require the local checkout to already be clean and at base_sha.

    The returned `base_sha` is meant to be the actual implementation base,
    not advisory metadata; a checkout left on an unrelated or stale commit,
    or one carrying pre-existing untracked files, would let the host build
    the first commit and pull request on the wrong or contaminated history.

    Raises:
        LooprError: local `HEAD` is not base_sha, or tracked or untracked
            changes are pending.
    """
    head_sha = _local_head(issue_client)
    if head_sha != base_sha:
        message = (
            f"local HEAD ({head_sha}) is not base commit {base_sha} for "
            f"branch {base_ref}; run `git fetch origin {base_ref}` then "
            f"`git switch -c <branch> {base_sha}` before running bootstrap"
        )
        raise LooprError(EXIT_PRECONDITION, "workspace", message)
    if _worktree_is_dirty(issue_client, artifacts_dir):
        raise LooprError(
            EXIT_PRECONDITION,
            "workspace",
            "local checkout has uncommitted tracked changes or untracked "
            "files; commit, stash, or discard them before running bootstrap",
        )


def _artifacts_exclude_pathspec(repo_dir: Path, artifacts_dir: Path) -> str | None:
    """Return a `git status` pathspec excluding this command's own artifacts.

    `execute_bootstrap` claims a run directory under `artifacts_dir` and
    writes its own bounded prompt-generation artifacts there before the
    post-Oracle workspace recheck runs; that self-created, tool-owned
    output must not itself be flagged as workspace contamination, and this
    must hold regardless of whether a given host repository also lists
    `artifacts_dir` in `.gitignore`.

    Returns:
        A `:(exclude)<path>` pathspec for `artifacts_dir`, or None when it
        does not resolve inside repo_dir and so cannot appear in `git
        status` output at all.
    """
    root = repo_dir.resolve()
    target = artifacts_dir if artifacts_dir.is_absolute() else root / artifacts_dir
    try:
        relative = target.resolve().relative_to(root)
    except ValueError:
        return None
    if str(relative) == ".":
        return None
    return f":(exclude){relative.as_posix()}"


def _worktree_is_dirty(issue_client: IssueClient, artifacts_dir: Path) -> bool:
    """Report whether the local checkout has any tracked or untracked changes.

    Any change outside `artifacts_dir` counts, whether or not the host
    repository's `.gitignore`/`.git/info/exclude` also covers that
    directory.

    Returns:
        True if the checkout has any pending tracked or untracked change
        outside artifacts_dir.
    """
    args = ["status", "--porcelain"]
    pathspec = _artifacts_exclude_pathspec(issue_client.repo_dir, artifacts_dir)
    if pathspec is not None:
        args = [*args, "--", ".", pathspec]
    status = issue_client.git_bytes(args, max_output=64 * 1024)
    return bool(status.strip())
