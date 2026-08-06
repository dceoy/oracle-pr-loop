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
COMMIT_COMMAND = (
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgSign=false",
    "commit",
    "--no-verify",
    "--no-gpg-sign",
    "-m",
    submit_core.COMMIT_MESSAGE,
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
        argv = _constrain_push(self._exclude_artifacts(original_argv))
        env = _constrain_push_env(argv, env)
        self._guard_commit_shape(
            original_argv=original_argv,
            argv=argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        retry_deadline = (
            time.monotonic() + submit_core.POLL_TIMEOUT_SECONDS
            if self._pushed_commit_sha is not None and argv[:3] == ("gh", "pr", "view")
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
                remote, commit_sha, ref, expected_head = target
                remote_confirmed = self._confirm_remote_after_push_error(
                    remote=remote,
                    commit_sha=commit_sha,
                    expected_head=expected_head,
                    ref=ref,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                )
                self._pushed_commit_sha = commit_sha
                if not remote_confirmed:
                    raise
                result = CommandResult(argv, 0, b"", "")
            else:
                target = _push_target(argv)
                if target is not None:
                    self._pushed_commit_sha = target[1]
            break

        if original_argv == STAGED_PATCH_COMMAND and self.contains_secret(
            result.stdout
        ):
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

    def _guard_commit_shape(
        self,
        *,
        original_argv: tuple[str, ...],
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> None:
        """Reject merge state and multi-parent commits at the write boundary."""
        if original_argv == COMMIT_COMMAND:
            self._require_no_merge_state(
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        push_target = _push_target(argv)
        if push_target is not None:
            self._require_single_parent(
                commit_sha=push_target[1],
                cwd=cwd,
                env=env,
                timeout=timeout,
            )

    def _require_no_merge_state(
        self,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> None:
        """Reject a resolved or unresolved in-progress merge before commit."""
        try:
            result = self._delegate.run(
                ["git", "rev-parse", "--git-path", "MERGE_HEAD"],
                cwd=cwd,
                env=env,
                timeout=timeout,
                max_output=MAX_REMOTE_OUTPUT,
            )
            value = result.stdout.decode("utf-8", "strict").strip()
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
        if not value:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned an empty MERGE_HEAD path",
            )
        merge_head = Path(value)
        if not merge_head.is_absolute():
            merge_head = cwd / merge_head
        if merge_head.is_file():
            raise LooprError(
                EXIT_PRECONDITION,
                "conflict",
                "workspace contains an in-progress merge",
            )

    def _require_single_parent(
        self,
        *,
        commit_sha: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> None:
        """Require the exact commit being pushed to have one parent."""
        try:
            result = self._delegate.run(
                ["git", "rev-list", "--parents", "-n", "1", commit_sha],
                cwd=cwd,
                env=env,
                timeout=timeout,
                max_output=MAX_REMOTE_OUTPUT,
            )
            fields = result.stdout.decode("ascii", "strict").split()
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_PRECONDITION, "commit", str(exc)) from exc
        if len(fields) != 2 or fields[0] != commit_sha:
            raise LooprError(
                EXIT_PRECONDITION,
                "commit",
                "created commit must have exactly one parent",
            )

    def _confirm_remote_after_push_error(
        self,
        *,
        remote: str,
        commit_sha: str,
        expected_head: str,
        ref: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> bool:
        """Retry expected or inconclusive remote reads after a push error."""
        deadline = time.monotonic() + submit_core.POLL_TIMEOUT_SECONDS
        while True:
            matches = self._remote_matches(
                remote=remote,
                commit_sha=commit_sha,
                expected_head=expected_head,
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
        expected_head: str,
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
        lines = output.splitlines()
        if len(lines) != 1:
            return None
        remote_sha, separator, remote_ref = lines[0].partition("\t")
        if separator != "\t" or remote_ref != ref or not _is_sha(remote_sha):
            return None
        if remote_sha == commit_sha:
            return True
        if remote_sha == expected_head:
            return None
        return False


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
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned an empty repository root",
        )
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
        index = runner.run(
            ["git", "ls-files", "-z", "--", artifact_pathspec],
            cwd=repo_root,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
        head = runner.run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                "HEAD",
                "--",
                artifact_pathspec,
            ],
            cwd=repo_root,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
    except CommandError as exc:
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if index.stdout or head.stdout:
        raise LooprError(
            EXIT_PRECONDITION,
            "artifacts",
            "artifact directory must not contain tracked content",
        )


def _constrain_push(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Disable configuration-driven recursive writes for every submit push."""
    if argv[:2] != ("git", "push"):
        return argv
    return (*argv[:2], "--recurse-submodules=no", *argv[2:])


def _constrain_push_env(
    argv: tuple[str, ...],
    env: Mapping[str, str],
) -> Mapping[str, str]:
    """Override command-scope Git config so follow-tags cannot add refs."""
    if argv[:2] != ("git", "push"):
        return env
    constrained = {
        key: value
        for key, value in env.items()
        if key != "GIT_CONFIG_COUNT"
        and key != "GIT_CONFIG_PARAMETERS"
        and not key.startswith("GIT_CONFIG_KEY_")
        and not key.startswith("GIT_CONFIG_VALUE_")
    }
    constrained.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "push.followTags",
            "GIT_CONFIG_VALUE_0": "false",
        }
    )
    return constrained


def _push_target(argv: tuple[str, ...]) -> tuple[str, str, str, str] | None:
    if len(argv) < 5 or argv[:2] != ("git", "push"):
        return None
    remote = argv[-2]
    source, separator, destination = argv[-1].partition(":")
    leases = [
        value.removeprefix("--force-with-lease=")
        for value in argv[2:-2]
        if value.startswith("--force-with-lease=")
    ]
    if len(leases) != 1:
        return None
    lease_ref, lease_separator, expected_head = leases[0].partition(":")
    if (
        separator != ":"
        or not _is_sha(source)
        or not destination.startswith("refs/heads/")
        or lease_separator != ":"
        or lease_ref != destination
        or not _is_sha(expected_head)
    ):
        return None
    return remote, source, destination, expected_head


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )
