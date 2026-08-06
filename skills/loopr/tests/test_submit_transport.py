"""Regression tests for hardened submit transport behavior."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts.models import EXIT_PRECONDITION, JsonObject, LooprError
from scripts.process import CommandError, CommandResult
from scripts.submit import execute_submit


class MultiplePushUrlRunner(ScenarioRunner):
    """Expose two configured push destinations."""

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
        """Return every configured push URL for the guarded preflight."""
        argv = [str(value) for value in args]
        if argv == [
            "git",
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        ]:
            return CommandResult(
                tuple(argv),
                0,
                (
                    b"https://github.com/acme/demo.git\n"
                    b"https://github.com/acme/mirror.git\n"
                ),
                "",
            )
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


class AmbiguousPushRunner(ScenarioRunner):
    """Update the remote, then simulate a lost local push response."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        """Initialize one post-update failure."""
        super().__init__(repo, remote, state)
        self.fail_after_push = True

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
        """Raise only after the real local push has updated the remote."""
        argv = [str(value) for value in args]
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
            raise CommandError("connection dropped after remote update")
        return result


def test_multiple_push_urls_fail_before_staging(tmp_path: Path) -> None:
    """A second push destination is rejected before local mutation."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = MultiplePushUrlRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "repository"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_ambiguous_push_failure_accepts_updated_remote(tmp_path: Path) -> None:
    """A post-update command error proceeds through GitHub confirmation."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = AmbiguousPushRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == result.commit_sha
    assert result.resulting_head_sha == result.commit_sha
    assert (Path(result.artifacts_dir) / "push.json").is_file()
