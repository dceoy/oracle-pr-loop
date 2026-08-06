"""Hardened public entrypoint for deterministic PR submission."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import submit_core
from .models import EXIT_PRECONDITION, LooprError, SubmitResult
from .process import CommandError, CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_REMOTE_OUTPUT = 1024 * 1024
STAGE_COMMAND = ("git", "add", "--all", "--")
WORKSPACE_STATUS_COMMAND = (
    "git",
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
)
WORKSPACE_DIFF_CHECK_COMMAND = ("git", "diff", "--check", "HEAD", "--")
STAGED_DIFF_CHECK_COMMAND = ("git", "diff", "--cached", "--check", "--")
STAGED_PATCH_COMMAND = (
    "git",
    "diff",
    "--cached",
    "--binary",
    "--full-index",
    "--no-ext-diff",
    "--",
)
STAGED_RAW_COMMAND = (
    "git",
    "diff",
    "--cached",
    "--raw",
    "--no-abbrev",
    "-z",
    "--diff-filter=ACMRT",
    "--",
)


class _PushAwareRunner(CommandRunner):
    """Normalize workspace scope and ambiguous post-write state safely."""

    def __init__(
        self,
        delegate: CommandRunner,
        artifact_exclusion: str | None,
    ) -> None:
        """Wrap the caller-supplied runner while preserving its state."""
        self._delegate = delegate
        self.source_env = delegate.source_env
        self.secrets = delegate.secrets
        self._artifact_exclusion = artifact_exclusion
        self._pushed_commit_sha: str | None = None

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
        """Harden workspace staging and post-write confirmation."""
        original_argv = tuple(str(value) for value in args)
        argv = self._exclude_artifacts(original_argv)
        retry_deadline = (
            time.monotonic() + submit_core.POLL_TIMEOUT_SECONDS
            if self._pushed_commit_sha is not None
            and argv[:3] == ("gh", "pr", "view")
            else None
        )
        while True:
            try:
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
            except CommandError:
                if retry_deadline is not None and time.monotonic() < retry_deadline:
                    time.sleep(submit_core.POLL_INTERVAL_SECONDS)
                    continue
                target = _push_target(argv)
                if target is None:
                    raise
                remote, commit_sha, ref = target
                if not self._confirm_remote_after_push_error(
                    remote=remote,
                    commit_sha=commit_sha,
                    ref=ref,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                ):
                    raise
                self._pushed_commit_sha = commit_sha
                result = CommandResult(argv, 0, b"", "")
            else:
                target = _push_target(argv)
                if target is not None:
                    self._pushed_commit_sha = target[1]
            break

        if original_argv == STAGED_PATCH_COMMAND and self.contains_secret(result.stdout):
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "staged patch metadata contains a known credential value",
            )
        return self._normalize_post_push_snapshot(argv, result)

    def _exclude_artifacts(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        """Keep private loopr artifacts outside every workspace pathspec."""
        if self._artifact_exclusion is None:
            return argv
        if argv in {
            STAGE_COMMAND,
            WORKSPACE_DIFF_CHECK_COMMAND,
            STAGED_DIFF_CHECK_COMMAND,
            STAGED_PATCH_COMMAND,
            STAGED_RAW_COMMAND,
        }:
            return (*argv, ".", self._artifact_exclusion)
        if argv == WORKSPACE_STATUS_COMMAND:
            return (*argv, "--", ".", self._artifact_exclusion)
        return argv

    def _confirm_remote_after_push_error(
        self,
        *,
        remote: str,
        commit_sha: str,
        ref: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> bool:
        """Retry only inconclusive remote reads after an ambiguous push error."""
        deadline = time.monotonic() + submit_core.POLL_TIMEOUT_SECONDS
        while True:
            matches = self._remote_matches(
                remote=remote,
                commit_sha=commit_sha,
                ref=ref,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            if matches is not None:
                return matches
            if time.monotonic() >= deadline:
                return False
            time.sleep(submit_core.POLL_INTERVAL_SECONDS)

    def _normalize_post_push_snapshot(
        self,
        argv: tuple[str, ...],
        result: CommandResult,
    ) -> CommandResult:
        """Permit state-only PR changes after the remote write."""
        if self._pushed_commit_sha is None or argv[:3] != ("gh", "pr", "view"):
            return result
        try:
            payload = json.loads(result.stdout.decode("utf-8", "strict"))
        except (json.JSONDecodeError, UnicodeError):
            return result
        if not isinstance(payload, dict):
            return result
        payload["state"] = "OPEN"
        payload["isDraft"] = False
        output = json.dumps(payload, separators=(",", ":")).encode()
        return CommandResult(result.args, result.returncode, output, result.stderr)

    def _remote_matches(
        self,
        *,
        remote: str,
        commit_sha: str,
        ref: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> bool | None:
        try:
            result = self._delegate.run(
                ["git", "ls-remote", "--refs", remote, ref],
                cwd=cwd,
                env=env,
                timeout=timeout,
                max_output=MAX_REMOTE_OUTPUT,
            )
            output = result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError):
            return None
        return output.splitlines() == [f"{commit_sha}\t{ref}"]


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    artifacts_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Validate the push destination, then execute one guarded submission."""
    command_runner = runner or CommandRunner()
    _require_single_push_url(command_runner, repo_dir)
    repo_root = _repo_root(command_runner, repo_dir)
    artifact_exclusion = _artifact_exclusion(repo_root, artifacts_dir)
    if artifact_exclusion is not None:
        _require_artifacts_unstaged(command_runner, repo_root, artifact_exclusion)
    return submit_core.execute_submit(
        pr_value=pr_value,
        expected_head=expected_head,
        repo_dir=repo_root,
        artifacts_dir=artifacts_dir,
        runner=_PushAwareRunner(command_runner, artifact_exclusion),
    )


