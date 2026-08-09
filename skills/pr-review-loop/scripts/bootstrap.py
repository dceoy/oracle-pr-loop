"""Issue bootstrap command orchestration."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .artifacts import temporary_file_writer
from .github import IssueClient
from .models import EXIT_PRECONDITION, EXIT_RACE, BootstrapResult, LooprError
from .oracle import (
    BOOTSTRAP_PROMPT,
    MAX_BOOTSTRAP_ATTACHMENTS,
    build_bootstrap_bundle,
    invoke_oracle,
    parse_bootstrap,
)
from .process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path


def execute_bootstrap(
    *,
    issue_value: str,
    repo_dir: Path,
    thinking_time: str,
    runner: CommandRunner | None = None,
) -> BootstrapResult:
    """Turn one open GitHub Issue into a bounded implementation prompt.

    Returns:
        The stable bootstrap command result.

    Raises:
        LooprError: The Issue, base branch, local workspace, temporary files,
            or Oracle output violated a precondition, or the issue, base
            branch, or local workspace changed during prompt generation.
    """
    command_runner = runner or CommandRunner()
    issue_client = IssueClient(command_runner, repo_dir)
    issue_client.initialize(issue_value)
    initial = issue_client.snapshot()
    base_ref, base_sha = _base_snapshot(issue_client)
    _ensure_base_available(issue_client, base_ref, base_sha)
    _ensure_workspace_bound_to_base(issue_client, base_ref, base_sha)

    with temporary_file_writer(
        command_runner,
        prefix=f"pr-review-loop-bootstrap-{initial.number}-",
    ) as writer:
        attachments = build_bootstrap_bundle(
            command_runner,
            issue_client,
            writer,
            initial,
            base_sha,
        )
        prompt = BOOTSTRAP_PROMPT.format(
            repository=initial.repository,
            issue_number=initial.number,
            base_ref=base_ref,
            base_sha=base_sha,
        )
        slug = (
            f"loopr-bootstrap-{initial.number}-{base_sha[:12]}-{uuid.uuid4().hex[:8]}"
        )
        raw = invoke_oracle(
            command_runner,
            writer,
            issue_client.repo_dir,
            thinking_time,
            prompt,
            attachments,
            slug,
            max_attachments=MAX_BOOTSTRAP_ATTACHMENTS,
        )
        generated = parse_bootstrap(raw, initial, base_sha)

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
        or _worktree_is_dirty(issue_client)
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
    )
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
    `bootstrap` promises not to mutate Git state, so it cannot create the
    feature branch itself; it instead requires one to already exist, since
    the skill tells the host to commit, push, and open a pull request straight
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
    if _worktree_is_dirty(issue_client):
        raise LooprError(
            EXIT_PRECONDITION,
            "workspace",
            "local checkout has uncommitted tracked changes or untracked "
            "files; commit, stash, or discard them before running bootstrap",
        )


def _worktree_is_dirty(issue_client: IssueClient) -> bool:
    """Report whether the local checkout has any tracked or untracked changes.

    Returns:
        True if the checkout has any pending tracked or untracked change.
    """
    status = issue_client.git_bytes(
        ["status", "--porcelain", "--untracked-files=all"],
        max_output=64 * 1024,
    )
    return bool(status.strip())
