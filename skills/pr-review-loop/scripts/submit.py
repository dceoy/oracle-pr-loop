"""Deterministic, lease-protected pull-request submission."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .github import (
    SHA_RE,
    GitHubClient,
)
from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    LooprError,
    PullRequestIdentity,
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
REMOTE_REF_LINE_FIELD_COUNT = 2
COMMIT_MESSAGE = "apply reviewed changes"
LEGACY_ARTIFACTS_PATH = ".pr-review-loop"
LEGACY_ARTIFACTS_PATHSPEC = f":(exclude,top){LEGACY_ARTIFACTS_PATH}"


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Validate, commit, and lease-protect the complete workspace patch.

    The flow is linear and locally auditable: resolve and validate the
    repository/PR identity; validate the local workspace and staged
    candidate; create exactly one child commit; revalidate the PR and
    remote lease; push the exact commit with an explicit lease; confirm the
    resulting GitHub PR head.

    Returns:
        The stable submit command result.

    Raises:
        LooprError: The workspace, patch, remote, or PR lease violated a
            fail-closed precondition.
    """
    command_runner = runner or CommandRunner()
    if SHA_RE.fullmatch(expected_head) is None:
        raise LooprError(
            EXIT_PRECONDITION,
            "sha",
            "--expected-head must be a full lowercase commit SHA",
        )

    github = GitHubClient(command_runner, repo_dir)
    github.initialize_for_submit(pr_value)
    initial = github.identity_snapshot()
    if initial.head_sha != expected_head:
        raise LooprError(
            EXIT_RACE,
            "stale_head",
            "remote pull request head does not match --expected-head",
        )

    _validate_local_workspace(command_runner, github.repo_dir, expected_head)
    _require_same_snapshot(
        initial,
        github.identity_snapshot(),
        phase="before staging",
    )

    add_args = ["add", "--all", "--", "."]
    if not _legacy_artifacts_already_ignored(command_runner, github.repo_dir):
        add_args.append(LEGACY_ARTIFACTS_PATHSPEC)
    _git(command_runner, github.repo_dir, add_args)
    _reject_staged_legacy_artifacts(command_runner, github.repo_dir)
    _git(command_runner, github.repo_dir, ["diff", "--cached", "--check", "--"])

    staged_patch = _git(
        command_runner,
        github.repo_dir,
        ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--"],
        max_output=MAX_PATCH_BYTES,
    )
    if not staged_patch:
        raise LooprError(
            EXIT_PRECONDITION,
            "empty_patch",
            "staged patch is empty",
        )
    if command_runner.contains_secret(staged_patch):
        raise LooprError(
            EXIT_PRECONDITION,
            "credentials",
            "staged patch metadata contains a known credential value",
        )
    _require_no_known_credentials_in_staged_blobs(command_runner, github)
    _require_same_snapshot(
        initial,
        github.identity_snapshot(),
        phase="before commit",
    )

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

    _require_same_snapshot(
        initial,
        github.identity_snapshot(),
        phase="before push",
    )
    remote_head = _remote_head(command_runner, github.repo_dir, initial.head_ref)
    if remote_head != expected_head:
        raise LooprError(
            EXIT_RACE,
            "lease_lost",
            "remote branch head changed before push",
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


def _legacy_artifacts_already_ignored(
    runner: CommandRunner,
    repo_dir: Path,
) -> bool:
    """Detect whether an ignore rule already covers the legacy artifacts path.

    Returns:
        Whether `.pr-review-loop` is already excluded by an existing Git
        ignore rule (`.gitignore`, `.git/info/exclude`, or equivalent).

    Raises:
        LooprError: Git could not evaluate the ignore rules.
    """
    try:
        result = runner.run(
            ["git", "check-ignore", "--quiet", "--", LEGACY_ARTIFACTS_PATH],
            cwd=repo_dir,
            env=runner.base_env(),
            check=False,
        )
    except CommandError as exc:
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if result.returncode not in {0, 1}:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "could not evaluate ignore rules for .pr-review-loop",
        )
    return result.returncode == 0


