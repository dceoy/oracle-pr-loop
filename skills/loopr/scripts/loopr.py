"""Vendor-neutral loopr command entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from .models import EXIT_PRECONDITION, JsonObject, LooprError
from .process import CommandRunner
from .review import execute_review

if TYPE_CHECKING:
    from collections.abc import Sequence


class StructuredArgumentParser(argparse.ArgumentParser):
    """Raise structured command errors instead of terminating with prose."""

    def error(self, message: str) -> None:
        """Convert argparse validation failures into LooprError."""
        raise LooprError(EXIT_PRECONDITION, "input", message)


def parser() -> argparse.ArgumentParser:
    """Build the stable loopr command parser."""
    root = StructuredArgumentParser(
        description="Review one exact GitHub pull-request head through Oracle/ChatGPT."
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
        default=".pr-loopr",
        help="private artifact directory",
    )
    review.add_argument(
        "--oracle-thinking-time",
        choices=("light", "standard", "extended", "heavy"),
        default="heavy",
    )
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one command and emit exactly one JSON object on stdout."""
    runner = CommandRunner()
    try:
        args = parser().parse_args(argv)
        result = execute_review(
            pr_value=args.pr,
            repo_dir=Path(args.repo_dir),
            artifacts_dir=Path(args.artifacts_dir),
            thinking_time=args.oracle_thinking_time,
            runner=runner,
        )
        _emit(result.as_json())
        return 0
    except LooprError as exc:
        _emit_error(exc.category, runner.redact(str(exc)))
        sys.stderr.write(f"loopr review: {runner.redact(str(exc))}\n")
        return exc.code
    except KeyboardInterrupt:
        message = "interrupted; failed closed"
        _emit_error("interrupted", message)
        sys.stderr.write(f"loopr review: {message}\n")
        return EXIT_PRECONDITION
    except Exception as exc:
        message = runner.redact(f"{type(exc).__name__}: {exc}")
        _emit_error("internal", message)
        sys.stderr.write(f"loopr review: {message}\n")
        return EXIT_PRECONDITION


def _emit_error(category: str, message: str) -> None:
    """Emit the stable structured failure schema."""
    _emit(
        {
            "schema_version": 1,
            "command": "review",
            "error": {"category": category, "message": message},
        }
    )


def _emit(value: JsonObject) -> None:
    """Write one compact JSON object and one trailing newline."""
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(f"{serialized}\n")


if __name__ == "__main__":
    raise SystemExit(main())
