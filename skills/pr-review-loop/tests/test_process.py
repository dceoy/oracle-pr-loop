"""Security and subprocess-boundary tests for command execution."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts.models import EXIT_PRECONDITION, ReviewLoopError
from scripts.process import (
    MAX_INPUT,
    CommandError,
    CommandRunner,
    normalize_oracle_remote_value,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (1, None),
        ("", None),
        ("   ", None),
        (" host:9473 ", "host:9473"),
        ("\ufeff\u3000host:9473\u00a0", "host:9473"),
    ],
)
def test_normalize_oracle_remote_value_matches_oracle_trim(
    value: object,
    expected: str | None,
) -> None:
    assert normalize_oracle_remote_value(value) == expected


def test_runner_registers_generic_credentials_and_remote_token() -> None:
    runner = CommandRunner({
        "PATH": os.environ.get("PATH", ""),
        "GH_TOKEN": "github-secret-token",
        "ORACLE_REMOTE_TOKEN": "  remote-secret-token  ",
        "ORDINARY": "not-secret",
    })

    assert runner.contains_secret("github-secret-token")
    assert runner.contains_secret("remote-secret-token")
    assert not runner.contains_secret("not-secret")
    assert runner.redact("a github-secret-token b remote-secret-token c") == (
        "a [REDACTED] b [REDACTED] c"
    )


@pytest.mark.parametrize("token", ["x", "xy", "xyz"])
def test_short_remote_tokens_are_still_registered(token: str) -> None:
    runner = CommandRunner({
        "PATH": os.environ.get("PATH", ""),
        "ORACLE_REMOTE_TOKEN": token,
    })

    assert runner.contains_secret(token)
    assert runner.redact(f"before-{token}-after") == "before-[REDACTED]-after"


def test_allowlisted_environments_do_not_forward_unrelated_secrets() -> None:
    runner = CommandRunner({
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "GH_TOKEN": "github-secret",
        "GITHUB_TOKEN": "github-secret-2",
        "ORACLE_REMOTE_HOST": "127.0.0.1:9473",
        "ORACLE_REMOTE_TOKEN": "oracle-secret",
        "ORACLE_HOME_DIR": "/host/config",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
        "UNRELATED": "value",
    })

    gh = runner.gh_env()
    oracle = runner.oracle_env()

    assert gh["GH_TOKEN"] == "github-secret"
    assert gh["GITHUB_TOKEN"] == "github-secret-2"
    assert "ORACLE_REMOTE_TOKEN" not in gh
    assert oracle["ORACLE_REMOTE_HOST"] == "127.0.0.1:9473"
    assert oracle["ORACLE_REMOTE_TOKEN"] == "oracle-secret"
    assert oracle["HOME"] == "/home/tester"
    assert "ORACLE_HOME_DIR" not in oracle
    assert "AWS_SECRET_ACCESS_KEY" not in gh
    assert "AWS_SECRET_ACCESS_KEY" not in oracle
    assert "UNRELATED" not in gh
    assert "UNRELATED" not in oracle


def test_oracle_env_resolves_home_when_source_omits_it(
    mocker: MockerFixture,
) -> None:
    runner = CommandRunner({"PATH": "/usr/bin"})
    mocker.patch.object(Path, "home", return_value=Path("/resolved/home"))

    assert runner.oracle_env()["HOME"] == "/resolved/home"


def test_oracle_env_fails_closed_when_home_cannot_be_resolved(
    mocker: MockerFixture,
) -> None:
    runner = CommandRunner({"PATH": "/usr/bin"})
    mocker.patch.object(Path, "home", side_effect=RuntimeError("no home"))

    with pytest.raises(ReviewLoopError) as captured:
        runner.oracle_env()

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "bundle"


def test_trusted_requires_absolute_path_entries() -> None:
    runner = CommandRunner({"PATH": "relative:also-relative"})

    with pytest.raises(CommandError, match="required executable not found"):
        runner.trusted("python")


def test_run_captures_bounded_stdout_and_stderr(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
        ],
        cwd=tmp_path,
        env=runner.base_env(),
    )

    assert result.returncode == 0
    assert result.stdout == b"out"
    assert result.stderr == "err"
    assert Path(result.args[0]).is_absolute()


def test_run_redacts_nonzero_command_output(tmp_path: Path) -> None:
    secret = "remote-secret-token"
    runner = CommandRunner({
        "PATH": os.environ.get("PATH", ""),
        "ORACLE_REMOTE_TOKEN": secret,
    })

    command = "; ".join(
        (
            f"import sys; print({secret!r})",
            f"sys.stderr.write({secret!r})",
            "sys.exit(7)",
        ),
    )

    with pytest.raises(CommandError) as captured:
        runner.run(
            [
                sys.executable,
                "-c",
                command,
            ],
            cwd=tmp_path,
            env=runner.base_env(),
        )

    assert captured.value.returncode == 7
    assert secret not in str(captured.value)
    assert secret not in captured.value.stdout
    assert secret not in captured.value.stderr
    assert "[REDACTED]" in str(captured.value)


def test_run_rejects_nul_arguments(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})

    with pytest.raises(CommandError, match="invalid subprocess argument vector"):
        runner.run(
            [sys.executable, "bad\0arg"],
            cwd=tmp_path,
            env=runner.base_env(),
        )


def test_run_rejects_oversized_input(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})

    with pytest.raises(CommandError, match="command input exceeded bound"):
        runner.run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env=runner.base_env(),
            input_text="x" * (MAX_INPUT + 1),
        )


def test_run_rejects_invalid_bounds(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})

    with pytest.raises(CommandError, match="subprocess bounds must be positive"):
        runner.run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=0,
        )


def test_run_enforces_stdout_bound_while_process_runs(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})

    with pytest.raises(CommandError, match="command output exceeded bound"):
        runner.run(
            [sys.executable, "-c", "print('x' * 10000)"],
            cwd=tmp_path,
            env=runner.base_env(),
            max_output=128,
        )


def test_run_enforces_watched_file_bound(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})
    watched = tmp_path / "out"
    program = (
        f"from pathlib import Path; Path({str(watched)!r}).write_bytes(b'x' * 10000)"
    )

    with pytest.raises(CommandError, match="command output exceeded bound"):
        runner.run(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            env=runner.base_env(),
            max_output=128,
            watch_path=watched,
        )


def test_run_times_out_and_reaps_child(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})

    with pytest.raises(CommandError, match="command timed out"):
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=0.05,
        )


def test_check_false_preserves_nonzero_result(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": os.environ.get("PATH", "")})
    result = runner.run(
        [sys.executable, "-c", "raise SystemExit(9)"],
        cwd=tmp_path,
        env=runner.base_env(),
        check=False,
    )

    assert result.returncode == 9
