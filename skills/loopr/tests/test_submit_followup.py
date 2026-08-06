"""Regression tests for final submit review findings."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts.models import EXIT_PRECONDITION, JsonObject, LooprError
from scripts.process import CommandError, CommandResult
from scripts.submit import execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class TransientRemoteConfirmationRunner(ScenarioRunner):
    """Lose the push response and the first recovery read."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        """Initialize one ambiguous push with a transient remote read."""
        super().__init__(repo, remote, state)
        self.fail_after_push = True
        self.fail_first_recovery = True
        self.remote_updated = False

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = 24 * 1024 * 1024,
        watch_path: Path | None = None,
    ) -> CommandResult:
        """Fail the first ls-remote after the remote accepted the commit."""
        argv = [str(value) for value in args]
        if (
            argv[:4] == ["git", "ls-remote", "--refs", "origin"]
            and self.remote_updated
            and self.fail_first_recovery
        ):
            self.fail_first_recovery = False
            raise CommandError("temporary remote confirmation failure")
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[:2] == ["git", "push"] and self.fail_after_push:
            self.fail_after_push = False
            self.remote_updated = True
            raise CommandError("connection dropped after remote update")
        return result


def test_previous_artifacts_are_excluded_from_submit(tmp_path: Path) -> None:
    """A prior run directory never becomes part of the next commit."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    previous = repo / ".pr-loopr" / "runs" / "previous"
    previous.mkdir(parents=True)
    (previous / "result.json").write_text("{}\n", encoding="utf-8")
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=Path(".pr-loopr"),
        runner=ScenarioRunner(repo, remote, state),
    )

    committed_paths = _git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        result.commit_sha,
    ).splitlines()
    assert "file.txt" in committed_paths
    assert not any(path.startswith(".pr-loopr/") for path in committed_paths)
    assert (Path(result.artifacts_dir) / "result.json").is_file()


def test_only_previous_artifacts_remain_an_empty_patch(tmp_path: Path) -> None:
    """Audit output alone cannot satisfy the implementation-change check."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    previous = repo / ".pr-loopr" / "runs" / "previous"
    previous.mkdir(parents=True)
    (previous / "result.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=Path(".pr-loopr"),
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "empty_patch"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_transient_remote_confirmation_after_push_is_retried(
    tmp_path: Path,
) -> None:
    """One failed recovery read cannot negate an already-successful push."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = TransientRemoteConfirmationRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.fail_first_recovery is False
    assert result.resulting_head_sha == result.commit_sha
    assert (Path(result.artifacts_dir) / "push.json").is_file()
