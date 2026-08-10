"""Small shared builders and real-Git primitives for tests."""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise Git directly
from typing import TYPE_CHECKING

from scripts.models import IssueSnapshot, JsonObject, PullRequest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def sample_pr(
    *,
    repository: str = "owner/repository",
    number: int = 21,
    title: str = "Title",
    body: str = "Body",
    author: str = "author",
    base_ref: str = "main",
    base_sha: str = SHA_A,
    head_ref: str = "feature",
    head_sha: str = SHA_B,
    changed_paths: tuple[str, ...] = ("file.py",),
) -> PullRequest:
    """Return one valid open same-repository pull-request snapshot."""
    return PullRequest(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/pull/{number}",
        title=title,
        body=body,
        author=author,
        state="OPEN",
        is_draft=False,
        base_ref=base_ref,
        base_sha=base_sha,
        head_ref=head_ref,
        head_sha=head_sha,
        head_repository=repository,
        changed_paths=changed_paths,
    )


def sample_issue(
    *,
    repository: str = "owner/repository",
    number: int = 7,
    title: str = "Title",
    body: str = "Body",
    author: str = "author",
    updated_at: str = "2026-01-01T00:00:00Z",
    comments: tuple[JsonObject, ...] = (),
) -> IssueSnapshot:
    """Return one valid open Issue snapshot."""
    return IssueSnapshot(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        body=body,
        author=author,
        state="OPEN",
        updated_at=updated_at,
        comments=comments,
    )


GIT = shutil.which("git")
if GIT is None:
    raise RuntimeError("git is required for integration tests")


def run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one test-controlled process and capture bytes."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        list(args),
        cwd=cwd,
        env=None if env is None else dict(env),
        input=None if input_text is None else input_text.encode(),
        check=check,
        capture_output=True,
    )


def git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run Git in a test repository and return stripped UTF-8 stdout."""
    return (
        run_process([GIT, *args], cwd=repo, env=env)
        .stdout.decode("utf-8", "strict")
        .strip()
    )


def init_git_repo(
    repo: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Initialize a repository with deterministic test identity."""
    repo.mkdir()
    git(repo, "init", "--quiet", env=env)
    git(repo, "config", "user.name", "Test User", env=env)
    git(repo, "config", "user.email", "test@example.com", env=env)


def commit_all(
    repo: Path,
    message: str,
    *,
    allow_empty: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    """Commit the full worktree and return the new commit SHA."""
    git(repo, "add", "-A", env=env)
    args = ["commit", "--quiet"]
    if allow_empty:
        args.append("--allow-empty")
    git(repo, *args, "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD", env=env)
