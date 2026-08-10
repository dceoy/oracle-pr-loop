"""Real-Git regression tests for immutable inline-anchor safety."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from scripts.github import GitHubClient
from scripts.process import CommandRunner

from .support import commit_all, git, init_git_repo, sample_pr

if TYPE_CHECKING:
    from pathlib import Path


def _runner() -> CommandRunner:
    """Return a runner with only the host PATH needed by Git."""
    return CommandRunner({"PATH": os.environ["PATH"]})


def test_local_info_attributes_cannot_force_inline_filtering(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "file.py").write_text("print('safe')\n", encoding="utf-8")
    sha = commit_all(repo, "base")
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("file.py -diff\n", encoding="utf-8")

    client = GitHubClient(_runner(), repo)

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
    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "file.py").write_text("print('safe')\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("file.py -diff\n", encoding="utf-8")
    sha = commit_all(repo, "base")

    client = GitHubClient(_runner(), repo)

    assert client.paths_with_diff_unset(
        sha,
        frozenset({"file.py"}),
        max_output=4096,
    ) == frozenset({"file.py"})


def test_rename_uses_base_path_for_immutable_attribute_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    original = "\n".join(f"line {index}" for index in range(1, 11)) + "\n"
    (repo / "old.py").write_text(original, encoding="utf-8")
    (repo / ".gitattributes").write_text("old.py -diff\n", encoding="utf-8")
    base_sha = commit_all(repo, "base")

    git(repo, "mv", "old.py", "new.py")
    updated = original.replace("line 5", "line five")
    (repo / "new.py").write_text(updated, encoding="utf-8")
    head_sha = commit_all(repo, "rename")

    pull_request = sample_pr(
        title="Rename",
        body="",
        base_sha=base_sha,
        head_sha=head_sha,
        changed_paths=("new.py",),
    )
    client = GitHubClient(_runner(), repo)

    assert client.diff_anchors(pull_request) == frozenset()
