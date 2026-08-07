"""Regression coverage for credential values in raw staged path metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts.models import EXIT_PRECONDITION, LooprError
from scripts.submit import execute_guarded as execute_submit

if TYPE_CHECKING:
    from pathlib import Path


def test_escaped_credential_in_path_fails_before_commit(tmp_path: Path) -> None:
    """Git quoting cannot hide a known credential embedded in a pathname."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    credential = "known\\credential"
    (repo / f"{credential}.txt").write_text("safe\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)
    runner.secrets.add(credential)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "credentials"
    assert _git(repo, "rev-parse", "HEAD") == head
