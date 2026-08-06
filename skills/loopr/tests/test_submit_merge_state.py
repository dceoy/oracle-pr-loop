"""Regression coverage for submit commit-shape validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts.models import EXIT_PRECONDITION, LooprError
from scripts.submit import execute_submit

if TYPE_CHECKING:
    from pathlib import Path


def test_resolved_merge_state_is_rejected_before_commit(tmp_path: Path) -> None:
    """A merge with no unmerged index entries cannot become the submitted commit."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "add side change")
    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "--no-commit", "side")

    assert not _git(repo, "ls-files", "-u")
    assert _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "conflict"
    assert _git(repo, "rev-parse", "HEAD") == head
    remote_head = _git(
        repo,
        "ls-remote",
        "--refs",
        "origin",
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == head