def _reject_staged_legacy_artifacts(
    runner: CommandRunner,
    repo_dir: Path,
) -> None:
    """Fail closed if the reserved legacy `.pr-review-loop` path is indexed.

    Raises:
        LooprError: A `.pr-review-loop` entry is staged or tracked, whether
            pre-staged before submit ran or already committed to Git.
    """
    staged = _git(
        runner,
        repo_dir,
        ["diff", "--cached", "--name-only", "-z", "--", LEGACY_ARTIFACTS_PATH],
    )
    tracked = _git(
        runner,
        repo_dir,
        ["ls-files", "--cached", "-z", "--", LEGACY_ARTIFACTS_PATH],
    )
    if staged or tracked:
        raise LooprError(
            EXIT_PRECONDITION,
            "legacy_artifacts",
            "workspace contains reserved .pr-review-loop runtime content",
        )


def _validate_local_workspace(
    runner: CommandRunner,
    repo_dir: Path,
    expected_head: str,
) -> None:
    head = _git_text(runner, repo_dir, ["rev-parse", "HEAD"]).strip()
    if head != expected_head:
        raise LooprError(
            EXIT_PRECONDITION,
            "stale_workspace",
            "local HEAD must equal --expected-head",
        )
    _git(
        runner,
        repo_dir,
        ["cat-file", "-e", f"{expected_head}^{{commit}}"],
    )
    if _git(runner, repo_dir, ["ls-files", "-u", "-z", "--"]):
        raise LooprError(
            EXIT_PRECONDITION,
            "conflict",
            "workspace contains unresolved conflicts",
        )
    status = _git(
        runner,
        repo_dir,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if not status:
        raise LooprError(
            EXIT_PRECONDITION,
            "empty_patch",
            "workspace has no implementation changes",
        )
    _git(runner, repo_dir, ["diff", "--check", "HEAD", "--"])


def _require_no_merge_state(runner: CommandRunner, repo_dir: Path) -> None:
    """Reject a resolved or unresolved in-progress merge before commit.

    Raises:
        LooprError: The merge state could not be read, or a merge is in progress.
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
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    if not value:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned an empty MERGE_HEAD path",
        )
    merge_head = Path(value)
    if not merge_head.is_absolute():
        merge_head = repo_dir / merge_head
    if merge_head.is_file():
        raise LooprError(
            EXIT_PRECONDITION,
            "conflict",
            "workspace contains an in-progress merge",
        )


def _require_single_child_commit(
    github: GitHubClient,
    expected_head: str,
) -> str:
    """Require the new HEAD to be one hook-free commit on expected_head.

    Returns:
        The created commit's SHA.

    Raises:
        LooprError: The created commit does not have exactly one parent, or
            its parent is not expected_head.
    """
    fields = github.git_text(
        ["rev-list", "--parents", "-n", "1", "HEAD"],
        max_output=MAX_REMOTE_OUTPUT,
    ).split()
    if (
        len(fields) != COMMIT_PARENTS_FIELD_COUNT
        or SHA_RE.fullmatch(fields[0]) is None
        or fields[1] != expected_head
    ):
        raise LooprError(
            EXIT_PRECONDITION,
            "commit",
            "created commit is not a single child of the expected head",
        )
    return fields[0]


def _require_no_known_credentials_in_staged_blobs(
    runner: CommandRunner,
    github: GitHubClient,
) -> None:
    """Scan bounded staged path metadata and blob contents for credentials.

    Raises:
        LooprError: Git returned malformed or oversized blob metadata, or
            staged path metadata or a staged blob contains a known credential
            value.
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
        raise LooprError(
            EXIT_PRECONDITION,
            "credentials",
            "staged path metadata contains a known credential value",
        )
    scanned_bytes = 0
    for object_id in _staged_object_ids(raw):
        object_type = github.git_text(
            ["cat-file", "-t", object_id],
            max_output=MAX_REMOTE_OUTPUT,
        ).strip()
        if object_type != "blob":
            continue
        size_text = github.git_text(
            ["cat-file", "-s", object_id],
            max_output=MAX_REMOTE_OUTPUT,
        ).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned an invalid staged blob size",
            ) from exc
        if (
            size < 0
            or size > MAX_STAGED_CONTENT_BYTES
            or scanned_bytes + size > MAX_STAGED_CONTENT_BYTES
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "staged blob content exceeds the credential scan bound",
            )
        content = github.git_bytes(
            ["cat-file", "blob", object_id],
            max_output=max(1, size),
        )
        if len(content) != size:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned a truncated staged blob",
            )
        scanned_bytes += size
        if runner.contains_secret(content):
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "staged blob contains a known credential value",
            )


