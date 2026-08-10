"""Real-Git regression tests for immutable inline-anchor safety."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.github import GitHubClient
from scripts.models import PullRequest
from scripts.process import CommandRunner


def _runner() -> CommandRunner:
    """Return a runner with only the host PATH needed by Git."""
    return CommandRunner({"PATH": os.environ["PATH"]})


def _git(
    runner: CommandRunner,
    repo: Path,
    *args: str,
) -> bytes:
    """Run one test-controlled Git command and return stdout."""
    return runner.run(
        ["git", *args],
        cwd=repo,
        env=runner.base_env(),
        max_output=1024 * 1024,
    ).stdout


def _init_repo(tmp_path: Path) -> tuple[CommandRunner, Path]:
    """Create one isolated repository with deterministic commit identity."""
    runner = _runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(runner, repo, "init", "--quiet")
    _git(runner, repo, "config", "user.name", "Test User")
    _git(runner, repo, "config", "user.email", "test@example.com")
    return runner, repo


def _commit(runner: CommandRunner, repo: Path, message: str) -> str:
    """Commit the worktree and return the immutable commit SHA."""
    _git(runner, repo, "add", "-A")
    _git(runner, repo, "commit", "--quiet", "-m", message)
    return _git(runner, repo, "rev-parse", "HEAD").decode().strip()


def test_local_info_attributes_cannot_force_inline_filtering(tmp_path: Path) -> None:
    runner, repo = _init_repo(tmp_path)
    (repo / "file.py").write_text("print('safe')\n", encoding="utf-8")
    sha = _commit(runner, repo, "base")
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("file.py -diff\n", encoding="utf-8")

    client = GitHubClient(runner, repo)

    assert (
        client.paths_with_diff_unset(
            sha,
            frozenset({"file.py"}),
            max_output=4096,
        )
        == frozenset()
    )


def test_committed_attributes_are_read_from_the_immutable_commit(
    tmp_path: Path,
) -> None:
    runner, repo = _init_repo(tmp_path)
    (repo / "file.py").write_text("print('safe')\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("file.py -diff\n", encoding="utf-8")
    sha = _commit(runner, repo, "base")

    client = GitHubClient(runner, repo)

    assert client.paths_with_diff_unset(
        sha,
        frozenset({"file.py"}),
        max_output=4096,
    ) == frozenset({"file.py"})


def test_rename_uses_base_path_for_immutable_attribute_check(tmp_path: Path) -> None:
    runner, repo = _init_repo(tmp_path)
    original = "\n".join(f"line {index}" for index in range(1, 11)) + "\n"
    (repo / "old.py").write_text(original, encoding="utf-8")
    (repo / ".gitattributes").write_text("old.py -diff\n", encoding="utf-8")
    base_sha = _commit(runner, repo, "base")

    _git(runner, repo, "mv", "old.py", "new.py")
    updated = original.replace("line 5", "line five")
    (repo / "new.py").write_text(updated, encoding="utf-8")
    head_sha = _commit(runner, repo, "rename")

    pull_request = PullRequest(
        repository="owner/repository",
        number=21,
        url="https://github.com/owner/repository/pull/21",
        title="Rename",
        body="",
        author="author",
        state="OPEN",
        is_draft=False,
        base_ref="main",
        base_sha=base_sha,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=("new.py",),
    )
    client = GitHubClient(runner, repo)

    assert client.diff_anchors(pull_request) == frozenset()
