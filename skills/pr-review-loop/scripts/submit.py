"""Deterministic pull-request submission safety and transport."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import submission
from .models import (
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    LooprError,
    SubmitResult,
)
from .process import CommandError, CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_REMOTE_OUTPUT = 1024 * 1024
MAX_GITLINK_DIFF_BYTES = 1024 * 1024
GITLINK_MODE = b"160000"
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
    submission.COMMIT_MESSAGE,
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
            time.monotonic() + submission.POLL_TIMEOUT_SECONDS
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
                    time.sleep(submission.POLL_INTERVAL_SECONDS)
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
        """Keep private skill artifacts outside every workspace pathspec."""
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
        deadline = time.monotonic() + submission.POLL_TIMEOUT_SECONDS
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
            time.sleep(submission.POLL_INTERVAL_SECONDS)

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
        is_real_push = (
            argv[:2] == ("git", "push") and "--recurse-submodules=no" in argv
        )
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
        if (
            argv[: len(STAGED_RAW_COMMAND)] == STAGED_RAW_COMMAND
            and self.contains_secret(result.stdout)
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
            payload: object = json.loads(output.decode("utf-8", "strict"))
        except (json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            return
        data = cast(JsonObject, payload)
        base_ref = data.get("baseRefName")
        head_ref = data.get("headRefName")
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


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    artifacts_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Validate the push destination, then execute one transport-safe submission."""
    command_runner = runner or CommandRunner()
    _require_single_push_url(command_runner, repo_dir)
    repo_root = _repo_root(command_runner, repo_dir)
    artifact_exclusion = _artifact_exclusion(repo_root, artifacts_dir)
    if artifact_exclusion is not None:
        _require_artifacts_unstaged(command_runner, repo_root, artifact_exclusion)
    return submission.execute_submit(
        pr_value=pr_value,
        expected_head=expected_head,
        repo_dir=repo_root,
        artifacts_dir=artifacts_dir,
        runner=_PushAwareRunner(command_runner, artifact_exclusion),
    )


def execute_guarded(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    artifacts_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Execute submit while freezing refs and rejecting unsafe metadata."""
    command_runner = runner or CommandRunner()
    return execute_submit(
        pr_value=pr_value,
        expected_head=expected_head,
        repo_dir=repo_dir,
        artifacts_dir=artifacts_dir,
        runner=_SubmitBoundaryRunner(command_runner),
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
    """Append a highest-precedence follow-tags override for submit pushes."""
    if argv[:2] != ("git", "push"):
        return env
    parameters = " ".join(
        value.strip()
        for key, value in env.items()
        if key.upper() == "GIT_CONFIG_PARAMETERS" and value.strip()
    )
    constrained = {
        key: value
        for key, value in env.items()
        if key.upper() != "GIT_CONFIG_PARAMETERS"
    }
    override = "'push.followTags=false'"
    constrained["GIT_CONFIG_PARAMETERS"] = (
        f"{parameters} {override}" if parameters else override
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


def _pushed_commit(argv: tuple[str, ...]) -> str | None:
    """Extract the exact source commit from the constrained push refspec."""
    if len(argv) < 4:
        return None
    source, separator, destination = argv[-1].partition(":")
    if (
        separator != ":"
        or not destination.startswith("refs/heads/")
        or not _is_sha(source)
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


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )
