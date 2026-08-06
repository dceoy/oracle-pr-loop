"""Regression coverage for the push-recovery deadline boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts import submit_core
from scripts.models import JsonObject
from scripts.process import CommandError, CommandResult
from scripts.submit import execute_submit


class RecoveryDeadlineRunner(ScenarioRunner):
    """Advance the remote only after wrapper recovery reaches its deadline."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        """Initialize one delayed remote update after a lost push response."""
        super().__init__(repo, remote, state)
        self.pending_commit: str | None = None
        self.recovery_reads = 0

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
        """Expose the leased head first and the created commit on fallback."""
        argv = [str(value) for value in args]
        if argv[:2] == ["git", "push"]:
            self.pending_commit = argv[-1].split(":", 1)[0]
            raise CommandError("connection dropped before remote confirmation")
        if (
            argv[:4] == ["git", "ls-remote", "--refs", "origin"]
            and self.pending_commit is not None
        ):
            self.recovery_reads += 1
            if self.recovery_reads == 2:
                commit_sha = self.pending_commit
                _git(
                    self.repo,
                    "push",
                    str(self.remote),
                    f"{commit_sha}:refs/heads/feature",
                )
                self.state["headRefOid"] = commit_sha
                self.pending_commit = None
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )


def test_fallback_accepts_commit_after_recovery_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit observed by the core fallback remains a successful push."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = RecoveryDeadlineRunner(repo, remote, state)
    monkeypatch.setattr(submit_core, "POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(submit_core, "POLL_INTERVAL_SECONDS", 0)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.recovery_reads == 2
    assert runner.pending_commit is None
    assert result.resulting_head_sha == result.commit_sha
    assert (Path(result.artifacts_dir) / "push.json").is_file()
