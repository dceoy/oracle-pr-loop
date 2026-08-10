"""Deterministic, lease-protected pull-request submission."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .github import SHA_RE, GitHubClient
from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    PullRequestIdentity,
    ReviewLoopError,
    SubmitResult,
)
from .process import CommandError, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_PATCH_BYTES = 20 * 1024 * 1024
MAX_STAGED_CONTENT_BYTES = MAX_PATCH_BYTES
MAX_REMOTE_OUTPUT = 1024 * 1024
MAX_GITLINK_DIFF_BYTES = 1024 * 1024
GITLINK_MODE = b"160000"
POLL_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 2
COMMIT_PARENTS_FIELD_COUNT = 2
RAW_DIFF_HEADER_FIELD_COUNT = 5
RAW_DIFF_MODE_LENGTH = 6
RAW_RENAME_PATH_COUNT = 2
RAW_SCORE_DIGITS = 3
RAW_SCORE_MAX = 100
REMOTE_REF_FIELD_COUNT = 2
COMMIT_MESSAGE = "apply reviewed changes"
NULL_SHA = "0" * 40
RAW_STATUSES = frozenset({b"A", b"B", b"C", b"D", b"M", b"R", b"T", b"U", b"X"})
RAW_RENAME_STATUSES = frozenset({b"C", b"R"})
RAW_SCORED_STATUSES = frozenset({b"C", b"M", b"R"})
STAGED_STATUSES = frozenset({b"A", b"C", b"M", b"R", b"T"})
GITLINK_STATUSES = frozenset({b"A", b"C", b"D", b"M", b"R", b"T"})


class _RawDiffRecord(tuple):
    """The validated metadata needed by submit's raw-diff callers."""

    __slots__ = ()

    def __new__(
        cls,
        old_mode: bytes,
        new_mode: bytes,
        new_object_id: str | None,
    ) -> _RawDiffRecord:
        return tuple.__new__(cls, (old_mode, new_mode, new_object_id))

    @property
    def old_mode(self) -> bytes:
        """Return the validated old mode."""
        return self[0]

    @property
    def new_mode(self) -> bytes:
        """Return the validated new mode."""
        return self[1]

    @property
    def new_object_id(self) -> str | None:
        """Return the validated new object ID."""
        return self[2]


