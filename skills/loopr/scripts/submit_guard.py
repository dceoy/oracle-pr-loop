"""Guard the public submit command against unsafe remote writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .models import EXIT_PRECONDITION, EXIT_RACE, LooprError, SubmitResult
from .process import CommandError, CommandResult, CommandRunner
from .submit import execute_submit as _execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

GITLINK_MODE = b"160000"
MAX_GITLINK_DIFF_BYTES = 1024 * 1024
STAGED_RAW_PREFIX = (
    "git",
    "diff",
    "--cached",
    "--raw",
    "--no-abbrev",
    "-z",
    "--diff-filter=ACMRT",
    "--",
)


class _SubmitBoundaryRunner(CommandRunner):
    """Freeze PR refs and reject unsafe candidate metadata before remote writes."""

    def __init__(self, delegate: CommandRunner) -> None:
        """Wrap one command runner without changing its environment state."""
        self._delegate = delegate
        self.source_env = delegate.source_env
        self.secrets = delegate.secrets
        self._initial_refs: tuple[str, str] | None = None
        self._push_started = False

    def redact(self, text: str) -> str:
        """Delegate redaction to the caller-supplied runner."""
        return self._delegate.redact(text)

    def contains_secret(self, value: str | bytes) -> bool:
        """Delegate credential detection to the caller-supplied runner."""
        return self._delegate.contains_secret(value)

    def base_env(self) -> dict[str, str]:
        """Delegate the base environment."""
        return self._delegate.base_env()

    def gh_env(self, reviewer_token: str | None = None) -> dict[str, str]:
        """Delegate the GitHub environment."""
        return self._delegate.gh_env(reviewer_token)

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
        """Freeze PR refs and reject unsafe staged metadata or gitlinks."""
        argv = tuple(str(value) for value in args)
        is_real_push = argv[:2] == ("git", "push") and "--recurse-submodules=no" in argv
        if is_real_push:
            self._reject_gitlink_changes(
                argv=argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            self._push_started = True

        result = self._delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[: len(STAGED_RAW_PREFIX)] == STAGED_RAW_PREFIX and self.contains_secret(
            result.stdout
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "staged path metadata contains a known credential value",
            )
        if not self._push_started and argv[:3] == ("gh", "pr", "view"):
            self._require_stable_pr_refs(result.stdout)
        return result

    def _require_stable_pr_refs(self, output: bytes) -> None:
        """Reject base or head ref rebinding while SHAs remain unchanged."""
        try:
            payload = json.loads(output.decode("utf-8", "strict"))
        except (json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        base_ref = payload.get("baseRefName")
        head_ref = payload.get("headRefName")
        if not isinstance(base_ref, str) or not isinstance(head_ref, str):
            return
        current_refs = (base_ref, head_ref)
        if self._initial_refs is None:
            self._initial_refs = current_refs
            return
        if current_refs != self._initial_refs:
            raise LooprError(
                EXIT_RACE,
                "stale_state",
                "pull request base or head ref changed before push",
            )

    def _reject_gitlink_changes(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> None:
        """Fail closed when the exact candidate commit changes a gitlink."""
        commit_sha = _pushed_commit(argv)
        if commit_sha is None:
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                "could not identify the exact commit selected for push",
            )
        try:
            result = self._delegate.run(
                [
                    "git",
                    "diff-tree",
                    "--no-commit-id",
                    "--raw",
                    "-z",
                    "--no-abbrev",
                    "--no-renames",
                    "-r",
                    f"{commit_sha}^",
                    commit_sha,
                    "--",
                ],
                cwd=cwd,
                env=env,
                timeout=timeout,
                check=True,
                max_output=MAX_GITLINK_DIFF_BYTES,
            )
        except CommandError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                str(exc),
            ) from exc
        if _contains_gitlink_change(result.stdout):
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                "submit does not support gitlink changes",
            )


def _pushed_commit(argv: tuple[str, ...]) -> str | None:
    """Extract the exact source commit from the constrained push refspec."""
    if len(argv) < 4:
        return None
    source, separator, destination = argv[-1].partition(":")
    if (
        separator != ":"
        or not destination.startswith("refs/heads/")
        or len(source) != 40
        or any(character not in "0123456789abcdef" for character in source)
    ):
        return None
    return source


def _contains_gitlink_change(raw: bytes) -> bool:
    """Parse a bounded NUL-delimited raw diff and detect mode 160000."""
    if not raw:
        return False
    fields = raw.split(b"\0")
    if fields[-1]:
        raise LooprError(
            EXIT_PRECONDITION,
            "submodule",
            "Git returned malformed commit diff metadata",
        )
    fields.pop()
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        parts = header.split()
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                "Git returned malformed commit diff metadata",
            )
        status = parts[4][:1]
        path_count = 2 if status in {b"C", b"R"} else 1
        if status not in {b"A", b"C", b"D", b"M", b"R", b"T"}:
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                "Git returned an unexpected commit diff status",
            )
        if index + path_count > len(fields) or any(
            not value for value in fields[index : index + path_count]
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "submodule",
                "Git returned malformed commit diff paths",
            )
        old_mode = parts[0][1:]
        new_mode = parts[1]
        if GITLINK_MODE in {old_mode, new_mode}:
            return True
        index += path_count
    return False


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    artifacts_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Execute submit while freezing refs and rejecting unsafe metadata."""
    command_runner = runner or CommandRunner()
    return _execute_submit(
        pr_value=pr_value,
        expected_head=expected_head,
        repo_dir=repo_dir,
        artifacts_dir=artifacts_dir,
        runner=_SubmitBoundaryRunner(command_runner),
    )
