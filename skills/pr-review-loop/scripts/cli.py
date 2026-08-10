"""Vendor-neutral pull-request review skill CLI."""

from __future__ import annotations

import argparse
import json
import sys
import typing
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from .bootstrap import execute_bootstrap
from .models import (
    EXIT_PRECONDITION,
    BootstrapResult,
    JsonObject,
    ReviewLoopError,
    ReviewResult,
    SubmitResult,
)
from .process import CommandRunner
from .review import execute_review
from .submit import execute_submit

if TYPE_CHECKING:
    from collections.abc import Sequence

CLI_COMMAND = "pr-review-loop"
COMMANDS = frozenset({"bootstrap", "review", "submit"})


class StructuredArgumentParser(argparse.ArgumentParser):
    """Raise structured command errors instead of terminating with prose."""

    @typing.override
    def error(self, message: str) -> NoReturn:
        """Convert argparse validation failures into ReviewLoopError.

        Raises:
            ReviewLoopError: Always.
        """
        raise ReviewLoopError(EXIT_PRECONDITION, "input", message)


def parser() -> argparse.ArgumentParser:
    """Build the stable skill command parser.

    Returns:
        The configured argument parser for the `bootstrap`, `review`, and
        `submit` commands.
    """
    root = StructuredArgumentParser(
        description=(
            "Bootstrap one exact GitHub Issue, or review or submit one exact "
            "GitHub pull request."
        )
    )
    subcommands = root.add_subparsers(dest="command", required=True)
    bootstrap = subcommands.add_parser(
        "bootstrap",
        help="turn one open GitHub Issue into a bounded implementation prompt",
        description=(
            "Turn one exact open GitHub Issue into a bounded implementation prompt."
        ),
    )
    review = subcommands.add_parser(
        "review",
        help="review and post one exact pull-request snapshot",
        description="Review and post one exact GitHub pull-request snapshot.",
    )
    submit = subcommands.add_parser(
        "submit",
        help="validate, commit, and lease-protect a pull-request workspace patch",
        description=(
            "Validate, commit, and lease-protect the workspace patch for one exact "
            "GitHub pull request."
        ),
    )

    for command in (review, submit):
        command.add_argument(
            "--pr",
            required=True,
            help="positive PR number or canonical GitHub pull URL",
        )
        command.add_argument(
            "--repo-dir",
            default=".",
            help="local checkout containing the target pull request",
        )

    bootstrap.add_argument(
        "--issue",
        required=True,
        help="positive Issue number or canonical GitHub issue URL",
    )
    bootstrap.add_argument(
        "--repo-dir",
        default=".",
        help="local checkout containing the target repository",
    )
    for command in (bootstrap, review):
        command.add_argument(
            "--oracle-model",
            metavar="MODEL",
            help="select an Oracle browser model instead of the current model",
        )
        command.add_argument(
            "--oracle-thinking-time",
            choices=("light", "standard", "extended", "heavy"),
            metavar="EFFORT",
            help="override Oracle browser effort; omitted means inherit",
        )
    submit.add_argument(
        "--expected-head",
        required=True,
        help="full PR head SHA on which the workspace is based",
    )
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one command and emit exactly one JSON object on stdout.

    Returns:
        The process exit code.
    """
    command = _requested_command(argv)
    runner = CommandRunner()
    try:
        args = parser().parse_args(argv)
        command = args.command
        result = _dispatch(command, args, runner)
    except ReviewLoopError as exc:
        message = runner.redact(str(exc))
        _emit_error(command, exc.category, message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return exc.code
    except KeyboardInterrupt:
        message = "interrupted; failed closed"
        _emit_error(command, "interrupted", message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return EXIT_PRECONDITION
    except Exception as exc:  # ruff: ignore[blind-except] -- top-level entry point must fail closed on any error
        message = runner.redact(f"{type(exc).__name__}: {exc}")
        _emit_error(command, "internal", message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return EXIT_PRECONDITION
    else:
        _emit(result.as_json())
        return 0


def _dispatch(
    command: str, args: argparse.Namespace, runner: CommandRunner
) -> BootstrapResult | ReviewResult | SubmitResult:
    """Run the requested command against its parsed arguments.

    Returns:
        The completed command's result.
    """
    if command == "bootstrap":
        return execute_bootstrap(
            issue_value=args.issue,
            repo_dir=Path(args.repo_dir),
            thinking_time=args.oracle_thinking_time,
            model=args.oracle_model,
            runner=runner,
        )
    if command == "review":
        return execute_review(
            pr_value=args.pr,
            repo_dir=Path(args.repo_dir),
            thinking_time=args.oracle_thinking_time,
            model=args.oracle_model,
            runner=runner,
        )
    return execute_submit(
        pr_value=args.pr,
        expected_head=args.expected_head,
        repo_dir=Path(args.repo_dir),
        runner=runner,
    )


def _requested_command(argv: Sequence[str] | None) -> str:
    """Return the attributable command before full argparse validation.

    A recognized first token remains attributable even when later option parsing
    fails. Missing or unknown subcommands use the documented top-level command
    label rather than pretending the failure belongs to a real subcommand.
    """
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in COMMANDS:
        return values[0]
    return CLI_COMMAND


def _emit_error(command: str, category: str, message: str) -> None:
    """Emit the stable structured failure schema."""
    _emit({
        "schema_version": 1,
        "command": command,
        "error": {"category": category, "message": message},
    })


def _emit(value: JsonObject) -> None:
    """Write one compact JSON object and one trailing newline."""
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(f"{serialized}\n")


if __name__ == "__main__":
    raise SystemExit(main())
