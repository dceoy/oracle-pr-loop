"""Structured CLI contract tests for bootstrap, review, and submit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest
from scripts import cli
from scripts.models import (
    BootstrapResult,
    ReviewLoopError,
    ReviewResult,
    SubmitResult,
)

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

    from pytest_mock import MockerFixture

SHA_A = "a" * 40
SHA_B = "b" * 40


def _stdout_object(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], str]:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value, captured.err


def test_missing_subcommand_uses_documented_top_level_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "pr-review-loop"
    assert value["schema_version"] == 1
    assert value["error"] == {
        "category": "input",
        "message": "the following arguments are required: command",
    }
    assert "pr-review-loop pr-review-loop:" in stderr


def test_unknown_subcommand_uses_documented_top_level_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["unknown"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "pr-review-loop"
    assert value["error"]["category"] == "input"  # type: ignore[index]
    assert "invalid choice" in value["error"]["message"]  # type: ignore[index]
    assert stderr


def test_invalid_root_option_uses_documented_top_level_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["--invalid-option"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "pr-review-loop"
    assert value["error"]["category"] == "input"  # type: ignore[index]
    assert stderr


def test_recognized_command_missing_required_option_keeps_attribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["review"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "review"
    assert value["error"] == {
        "category": "input",
        "message": "the following arguments are required: --pr",
    }
    assert "pr-review-loop review:" in stderr


def test_recognized_command_unknown_option_keeps_attribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["submit", "--pr", "1", "--expected-head", SHA_A, "--wat"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "submit"
    assert (
        "unrecognized arguments: --wat" in value["error"]["message"]  # type: ignore[index]
    )
    assert stderr


def test_invalid_option_value_keeps_recognized_command_attribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([
        "bootstrap",
        "--issue",
        "7",
        "--oracle-thinking-time",
        "maximum",
    ])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "bootstrap"
    assert "invalid choice" in value["error"]["message"]  # type: ignore[index]
    assert stderr


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("bootstrap", ["bootstrap", "--issue", "7", "--bad"]),
        ("review", ["review", "--pr", "7", "--bad"]),
        (
            "submit",
            ["submit", "--pr", "7", "--expected-head", SHA_A, "--bad"],
        ),
    ],
)
def test_each_recognized_command_owns_its_parse_failures(
    command: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(argv)
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == command
    assert value["error"]["category"] == "input"  # type: ignore[index]
    assert stderr


def test_root_help_is_issue_and_pr_oriented() -> None:
    help_text = cli.parser().format_help()

    assert "Bootstrap one exact GitHub Issue" in help_text
    assert "GitHub pull" in help_text
    assert "request" in help_text
    assert "turn one open GitHub Issue" in help_text
    assert "review and post one exact pull-request snapshot" in help_text


def test_subcommand_help_describes_bootstrap_issue_and_pr_commands() -> None:
    root = cli.parser()
    subparsers = cast(
        "argparse._SubParsersAction[argparse.ArgumentParser]",
        next(action for action in root._actions if action.dest == "command"),
    )
    choices = subparsers.choices
    bootstrap_description = choices["bootstrap"].description
    review_description = choices["review"].description
    submit_description = choices["submit"].description

    assert bootstrap_description is not None
    assert review_description is not None
    assert submit_description is not None
    assert "GitHub Issue" in bootstrap_description
    assert "pull-request snapshot" in review_description
    assert "GitHub pull request" in submit_description


def test_bootstrap_success_schema_is_stable(
    tmp_path: Path,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = BootstrapResult(
        repository="owner/repository",
        issue_number=7,
        issue_url="https://github.com/owner/repository/issues/7",
        issue_updated_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha=SHA_A,
        implementation_prompt="Implement it.",
    )
    execute = mocker.patch.object(cli, "execute_bootstrap", return_value=result)

    code = cli.main([
        "bootstrap",
        "--issue",
        "7",
        "--repo-dir",
        str(tmp_path),
        "--oracle-model",
        "gpt-5.6-sol",
        "--oracle-thinking-time",
        "heavy",
    ])
    value, stderr = _stdout_object(capsys)

    assert code == 0
    assert stderr == ""
    assert value == result.as_json()
    execute.assert_called_once()
    kwargs = execute.call_args.kwargs
    assert kwargs["issue_value"] == "7"
    assert kwargs["repo_dir"] == tmp_path
    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["thinking_time"] == "heavy"


def test_review_success_schema_is_stable(
    tmp_path: Path,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ReviewResult(
        repository="owner/repository",
        pr_number=21,
        base_sha=SHA_A,
        head_sha=SHA_B,
        verdict="APPROVE",
        github_review_id=123,
        blocking_findings=(),
        implementation_prompt=None,
    )
    execute = mocker.patch.object(cli, "execute_review", return_value=result)

    code = cli.main(["review", "--pr", "21", "--repo-dir", str(tmp_path)])
    value, stderr = _stdout_object(capsys)

    assert code == 0
    assert stderr == ""
    assert value == result.as_json()
    execute.assert_called_once()
    assert execute.call_args.kwargs["pr_value"] == "21"


def test_submit_success_schema_is_stable(
    tmp_path: Path,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = SubmitResult(
        repository="owner/repository",
        pr_number=21,
        base_sha=SHA_A,
        previous_head_sha=SHA_B,
        resulting_head_sha="c" * 40,
        commit_sha="c" * 40,
        pushed_branch="feature",
    )
    execute = mocker.patch.object(cli, "execute_submit", return_value=result)

    code = cli.main([
        "submit",
        "--pr",
        "21",
        "--expected-head",
        SHA_B,
        "--repo-dir",
        str(tmp_path),
    ])
    value, stderr = _stdout_object(capsys)

    assert code == 0
    assert stderr == ""
    assert value == result.as_json()
    execute.assert_called_once()
    assert execute.call_args.kwargs["expected_head"] == SHA_B


def test_cli_redacts_structured_and_stderr_failures(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "remote-secret-token"
    mocker.patch.dict(
        "os.environ",
        {"ORACLE_REMOTE_TOKEN": secret},
        clear=False,
    )
    mocker.patch.object(
        cli,
        "execute_review",
        side_effect=ReviewLoopError(3, "oracle", f"failed with {secret}"),
    )

    code = cli.main(["review", "--pr", "21"])
    value, stderr = _stdout_object(capsys)

    assert code == 3
    assert value["command"] == "review"
    assert secret not in json.dumps(value)
    assert secret not in stderr
    assert "[REDACTED]" in stderr


def test_keyboard_interrupt_is_structured_and_command_attributed(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch.object(cli, "execute_review", side_effect=KeyboardInterrupt)

    code = cli.main(["review", "--pr", "21"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "review"
    assert value["error"] == {
        "category": "interrupted",
        "message": "interrupted; failed closed",
    }
    assert stderr


def test_unexpected_exception_is_structured_and_fail_closed(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch.object(cli, "execute_review", side_effect=RuntimeError("boom"))

    code = cli.main(["review", "--pr", "21"])
    value, stderr = _stdout_object(capsys)

    assert code == 2
    assert value["command"] == "review"
    assert value["error"] == {
        "category": "internal",
        "message": "RuntimeError: boom",
    }
    assert stderr
