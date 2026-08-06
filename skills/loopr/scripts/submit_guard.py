"""Guard the public submit command against unpublished submodule commits."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .models import EXIT_PRECONDITION, LooprError, SubmitResult
from .process import CommandError, CommandResult, CommandRunner
from .submit import execute_submit as _execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class _SubmoduleCheckingRunner(CommandRunner):
    """Preflight the exact push with recursive submodule writes disabled."""

    def __init__(self, delegate: CommandRunner) -> None:
        """Wrap one command runner without changing its environment state."""
        self._delegate = delegate
        self.source_env = delegate.source_env
        self.secrets = delegate.secrets

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
        """Verify submodule availability before the real single-ref push."""
        argv = tuple(str(value) for value in args)
        if argv[:2] == ("git", "push") and "--recurse-submodules=no" in argv:
            preflight = (
                *argv[:2],
                "--dry-run",
                *(
                    "--recurse-submodules=check"
                    if value == "--recurse-submodules=no"
                    else value
                    for value in argv[2:]
                ),
            )
            try:
                self._delegate.run(
                    preflight,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                    input_text=input_text,
                    check=True,
                    max_output=max_output,
                    watch_path=watch_path,
                )
            except CommandError as exc:
                raise LooprError(
                    EXIT_PRECONDITION,
                    "submodule",
                    str(exc),
                ) from exc
        return self._delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    artifacts_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Execute submit with a non-mutating recursive-submodule preflight."""
    command_runner = runner or CommandRunner()
    return _execute_submit(
        pr_value=pr_value,
        expected_head=expected_head,
        repo_dir=repo_dir,
        artifacts_dir=artifacts_dir,
        runner=_SubmoduleCheckingRunner(command_runner),
    )
