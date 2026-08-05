"""Focused regression tests for immutable review evidence safety."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise Git directly
from typing import TYPE_CHECKING, cast

import pytest

from scripts.artifacts import ArtifactWriter
from scripts.github import GitHubClient
from scripts.models import EXIT_PRECONDITION, LooprError, PullRequest
from scripts.oracle import OracleClient
from scripts.process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path


def _git(
    git: str,
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> str:
    """Run one test-controlled Git command and return stripped stdout."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed test argv
        [git, *args],
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _sample_pr(
    base_sha: str,
    head_sha: str,
    paths: tuple[str, ...] = ("file.py",),
) -> PullRequest:
    """Return one valid frozen pull-request snapshot for local Git tests."""
    return PullRequest(
        repository="owner/repository",
        number=21,
        url="https://github.com/owner/repository/pull/21",
        title="Title",
        body="Body",
        author="author",
        state="OPEN",
        is_draft=False,
        base_ref="main",
        base_sha=base_sha,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=paths,
        raw={},
    )


def _repo_with_two_commits(tmp_path: Path) -> tuple[str, Path, str, str]:
    """Create a repository with distinct base and head blob contents."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["config", "user.email", "test@example.com"], cwd=repo)
    _git(git, ["config", "user.name", "Test"], cwd=repo)
    (repo / "file.py").write_text("base\n")
    _git(git, ["add", "file.py"], cwd=repo)
    _git(git, ["commit", "-q", "-m", "base"], cwd=repo)
    base = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    (repo / "file.py").write_text("expected\n")
    _git(git, ["commit", "-q", "-am", "head"], cwd=repo)
    head = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    return git, repo, base, head


def test_git_reads_ignore_replace_refs_and_injected_controls(tmp_path: Path) -> None:
    """Replace refs and inherited Git controls cannot redirect evidence reads."""
    git, repo, base, head = _repo_with_two_commits(tmp_path)
    malicious_blob = _git(
        git,
        ["hash-object", "-w", "--stdin"],
        cwd=repo,
        input_text="attacker\n",
    )
    malicious_tree = _git(
        git,
        ["mktree"],
        cwd=repo,
        input_text=f"100644 blob {malicious_blob}\tfile.py\n",
    )
    malicious_commit = _git(
        git,
        ["commit-tree", malicious_tree, "-p", base, "-m", "malicious"],
        cwd=repo,
    )
    _git(git, ["replace", head, malicious_commit], cwd=repo)

    runner = CommandRunner({
        **os.environ,
        "GIT_DIR": str(tmp_path / "redirected.git"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "redirected-worktree"),
        "GIT_NO_REPLACE_OBJECTS": "0",
    })
    client = GitHubClient(runner, repo, "token")

    data = client.changed_file_bytes(
        _sample_pr(base, head),
        "file.py",
        max_output=1024,
    )

    assert data == b"expected\n"


def test_changed_file_bytes_returns_none_for_deleted_path(tmp_path: Path) -> None:
    """A path absent from the frozen head is an explicit omission."""
    git, repo, base, _head = _repo_with_two_commits(tmp_path)
    (repo / "file.py").unlink()
    _git(git, ["commit", "-q", "-am", "delete"], cwd=repo)
    deleted_head = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    client = GitHubClient(CommandRunner(), repo, "token")

    assert (
        client.changed_file_bytes(
            _sample_pr(base, deleted_head),
            "file.py",
            max_output=1024,
        )
        is None
    )


class _FailingGitHub:
    """Provide valid bundle inputs but fail the changed-file Git read."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def patch(self, _pull_request: PullRequest, *, max_output: int) -> bytes:
        """Return a minimal valid UTF-8 patch."""
        del max_output
        return b"diff --git a/file.py b/file.py\n"

    def tracked_paths(self, _pull_request: PullRequest) -> tuple[str, ...]:
        """Return the changed path as a tracked head path."""
        return ("file.py",)

    def changed_file_bytes(
        self,
        _pull_request: PullRequest,
        _path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Inject an unexpected Git failure rather than an omission."""
        del max_output
        raise LooprError(EXIT_PRECONDITION, "git", "injected git failure")


def test_generic_git_failure_aborts_bundle_construction(tmp_path: Path) -> None:
    """Unexpected Git failures cannot be converted into omission evidence."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = _FailingGitHub(tmp_path)
    oracle = OracleClient(
        runner,
        cast("GitHubClient", github),
        writer,
        "heavy",
    )
    pull_request = _sample_pr("a" * 40, "b" * 40)

    with pytest.raises(LooprError, match="injected git failure"):
        oracle.build_bundle(pull_request)
