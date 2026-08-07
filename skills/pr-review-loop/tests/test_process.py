"""Tests for bounded subprocess execution and redaction."""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- test-controlled process
import sys
from types import SimpleNamespace
from typing import cast, TYPE_CHECKING

import pytest

from scripts.process import CommandError, CommandRunner
from scripts import process as process_module

if TYPE_CHECKING:
    from pathlib import Path


def test_redactor_matches_credential_aliases() -> None:
    """Credential-like environment names register their values as secrets."""
    runner = CommandRunner({
        "SSH_PRIVATE_KEY": "private-key-secret",
        "DB_PASSWD": "passwd-secret-value",
        "AWS_ACCESS_KEY_ID": "access-key-secret",
    })

    for secret in (
        "private-key-secret",
        "passwd-secret-value",
        "access-key-secret",
    ):
        assert runner.contains_secret(secret)
        assert runner.redact(f"leaked: {secret}") == "leaked: [REDACTED]"


def test_runner_rejects_output_overflow(tmp_path: Path) -> None:
    """Output growth past the configured bound terminates the command."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"]

    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            command,
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
        )


def test_runner_rejects_watched_file_overflow(tmp_path: Path) -> None:
    """A watched side-effect file is bounded independently of stdout."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    watch_path = tmp_path / "watched.bin"
    script = (
        "import pathlib, time\n"
        f"pathlib.Path({str(watch_path)!r}).write_bytes(b'x' * 65536)\n"
        "time.sleep(5)\n"
    )

    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
            watch_path=watch_path,
        )


def test_runner_reaps_child_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """KeyboardInterrupt still terminates and reaps the direct child."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    pids: list[int] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
                *args,  # type: ignore[arg-type]
                **kwargs,
            ),
        )
        pids.append(proc.pid)
        return proc

    def raise_interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        process_module,
        "subprocess",
        SimpleNamespace(
            Popen=recording_popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(
        process_module,
        "time",
        SimpleNamespace(monotonic=process_module.time.monotonic, sleep=raise_interrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
