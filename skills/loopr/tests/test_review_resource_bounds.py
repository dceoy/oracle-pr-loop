"""Regression tests for review resource and process cleanup bounds."""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from scripts import process as process_module
from scripts.artifacts import ArtifactWriter
from scripts.github import GitHubClient
from scripts.models import LooprError, PullRequest
from scripts.oracle import (
    MAX_INSTRUCTION_FILES,
    MAX_ORACLE_ARG_BYTES,
    MAX_ORACLE_ATTACHMENTS,
    OracleClient,
)
from scripts.process import (
    TERMINATION_GRACE_SECONDS,
    CommandError,
    CommandRunner,
)

if TYPE_CHECKING:
    import subprocess

SHA_A = "a" * 40
SHA_B = "b" * 40


def _sample_pr() -> PullRequest:
    """Return one valid frozen pull-request snapshot."""
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
        base_sha=SHA_A,
        head_ref="feature",
        head_sha=SHA_B,
        head_repository="owner/repository",
        changed_paths=("file.py",),
        raw={},
    )


class _TooManyInstructionsGitHub:
    """Expose an excessive repository-wide instruction-file inventory."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def tracked_paths(self, _pull_request: PullRequest) -> tuple[str, ...]:
        """Return more instruction files than the bundle contract allows."""
        return tuple(
            f"docs/{index}/AGENTS.md" for index in range(MAX_INSTRUCTION_FILES + 1)
        )

    def patch(self, _pull_request: PullRequest, *, max_output: int) -> bytes:
        """Return a minimal valid patch before instruction discovery is bounded."""
        del max_output
        return b"diff --git a/file.py b/file.py\n"


def test_bundle_rejects_excessive_instruction_file_inventory(tmp_path: Path) -> None:
    """Repository-wide instruction discovery is bounded before attachment reads."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = cast(
        "GitHubClient",
        _TooManyInstructionsGitHub(tmp_path),
    )
    oracle = OracleClient(runner, github, writer, "heavy")

    with pytest.raises(LooprError) as captured:
        oracle.build_bundle(_sample_pr())

    assert captured.value.category == "bundle"
    assert "instruction-file limit" in str(captured.value)


def test_oracle_review_rejects_excessive_attachment_count(tmp_path: Path) -> None:
    """The Oracle command cannot receive an unbounded number of --file arguments."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    attachments = tuple(
        Path(f"attachment-{index}.txt") for index in range(MAX_ORACLE_ATTACHMENTS + 1)
    )

    with pytest.raises(LooprError) as captured:
        oracle.review(_sample_pr(), attachments)

    assert captured.value.category == "bundle"
    assert "attachment count" in str(captured.value)


def test_oracle_review_rejects_excessive_argument_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The complete Oracle argv is byte-bounded before subprocess execution."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    oversized_path = Path("x" * MAX_ORACLE_ARG_BYTES)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Oracle subprocess must not run with oversized arguments")

    monkeypatch.setattr(runner, "run", unexpected_run)

    with pytest.raises(LooprError) as captured:
        oracle.review(_sample_pr(), (oversized_path,))

    assert captured.value.category == "bundle"
    assert "arguments exceed" in str(captured.value)


class _ExitedProcess:
    """Minimal Popen-compatible object whose group never disappears."""

    pid = 4242
    returncode = 0

    def poll(self) -> int:
        """Report that the direct child has already exited."""
        return 0

    def wait(self, timeout: float | None = None) -> int:
        """Return the already-observed exit status."""
        del timeout
        return 0


class _StuckProcess:
    """Popen-compatible process whose leader cannot be reaped after SIGKILL."""

    pid = 4343
    returncode = None

    def __init__(self) -> None:
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> None:
        """Report that the direct child remains alive."""
        return None

    def wait(self, timeout: float | None = None) -> int:
        """Record the bounded wait and simulate a leader that never exits."""
        self.wait_timeouts.append(timeout)
        raise process_module.subprocess.TimeoutExpired("fake", timeout)


def test_terminate_group_fails_when_final_sigkill_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup fails closed when the process group survives the final SIGKILL."""
    signals: list[int] = []

    def fake_killpg(_pgid: int, signal_number: int) -> None:
        signals.append(signal_number)

    ticks = iter((0.0, 3.0, 3.0, 6.0))
    monkeypatch.setattr(process_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        process_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        ),
    )

    process = cast("subprocess.Popen[bytes]", _ExitedProcess())
    with pytest.raises(CommandError, match="could not prove"):
        CommandRunner._terminate_group(process)

    assert [value for value in signals if value != 0] == [
        signal.SIGTERM,
        signal.SIGKILL,
    ]
    assert signals.count(0) == 2


def test_terminate_group_bounds_post_sigkill_leader_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-reapable leader cannot make post-SIGKILL cleanup wait forever."""
    signals: list[int] = []
    group_checks: list[tuple[int, float]] = []

    def fake_killpg(_pgid: int, signal_number: int) -> None:
        signals.append(signal_number)

    def fake_wait_for_group_exit(
        _cls: type[CommandRunner], pgid: int, timeout: float
    ) -> bool:
        group_checks.append((pgid, timeout))
        return False

    monkeypatch.setattr(process_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        CommandRunner,
        "_wait_for_group_exit",
        classmethod(fake_wait_for_group_exit),
    )

    stuck = _StuckProcess()
    process = cast("subprocess.Popen[bytes]", stuck)
    with pytest.raises(CommandError, match="could not prove"):
        CommandRunner._terminate_group(process)

    assert stuck.wait_timeouts == [
        TERMINATION_GRACE_SECONDS,
        TERMINATION_GRACE_SECONDS,
    ]
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert group_checks == [(stuck.pid, TERMINATION_GRACE_SECONDS)]