def _staged_object_id(parts: list[bytes]) -> str | None:
    """Return the staged blob ID, excluding gitlinks from blob scanning.

    Returns:
        The staged blob's object ID, or None for a gitlink.

    Raises:
        LooprError: Git returned a non-ASCII or invalid staged object ID.
    """
    if parts[1] == GITLINK_MODE:
        return None
    try:
        object_id = parts[3].decode("ascii", "strict")
    except UnicodeError as exc:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned a non-ASCII staged object ID",
        ) from exc
    if SHA_RE.fullmatch(object_id) is None:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned an invalid staged object ID",
        )
    return object_id


def _staged_object_ids(raw: bytes) -> list[str]:
    """Parse new blob object IDs from NUL-delimited staged raw diff records.

    Returns:
        The distinct new (non-gitlink) staged blob object IDs.

    Raises:
        LooprError: `raw` is malformed staged diff metadata.
    """
    if not raw:
        return []
    fields = raw.split(b"\0")
    if fields[-1]:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned malformed staged diff metadata",
        )
    fields.pop()
    object_ids: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        parts = header.split()
        if len(parts) != RAW_DIFF_HEADER_FIELD_COUNT or not parts[0].startswith(b":"):
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned malformed staged diff metadata",
            )
        status = parts[4][:1]
        path_count = 2 if status in {b"C", b"R"} else 1
        if status not in {b"A", b"C", b"M", b"R", b"T"}:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned an unexpected staged diff status",
            )
        if index + path_count > len(fields) or any(
            not value for value in fields[index : index + path_count]
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned malformed staged diff paths",
            )
        index += path_count
        object_id = _staged_object_id(parts)
        if object_id is not None and object_id not in seen:
            seen.add(object_id)
            object_ids.append(object_id)
    return object_ids


