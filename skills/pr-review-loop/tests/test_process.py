"""Tests for bounded subprocess execution and redaction."""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- test-controlled process
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from scripts import process as process_module
from scripts.models import LooprError
from scripts.process import CommandError, CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


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


def test_gh_env_preserves_ordinary_authentication_sources() -> None:
    """GitHub commands use the ordinary GH token and stored-auth settings."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "GH_TOKEN": "gh-token",
        "GITHUB_TOKEN": "github-token",
        "GH_CONFIG_DIR": "gh-config-dir",
    })

    environment = runner.gh_env()

    assert environment["GH_TOKEN"] == "gh-token"
    assert environment["GITHUB_TOKEN"] == "github-token"
    assert environment["GH_CONFIG_DIR"] == "gh-config-dir"


def test_oracle_env_preserves_remote_transport_configuration() -> None:
    """Oracle receives supported remote settings without putting tokens in argv."""
    runner = CommandRunner({
        "ORACLE_HOME_DIR": "oracle-home",
        "ORACLE_REMOTE_HOST": "oracle.example:9473",
        "ORACLE_REMOTE_TOKEN": "remote-token-value",
    })

    environment = runner.oracle_env()

    assert environment["ORACLE_HOME_DIR"] == "oracle-home"
    assert environment["ORACLE_REMOTE_HOST"] == "oracle.example:9473"
    assert environment["ORACLE_REMOTE_TOKEN"] == "remote-token-value"
    assert runner.redact("remote-token-value") == "[REDACTED]"


def test_oracle_remote_token_is_redacted_as_a_known_secret() -> None:
    """`ORACLE_REMOTE_TOKEN` is covered by the existing credential redaction."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_REMOTE_TOKEN": "remote-secret-token",
    })

    assert runner.contains_secret("remote-secret-token")
    assert runner.redact("token=remote-secret-token") == "token=[REDACTED]"


def test_command_error_keeps_bounded_redacted_completed_output(tmp_path: Path) -> None:
    """Retry classifiers can inspect failed streams without exposing secrets."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "API_TOKEN": "command-secret-value",
    })
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.stdout.write('stdout: command-secret-value'); "
            "sys.stderr.write('stderr: command-secret-value'); "
            "raise SystemExit(7)"
        ),
    ]

    with pytest.raises(CommandError) as captured:
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    error = captured.value
    assert error.returncode == 7
    assert error.stdout == "stdout: [REDACTED]"
    assert error.stderr == "stderr: [REDACTED]"
    assert "command-secret-value" not in str(error)


def test_oracle_remote_token_only_in_config_file_is_still_redacted(
    tmp_path: Path,
) -> None:
    """A token declared only in Oracle's config file is still a known secret."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteToken": "config-file-only-secret-token"}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    config_only_token = "config-file-only-secret-token"

    assert runner.contains_secret(config_only_token)
    assert runner.redact(f"token={config_only_token}") == "token=[REDACTED]"


def test_oracle_config_remote_token_is_trimmed_before_registration(
    tmp_path: Path,
) -> None:
    """A whitespace-padded config token registers as the trimmed value Oracle uses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteToken": " config-file-only-secret-token "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.contains_secret("config-file-only-secret-token")
    assert runner.redact("token=config-file-only-secret-token") == "token=[REDACTED]"


def test_oracle_config_remote_host_is_trimmed(tmp_path: Path) -> None:
    """A whitespace-padded config host is trimmed to the value Oracle uses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "  10.0.0.9:9473  "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_remote_host_whitespace_only_is_treated_as_unset(
    tmp_path: Path,
) -> None:
    """A whitespace-only config host is unset, matching Oracle's own trimming."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "   "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None


def test_oracle_config_remote_host_is_none_when_config_file_is_absent(
    tmp_path: Path,
) -> None:
    """A missing Oracle config file leaves the config-backed remote host unset."""
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None


def test_oracle_home_dir_config_is_read_without_an_extra_oracle_subdirectory(
    tmp_path: Path,
) -> None:
    """`ORACLE_HOME_DIR` points at Oracle's config directory, not its parent."""
    (tmp_path / "config.json").write_text(
        '{"browser": {"remoteHost": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_HOME_DIR": str(tmp_path),
    })

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_with_json5_syntax_fails_closed_on_access(
    tmp_path: Path,
) -> None:
    """A JSON5-only config file must not be silently treated as remote-free."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{\n  // remote transport\n  "browser": {"remoteHost": "10.0.0.9:9473"},\n}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_json5_syntax_does_not_break_construction(
    tmp_path: Path,
) -> None:
    """Construction must not raise, so commands that skip Oracle stay unaffected."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{\n  // remote transport\n  "browser": {"remoteHost": "10.0.0.9:9473"},\n}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.contains_secret("anything") is False
    assert runner.redact("no secrets here") == "no secrets here"


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
    mocker: MockerFixture,
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

    mocker.patch.object(
        process_module,
        "subprocess",
        SimpleNamespace(
            Popen=recording_popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    mocker.patch.object(
        process_module,
        "time",
        SimpleNamespace(monotonic=process_module.time.monotonic, sleep=raise_interrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
