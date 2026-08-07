"""Vendor-neutral pull-request review skill CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from .models import EXIT_PRECONDITION, JsonObject, LooprError
from .process import CommandRunner
from .review import execute_review
from .submit import execute_guarded as execute_submit

if TYPE_CHECKING:
    from collections.abc import Sequence


class StructuredArgumentParser(argparse.ArgumentParser):
    """Raise structured command errors instead of terminating with prose."""

    def error(self, message: str) -> NoReturn:
        """Convert argparse validation failures into LooprError."""
        raise LooprError(EXIT_PRECONDITION, "input", message)


def parser() -> argparse.ArgumentParser:
    """Build the stable skill command parser."""
    root = StructuredArgumentParser(
        description="Review or submit one exact GitHub pull-request head."
    )
    subcommands = root.add_subparsers(dest="command", required=True)
    review = subcommands.add_parser(
        "review",
        help="review and post one exact pull-request snapshot",
    )
    review.add_argument(
        "--pr",
        required=True,
        help="positive PR number or canonical GitHub pull URL",
    )
    review.add_argument(
        "--repo-dir",
        default=".",
        help="local checkout used for immutable Git object reads",
    )
    review.add_argument(
        "--artifacts-dir",
        default=".pr-review-loop",
        help="private artifact directory",
    )
    review.add_argument(
        "--oracle-thinking-time",
        choices=("light", "standard", "extended", "heavy"),
        default="heavy",
    )

    submit = subcommands.add_parser(
        "submit",
        help="validate, commit, and lease-protect a workspace patch",
    )
    submit.add_argument(
        "--pr",
        required=True,
        help="positive PR number or canonical GitHub pull URL",
    )
    submit.add_argument(
        "--expected-head",
        required=True,
        help="full PR head SHA on which the workspace is based",
    )
    submit.add_argument(
        "--repo-dir",
        default=".",
        help="local checkout containing the host-agent patch",
    )
    submit.add_argument(
        "--artifacts-dir",
        default=".pr-review-loop",
        help="private artifact directory",
    )
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one command and emit exactly one JSON object on stdout."""
    runner = CommandRunner()
    command = _requested_command(argv)
    try:
        args = parser().parse_args(argv)
        command = args.command
        if command == "review":
            result = execute_review(
                pr_value=args.pr,
                repo_dir=Path(args.repo_dir),
                artifacts_dir=Path(args.artifacts_dir),
                thinking_time=args.oracle_thinking_time,
                runner=runner,
            )
        else:
            result = execute_submit(
                pr_value=args.pr,
                expected_head=args.expected_head,
                repo_dir=Path(args.repo_dir),
                artifacts_dir=Path(args.artifacts_dir),
                runner=runner,
            )
        _emit(result.as_json())
        return 0
    except LooprError as exc:
        message = runner.redact(str(exc))
        _emit_error(command, exc.category, message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return exc.code
    except KeyboardInterrupt:
        message = "interrupted; failed closed"
        _emit_error(command, "interrupted", message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return EXIT_PRECONDITION
    except Exception as exc:
        message = runner.redact(f"{type(exc).__name__}: {exc}")
        _emit_error(command, "internal", message)
        sys.stderr.write(f"pr-review-loop {command}: {message}\n")
        return EXIT_PRECONDITION


def _requested_command(argv: Sequence[str] | None) -> str:
    """Return a bounded command label before argparse validation runs."""
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {"review", "submit"}:
        return values[0]
    return "unknown"


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
