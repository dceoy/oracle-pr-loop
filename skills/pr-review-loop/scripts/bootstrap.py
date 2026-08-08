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
        LooprError: The Issue, base branch, local workspace, artifacts, or
            Oracle output violated a precondition, or the issue, base
            branch, or local workspace changed during prompt generation.
    """
    command_runner = runner or CommandRunner()
    issue_client = IssueClient(command_runner, repo_dir)
    issue_client.initialize(issue_value)
    initial = issue_client.snapshot()
    base_ref, base_sha = _base_snapshot(issue_client)
    _ensure_base_available(issue_client, base_ref, base_sha)
    _ensure_workspace_bound_to_base(issue_client, base_ref, base_sha)

    writer = ArtifactWriter(
        claim_run_directory(
            issue_client.repo_dir,
            artifacts_dir,
            f"bootstrap-issue-{initial.number}",
        ),
        command_runner,
    )
    _require_artifacts_git_ignored(issue_client, writer.root)
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
        or _worktree_is_dirty(issue_client, writer.root)
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


def _local_branch(issue_client: IssueClient) -> str:
    """Return the local checkout's current branch, or "HEAD" if detached.

    Returns:
        The branch name `git rev-parse --abbrev-ref HEAD` reports, or the
        literal string "HEAD" when the checkout has no attached branch.
    """
    return (
        issue_client
        .git_bytes(["rev-parse", "--abbrev-ref", "HEAD"], max_output=1024)
        .decode("utf-8", "strict")
        .strip()
    )


def _ensure_workspace_bound_to_base(
    issue_client: IssueClient,
    base_ref: str,
    base_sha: str,
) -> None:
    """Require the local checkout to already be clean and at base_sha.

    The returned `base_sha` is meant to be the actual implementation base,
    not advisory metadata; a checkout left on an unrelated or stale commit,
    or one carrying pre-existing untracked files, would let the host build
    the first commit and pull request on the wrong or contaminated history.
    No run directory has been claimed yet at this point, so the complete
    worktree is checked; nothing, including any pre-existing content under
    a caller-controlled `--artifacts-dir`, may be excluded. `bootstrap`
    promises not to mutate Git state, so it cannot create the feature
    branch itself; it instead requires one to already exist, since the
    skill tells the host to commit, push, and open a pull request straight
    from this checkout, and a checkout sitting on the default branch (or
    detached) would let that first commit land on `base_ref` itself.

    Raises:
        LooprError: local `HEAD` is not base_sha, the checkout is detached
            or sitting on base_ref itself, or tracked or untracked changes
            are pending.
    """
    head_sha = _local_head(issue_client)
    if head_sha != base_sha:
        message = (
            f"local HEAD ({head_sha}) is not base commit {base_sha} for "
            f"branch {base_ref}; run `git fetch origin {base_ref}` then "
            f"`git switch -c <branch> {base_sha}` before running bootstrap"
        )
        raise LooprError(EXIT_PRECONDITION, "workspace", message)
    local_branch = _local_branch(issue_client)
    if local_branch in {"HEAD", base_ref}:
        state = (
            "in detached HEAD state"
            if local_branch == "HEAD"
            else f"on the default branch {base_ref}"
        )
        message = (
            f"local checkout is {state}; run `git switch -c <branch> "
            f"{base_sha}` before running bootstrap so the first "
            "implementation commit lands on its own branch"
        )
        raise LooprError(EXIT_PRECONDITION, "workspace", message)
    if _worktree_is_dirty(issue_client, None):
        raise LooprError(
            EXIT_PRECONDITION,
            "workspace",
            "local checkout has uncommitted tracked changes or untracked "
            "files; commit, stash, or discard them before running bootstrap",
        )


def _require_artifacts_git_ignored(
    issue_client: IssueClient,
    run_dir: Path,
) -> None:
    """Require Git to exclude the claimed run directory from `git add -A`.

    The host builds the very first implementation commit itself, outside
    `submit`, so none of `submit`'s own artifact-staging protections cover
    it; an unignored run directory would let an ordinary `git add -A`
    sweep this command's private, Issue-derived artifacts (including raw
    Oracle output) into that first public pull request. Nothing needs
    checking when `run_dir` resolves outside `issue_client.repo_dir`, since
    `git add -A` inside the worktree could never reach it there anyway.

    Raises:
        LooprError: `run_dir` is inside `issue_client.repo_dir` but Git
            would not exclude it from `git add -A`.
    """
    try:
        relative = run_dir.resolve().relative_to(issue_client.repo_dir.resolve())
    except ValueError:
        return
    if issue_client.path_is_ignored(relative.as_posix()):
        return
    message = (
        f"artifact directory {relative.as_posix()} is not excluded by Git; "
        "add it to the untracked `.git/info/exclude` (not a tracked "
        ".gitignore, which would itself dirty the workspace) before "
        "running bootstrap"
    )
    raise LooprError(EXIT_PRECONDITION, "artifacts", message)


def _exclude_pathspec_for(repo_dir: Path, path: Path) -> str | None:
    """Return a `git status` pathspec excluding one specific real path.

    Used to hide this command's own claimed run directory from the
    post-Oracle workspace recheck, and this must hold regardless of
    whether a given host repository also lists that path in `.gitignore`.
    Only the exact run directory is ever passed here, never a caller's
    broader `--artifacts-dir`, so unrelated content elsewhere under a
    shared artifacts directory stays visible to the check.

    Returns:
        A `:(exclude,top,literal)<path>` pathspec for path, or None when it
        does not resolve inside repo_dir and so cannot appear in `git
        status` output at all, or equals repo_dir itself.
    """
    root = repo_dir.resolve()
    target = path if path.is_absolute() else root / path
    try:
        relative = target.resolve().relative_to(root)
    except ValueError:
        return None
    if str(relative) == ".":
        return None
    return f":(exclude,top,literal){relative.as_posix()}"


def _worktree_is_dirty(issue_client: IssueClient, exclude_dir: Path | None) -> bool:
    """Report whether the local checkout has any tracked or untracked changes.

    `exclude_dir`, when given, must be the exact run directory this command
    itself claimed and wrote artifacts into; any other change anywhere else
    in the worktree, including elsewhere under a shared `--artifacts-dir`,
    still counts. Passing None checks the complete worktree, which the
    pre-Oracle check requires since no run directory exists yet.

    Returns:
        True if the checkout has any pending tracked or untracked change
        outside exclude_dir.
    """
    args = ["status", "--porcelain", "--untracked-files=all"]
    if exclude_dir is not None:
        pathspec = _exclude_pathspec_for(issue_client.repo_dir, exclude_dir)
        if pathspec is not None:
            args = [*args, "--", ".", pathspec]
    status = issue_client.git_bytes(args, max_output=64 * 1024)
    return bool(status.strip())