def _require_single_push_url(runner: CommandRunner, repo_dir: Path) -> None:
    try:
        result = runner.run(
            ["git", "remote", "get-url", "--push", "--all", "origin"],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
        output = result.stdout.decode("utf-8", "strict")
    except (CommandError, UnicodeError) as exc:
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    push_urls = output.splitlines()
    if len(push_urls) != 1 or not push_urls[0]:
        raise LooprError(
            EXIT_PRECONDITION,
            "repository",
            "origin must have exactly one push URL",
        )


def _repo_root(runner: CommandRunner, repo_dir: Path) -> Path:
    try:
        result = runner.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
        root = result.stdout.decode("utf-8", "strict").strip()
    except (CommandError, UnicodeError) as exc:
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if not root:
        raise LooprError(EXIT_PRECONDITION, "git", "Git returned an empty repository root")
    return Path(root).resolve()


def _artifact_exclusion(repo_root: Path, artifacts_dir: Path) -> str | None:
    artifact_root = (
        artifacts_dir if artifacts_dir.is_absolute() else repo_root / artifacts_dir
    ).resolve()
    try:
        relative = artifact_root.relative_to(repo_root)
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] == ".git":
        raise LooprError(
            EXIT_PRECONDITION,
            "artifacts",
            "artifact directory must not be the repository root or Git directory",
        )
    return f":(exclude,top,literal){relative.as_posix()}"


def _require_artifacts_unstaged(
    runner: CommandRunner,
    repo_root: Path,
    artifact_exclusion: str,
) -> None:
    artifact_pathspec = artifact_exclusion.replace(":(exclude,", ":(", 1)
    try:
        result = runner.run(
            ["git", "diff", "--cached", "--name-only", "-z", "--", artifact_pathspec],
            cwd=repo_root,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
    except CommandError as exc:
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if result.stdout:
        raise LooprError(
            EXIT_PRECONDITION,
            "artifacts",
            "artifact directory contains staged changes",
        )


def _push_target(argv: tuple[str, ...]) -> tuple[str, str, str] | None:
    if len(argv) < 5 or argv[:2] != ("git", "push"):
        return None
    remote = argv[-2]
    source, separator, destination = argv[-1].partition(":")
    if (
        separator != ":"
        or len(source) != 40
        or any(character not in "0123456789abcdef" for character in source)
        or not destination.startswith("refs/heads/")
    ):
        return None
    return remote, source, destination