def _reject_gitlink_changes(
    github: GitHubClient,
    commit_sha: str,
) -> None:
    """Fail closed when the exact candidate commit changes a gitlink.

    Raises:
        LooprError: The commit's diff could not be read, or it changes a gitlink.
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
    except LooprError as exc:
        raise LooprError(EXIT_PRECONDITION, "submodule", str(exc)) from exc
    if _contains_gitlink_change(raw):
        raise LooprError(
            EXIT_PRECONDITION,
            "submodule",
            "submit does not support gitlink changes",
        )


def _contains_gitlink_change(raw: bytes) -> bool:
    """Parse a bounded NUL-delimited raw diff and detect mode 160000.

    Returns:
        Whether the diff contains a gitlink (submodule) change.

    Raises:
        LooprError: raw is malformed commit diff metadata.
    """
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
        if len(parts) != RAW_DIFF_HEADER_FIELD_COUNT or not parts[0].startswith(b":"):
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


def _require_same_snapshot(
    initial: PullRequestIdentity,
    current: PullRequestIdentity,
    *,
    phase: str,
) -> None:
    """Reject a pre-write PR identity, SHA, or ref-name change.

    Comparing ref names (not just SHAs) rejects a base or head ref
    rebinding that leaves the observed SHAs unchanged.

    Raises:
        LooprError: current's base or head SHA or ref name differs from
            initial's.
    """
    if (
        current.base_sha != initial.base_sha
        or current.head_sha != initial.head_sha
        or current.base_ref != initial.base_ref
        or current.head_ref != initial.head_ref
    ):
        message = f"pull request base or head changed {phase}"
        raise LooprError(EXIT_RACE, "stale_state", message)


def _poll_result(
    github: GitHubClient,
    initial: PullRequestIdentity,
    commit_sha: str,
) -> PullRequestIdentity:
    """Wait until GitHub exposes the pushed commit as the PR head.

    Transient GitHub read failures are retried within the poll budget;
    malformed or schema-invalid responses fail immediately.

    Returns:
        The snapshot containing commit_sha as the PR head.

    Raises:
        LooprError: The remote snapshot is malformed, stale, or unavailable.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        try:
            current = github.identity_snapshot(require_open=False)
        except LooprError as exc:
            if exc.category != "github" or time.monotonic() >= deadline:
                raise
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if current.head_sha == commit_sha:
            return current
        if current.head_sha != initial.head_sha:
            raise LooprError(
                EXIT_RACE,
                "stale_state",
                "pull request head changed after push",
            )
        if time.monotonic() >= deadline:
            raise LooprError(
                EXIT_GITHUB,
                "github",
                "GitHub did not expose the pushed head before timeout",
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _remote_head(runner: CommandRunner, repo_dir: Path, ref: str) -> str:
    output = _git_text(
        runner,
        repo_dir,
        ["ls-remote", "--refs", "origin", f"refs/heads/{ref}"],
    )
    lines = output.splitlines()
    if len(lines) != 1:
        raise LooprError(
            EXIT_GITHUB,
            "remote_ref",
            "remote pull-request branch was missing or ambiguous",
        )
    fields = lines[0].split("\t")
    if len(fields) != REMOTE_REF_LINE_FIELD_COUNT or fields[1] != f"refs/heads/{ref}":
        raise LooprError(
            EXIT_GITHUB,
            "remote_ref",
            "remote pull-request branch response was malformed",
        )
    sha = fields[0]
    if SHA_RE.fullmatch(sha) is None:
        raise LooprError(
            EXIT_GITHUB,
            "remote_ref",
            "remote pull-request branch returned an invalid SHA",
        )
    return sha


def _push_env(runner: CommandRunner) -> dict[str, str]:
    """Return the base environment with follow-tags disabled for one push.

    A highest-precedence `push.followTags=false` override is appended so no
    inherited or repository Git configuration can enable implicit tag
    publication for the guarded push.

    Returns:
        The runner's base environment, extended with the override.
    """
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
        True if the remote already holds commit_sha, False if it holds
        anything else, or None if the remote read was inconclusive (a
        transient error, or the remote still shows the pre-push
        expected_head, so the caller should retry).
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
    lines = output.splitlines()
    if len(lines) != 1:
        return None
    remote_sha, separator, remote_ref = lines[0].partition("\t")
    if separator != "\t" or remote_ref != ref or SHA_RE.fullmatch(remote_sha) is None:
        return None
    if remote_sha == commit_sha:
        return True
    if remote_sha == expected_head:
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
        Whether the remote already shows the exact commit created by this
        submission (True), or does not within the poll budget (False).
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
    """Recover from a failed or ambiguous push report.

    Success is accepted only when the remote already holds the exact commit
    created by this submission. A remote branch that still shows
    expected_head re-raises the original push failure; any other remote
    commit is a lease loss.

    Raises:
        LooprError: The push did not land, or the lease was lost to a
            competing update.
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
        raise LooprError(EXIT_GITHUB, "push", str(push_error)) from push_error
    if current_remote != commit_sha:
        raise LooprError(
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
        raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc
    else:
        return result.stdout


def _git_text(
    runner: CommandRunner,
    repo_dir: Path,
    args: Sequence[str],
) -> str:
    try:
        return _git(
            runner,
            repo_dir,
            args,
            max_output=MAX_REMOTE_OUTPUT,
        ).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned non-UTF-8 output",
        ) from exc