def _raw_status(
    value: bytes,
    *,
    accepted_statuses: frozenset[bytes],
) -> bytes:
    """Validate one raw status and its optional similarity score.

    Returns:
        The validated status byte.

    Raises:
        ValueError: The status or similarity score is malformed or disallowed.
    """
    status = value[:1]
    if status not in RAW_STATUSES:
        raise ValueError
    score = value[1:]
    if not score and status in RAW_RENAME_STATUSES:
        raise ValueError
    if score and (
        status not in RAW_SCORED_STATUSES
        or not score.isdigit()
        or len(score) > RAW_SCORE_DIGITS
        or int(score) > RAW_SCORE_MAX
    ):
        raise ValueError
    if status not in accepted_statuses:
        raise ValueError
    return status


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Validate, commit, and lease-protect the complete workspace patch.

    Returns:
        The result of the submitted review patch.

    Raises:
        ReviewLoopError: The workspace, pull request, or remote state is invalid.
    """
    command_runner = runner or CommandRunner()
    if SHA_RE.fullmatch(expected_head) is None:
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "sha",
            "--expected-head must be a full lowercase commit SHA",
        )

    github = GitHubClient(command_runner, repo_dir)
    github.initialize_for_submit(pr_value)
    initial = github.identity_snapshot()
    if initial.head_sha != expected_head:
        raise ReviewLoopError(
            EXIT_RACE,
            "stale_head",
            "remote pull request head does not match --expected-head",
        )

    _validate_local_workspace(command_runner, github.repo_dir, expected_head)
    _require_same_snapshot(initial, github.identity_snapshot(), phase="before staging")

    _git(command_runner, github.repo_dir, ["add", "--all", "--", "."])
    _git(command_runner, github.repo_dir, ["diff", "--cached", "--check", "--"])

    staged_patch = _git(
        command_runner,
        github.repo_dir,
        ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--"],
        max_output=MAX_PATCH_BYTES,
    )
    if not staged_patch:
        raise ReviewLoopError(EXIT_PRECONDITION, "empty_patch", "staged patch is empty")
    if command_runner.contains_secret(staged_patch):
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "credentials",
            "staged patch metadata contains a known credential value",
        )
    _require_no_known_credentials_in_staged_blobs(command_runner, github)
    _require_same_snapshot(initial, github.identity_snapshot(), phase="before commit")

    _require_no_merge_state(command_runner, github.repo_dir)
    _git(
        command_runner,
        github.repo_dir,
        [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            COMMIT_MESSAGE,
        ],
    )
    commit_sha = _require_single_child_commit(github, expected_head)

    _require_same_snapshot(initial, github.identity_snapshot(), phase="before push")
    remote_head = _remote_head(command_runner, github.repo_dir, initial.head_ref)
    if remote_head != expected_head:
        raise ReviewLoopError(
            EXIT_RACE, "lease_lost", "remote branch head changed before push"
        )
    _reject_gitlink_changes(github, commit_sha)

    ref = f"refs/heads/{initial.head_ref}"
    push_args = [
        "git",
        "push",
        "--no-verify",
        "--recurse-submodules=no",
        f"--force-with-lease={ref}:{expected_head}",
        "origin",
        f"{commit_sha}:{ref}",
    ]
    try:
        command_runner.run(
            push_args,
            cwd=github.repo_dir,
            env=_push_env(command_runner),
        )
    except CommandError as exc:
        _recover_from_push_failure(
            command_runner,
            repo_dir=github.repo_dir,
            ref=ref,
            head_ref=initial.head_ref,
            commit_sha=commit_sha,
            expected_head=expected_head,
            push_error=exc,
        )

    resulting = _poll_result(github, initial, commit_sha)
    return SubmitResult(
        repository=initial.repository,
        pr_number=initial.number,
        base_sha=initial.base_sha,
        previous_head_sha=expected_head,
        resulting_head_sha=resulting.head_sha,
        commit_sha=commit_sha,
        pushed_branch=initial.head_ref,
    )


def _validate_local_workspace(
    runner: CommandRunner, repo_dir: Path, expected_head: str
) -> None:
    head = _git_text(runner, repo_dir, ["rev-parse", "HEAD"]).strip()
    if head != expected_head:
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "stale_workspace",
            "local HEAD must equal --expected-head",
        )
    _git(runner, repo_dir, ["cat-file", "-e", f"{expected_head}^{{commit}}"])
    if _git(runner, repo_dir, ["ls-files", "-u", "-z", "--"]):
        raise ReviewLoopError(
            EXIT_PRECONDITION, "conflict", "workspace contains unresolved conflicts"
        )
    status = _git(
        runner,
        repo_dir,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if not status:
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "empty_patch",
            "workspace has no implementation changes",
        )
    _git(runner, repo_dir, ["diff", "--check", "HEAD", "--"])


def _require_no_merge_state(runner: CommandRunner, repo_dir: Path) -> None:
    """Reject a resolved or unresolved in-progress merge before commit.

    Raises:
        ReviewLoopError: Git cannot be queried or a merge is in progress.
    """
    try:
        result = runner.run(
            ["git", "rev-parse", "--git-path", "MERGE_HEAD"],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
        value = result.stdout.decode("utf-8", "strict").strip()
    except (CommandError, UnicodeError) as exc:
        raise ReviewLoopError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if not value:
        raise ReviewLoopError(
            EXIT_PRECONDITION, "git", "Git returned an empty MERGE_HEAD path"
        )
    merge_head = Path(value)
    if not merge_head.is_absolute():
        merge_head = repo_dir / merge_head
    if merge_head.is_file():
        raise ReviewLoopError(
            EXIT_PRECONDITION, "conflict", "workspace contains an in-progress merge"
        )


def _require_single_child_commit(github: GitHubClient, expected_head: str) -> str:
    """Require the new HEAD to be one hook-free commit on expected_head.

    Returns:
        The newly created commit SHA.

    Raises:
        ReviewLoopError: The new commit is not a single child of expected_head.
    """
    fields = github.git_text(
        ["rev-list", "--parents", "-n", "1", "HEAD"], max_output=MAX_REMOTE_OUTPUT
    ).split()
    if (
        len(fields) != COMMIT_PARENTS_FIELD_COUNT
        or SHA_RE.fullmatch(fields[0]) is None
        or fields[1] != expected_head
    ):
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "commit",
            "created commit is not a single child of the expected head",
        )
    return fields[0]


def _require_no_known_credentials_in_staged_blobs(
    runner: CommandRunner, github: GitHubClient
) -> None:
    """Scan bounded staged path metadata and blob contents for credentials.

    Raises:
        ReviewLoopError: Staged metadata or blob content is unsafe or malformed.
    """
    raw = _git(
        runner,
        github.repo_dir,
        [
            "diff",
            "--cached",
            "--raw",
            "--no-abbrev",
            "-z",
            "--diff-filter=ACMRT",
            "--",
        ],
        max_output=MAX_PATCH_BYTES,
    )
    if runner.contains_secret(raw):
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "credentials",
            "staged path metadata contains a known credential value",
        )
    scanned_bytes = 0
    for object_id in _staged_object_ids(raw):
        object_type = github.git_text(
            ["cat-file", "-t", object_id], max_output=MAX_REMOTE_OUTPUT
        ).strip()
        if object_type != "blob":
            continue
        size_text = github.git_text(
            ["cat-file", "-s", object_id], max_output=MAX_REMOTE_OUTPUT
        ).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ReviewLoopError(
                EXIT_PRECONDITION, "git", "Git returned an invalid staged blob size"
            ) from exc
        if (
            size < 0
            or size > MAX_STAGED_CONTENT_BYTES
            or scanned_bytes + size > MAX_STAGED_CONTENT_BYTES
        ):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "credentials",
                "staged blob content exceeds the credential scan bound",
            )
        content = github.git_bytes(
            ["cat-file", "blob", object_id], max_output=max(1, size)
        )
        if len(content) != size:
            raise ReviewLoopError(
                EXIT_PRECONDITION, "git", "Git returned a truncated staged blob"
            )
        scanned_bytes += size
        if runner.contains_secret(content):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "credentials",
                "staged blob contains a known credential value",
            )


def _raw_object_id(value: bytes) -> str | None:
    """Decode one raw-diff object ID, allowing Git's null object ID.

    Returns:
        The validated object ID, or None for Git's null object ID.

    Raises:
        ValueError: The object ID is not valid ASCII or a SHA.
    """
    try:
        object_id = value.decode("ascii", "strict")
    except UnicodeError as exc:
        raise ValueError from exc
    if object_id == NULL_SHA:
        return None
    if SHA_RE.fullmatch(object_id) is None:
        raise ValueError
    return object_id


def _parse_raw_diff(
    raw: bytes,
    *,
    accepted_statuses: frozenset[bytes],
) -> tuple[_RawDiffRecord, ...]:
    """Parse one NUL-delimited Git raw diff with a caller's status policy.

    Returns:
        The validated raw-diff records.

    Raises:
        ValueError: The raw diff contains malformed metadata or paths.
    """
    if not raw:
        return ()
    fields = raw.split(b"\0")
    if fields[-1]:
        raise ValueError
    fields.pop()
    records: list[_RawDiffRecord] = []
    index = 0
    while index < len(fields):
        parts = fields[index].split()
        index += 1
        if len(parts) != RAW_DIFF_HEADER_FIELD_COUNT or not parts[0].startswith(b":"):
            raise ValueError

        old_mode = parts[0][1:]
        new_mode = parts[1]
        if any(
            len(mode) != RAW_DIFF_MODE_LENGTH
            or any(char not in b"01234567" for char in mode)
            for mode in (old_mode, new_mode)
        ):
            raise ValueError
        _raw_object_id(parts[2])
        new_object_id = _raw_object_id(parts[3])

        status = _raw_status(parts[4], accepted_statuses=accepted_statuses)
        path_count = RAW_RENAME_PATH_COUNT if status in RAW_RENAME_STATUSES else 1
        if index + path_count > len(fields) or any(
            not path for path in fields[index : index + path_count]
        ):
            raise ValueError
        index += path_count
        records.append(_RawDiffRecord(old_mode, new_mode, new_object_id))
    return tuple(records)


def _staged_object_ids(raw: bytes) -> list[str]:
    """Return distinct new object IDs from staged raw diff records.

    Raises:
        ReviewLoopError: Git returned malformed or unsafe staged metadata.
    """
    try:
        records = _parse_raw_diff(raw, accepted_statuses=STAGED_STATUSES)
    except ValueError as exc:
        raise ReviewLoopError(
            EXIT_PRECONDITION, "git", "Git returned malformed staged diff metadata"
        ) from exc

    object_ids: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.new_mode == GITLINK_MODE:
            continue
        if record.new_object_id is None:
            raise ReviewLoopError(
                EXIT_PRECONDITION, "git", "Git returned an invalid staged object ID"
            )
        if record.new_object_id not in seen:
            seen.add(record.new_object_id)
            object_ids.append(record.new_object_id)
    return object_ids


def _reject_gitlink_changes(github: GitHubClient, commit_sha: str) -> None:
    """Fail closed when the exact candidate commit changes a gitlink.

    Raises:
        ReviewLoopError: The diff cannot be validated or changes a gitlink.
    """
    try:
        raw = github.git_bytes(
            [
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
            max_output=MAX_GITLINK_DIFF_BYTES,
        )
    except ReviewLoopError as exc:
        raise ReviewLoopError(EXIT_PRECONDITION, "submodule", str(exc)) from exc
    if _contains_gitlink_change(raw):
        raise ReviewLoopError(
            EXIT_PRECONDITION, "submodule", "submit does not support gitlink changes"
        )


def _contains_gitlink_change(raw: bytes) -> bool:
    """Return whether a bounded raw diff changes a gitlink mode.

    Raises:
        ReviewLoopError: The raw diff metadata is malformed.
    """
    try:
        records = _parse_raw_diff(raw, accepted_statuses=GITLINK_STATUSES)
    except ValueError as exc:
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "submodule",
            "Git returned malformed commit diff metadata",
        ) from exc
    return any(GITLINK_MODE in {record.old_mode, record.new_mode} for record in records)


def _require_same_snapshot(
    initial: PullRequestIdentity,
    current: PullRequestIdentity,
    *,
    phase: str,
) -> None:
    """Reject a pre-write PR identity, SHA, or ref-name change.

    Raises:
        ReviewLoopError: The pull request changed during the submit phase.
    """
    if (
        current.base_sha != initial.base_sha
        or current.head_sha != initial.head_sha
        or current.base_ref != initial.base_ref
        or current.head_ref != initial.head_ref
    ):
        raise ReviewLoopError(
            EXIT_RACE,
            "stale_state",
            f"pull request base or head changed {phase}",
        )


def _poll_result(
    github: GitHubClient, initial: PullRequestIdentity, commit_sha: str
) -> PullRequestIdentity:
    """Wait until GitHub exposes the pushed commit as the PR head.

    Returns:
        The refreshed pull-request identity.

    Raises:
        ReviewLoopError: The remote state changes unexpectedly or times out.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        try:
            current = github.identity_snapshot(require_open=False)
        except ReviewLoopError as exc:
            if exc.category != "github" or time.monotonic() >= deadline:
                raise
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if current.head_sha == commit_sha:
            return current
        if current.head_sha != initial.head_sha:
            raise ReviewLoopError(
                EXIT_RACE, "stale_state", "pull request head changed after push"
            )
        if time.monotonic() >= deadline:
            raise ReviewLoopError(
                EXIT_GITHUB,
                "github",
                "GitHub did not expose the pushed head before timeout",
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _remote_head(runner: CommandRunner, repo_dir: Path, ref: str) -> str:
    output = _git_text(
        runner, repo_dir, ["ls-remote", "--refs", "origin", f"refs/heads/{ref}"]
    )
    sha = _parse_remote_ref(output, f"refs/heads/{ref}")
    if sha is None:
        raise ReviewLoopError(
            EXIT_GITHUB,
            "remote_ref",
            "remote pull-request branch was missing or malformed",
        )
    return sha


def _parse_remote_ref(output: str, expected_ref: str) -> str | None:
    """Parse exactly one validated ``git ls-remote --refs`` record.

    Returns:
        The validated remote SHA, or None when the output is invalid.
    """
    lines = output.splitlines()
    if len(lines) != 1:
        return None
    fields = lines[0].split("\t")
    if len(fields) != REMOTE_REF_FIELD_COUNT or fields[1] != expected_ref:
        return None
    if SHA_RE.fullmatch(fields[0]) is None:
        return None
    return fields[0]


def _push_env(runner: CommandRunner) -> dict[str, str]:
    """Return the base environment with implicit follow-tags disabled."""
    env = runner.base_env()
    parameters = " ".join(
        value.strip()
        for key, value in env.items()
        if key.upper() == "GIT_CONFIG_PARAMETERS" and value.strip()
    )
    env = {
        key: value
        for key, value in env.items()
        if key.upper() != "GIT_CONFIG_PARAMETERS"
    }
    override = "'push.followTags=false'"
    env["GIT_CONFIG_PARAMETERS"] = (
        f"{parameters} {override}" if parameters else override
    )
    return env


def _remote_matches(
    runner: CommandRunner,
    *,
    repo_dir: Path,
    remote: str,
    ref: str,
    commit_sha: str,
    expected_head: str,
) -> bool | None:
    """Read one exact remote ref state during push-failure recovery.

    Returns:
        True for the candidate commit, False for another commit, or None when
        the remote state cannot yet be determined.
    """
    try:
        result = runner.run(
            ["git", "ls-remote", "--refs", remote, ref],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=MAX_REMOTE_OUTPUT,
        )
        output = result.stdout.decode("utf-8", "strict")
    except (CommandError, UnicodeError):
        return None
    sha = _parse_remote_ref(output, ref)
    if sha is None:
        return None
    if sha == commit_sha:
        return True
    if sha == expected_head:
        return None
    return False


def _poll_remote_confirmation(
    runner: CommandRunner,
    *,
    repo_dir: Path,
    remote: str,
    ref: str,
    commit_sha: str,
    expected_head: str,
) -> bool:
    """Poll bounded remote reads for confirmation of an ambiguous push.

    Returns:
        Whether the remote confirms the candidate commit.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        matches = _remote_matches(
            runner,
            repo_dir=repo_dir,
            remote=remote,
            ref=ref,
            commit_sha=commit_sha,
            expected_head=expected_head,
        )
        if matches is not None:
            return matches
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


def _recover_from_push_failure(
    runner: CommandRunner,
    *,
    repo_dir: Path,
    ref: str,
    head_ref: str,
    commit_sha: str,
    expected_head: str,
    push_error: CommandError,
) -> None:
    """Recover only when the remote already holds this exact candidate commit.

    Raises:
        ReviewLoopError: The push result is ambiguous or the lease was lost.
    """
    if _poll_remote_confirmation(
        runner,
        repo_dir=repo_dir,
        remote="origin",
        ref=ref,
        commit_sha=commit_sha,
        expected_head=expected_head,
    ):
        return
    current_remote = _remote_head(runner, repo_dir, head_ref)
    if current_remote == expected_head:
        raise ReviewLoopError(EXIT_GITHUB, "push", str(push_error)) from push_error
    if current_remote != commit_sha:
        raise ReviewLoopError(
            EXIT_RACE,
            "lease_lost",
            "remote head changed and the lease-protected push was rejected",
        ) from push_error


def _git(
    runner: CommandRunner,
    repo_dir: Path,
    args: Sequence[str],
    *,
    max_output: int = 24 * 1024 * 1024,
) -> bytes:
    try:
        result = runner.run(
            ["git", *args],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=max_output,
        )
    except CommandError as exc:
        raise ReviewLoopError(EXIT_PRECONDITION, "git", str(exc)) from exc
    return result.stdout


def _git_text(runner: CommandRunner, repo_dir: Path, args: Sequence[str]) -> str:
    try:
        return _git(runner, repo_dir, args, max_output=MAX_REMOTE_OUTPUT).decode(
            "utf-8", "strict"
        )
    except UnicodeError as exc:
        raise ReviewLoopError(
            EXIT_PRECONDITION, "git", "Git returned non-UTF-8 output"
        ) from exc
