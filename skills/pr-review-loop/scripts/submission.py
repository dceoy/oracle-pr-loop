"""Deterministic validation, commit, and lease-protected PR submission."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .github import normalize_repo, resolve_target, validate_ref
from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    JsonValue,
    LooprError,
    SubmitResult,
)
from .process import CommandError, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
PR_FIELDS = (
    "url,number,state,isDraft,baseRefName,baseRefOid,"
    "headRefName,headRefOid,headRepository,headRepositoryOwner"
)
MAX_PATCH_BYTES = 20 * 1024 * 1024
MAX_STAGED_CONTENT_BYTES = MAX_PATCH_BYTES
GITLINK_MODE = b"160000"
POLL_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 2
RAW_DIFF_HEADER_FIELD_COUNT = 5
REMOTE_REF_LINE_FIELD_COUNT = 2
COMMIT_MESSAGE = "loopr: apply reviewed changes"


@dataclass(frozen=True)
class SubmissionSnapshot:
    """The remote pull-request identity and refs used for one submission."""

    repository: str
    number: int
    url: str
    state: str
    is_draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str
    raw: JsonObject


class SubmitGitHubClient:
    """Read and revalidate a pull request through ordinary GitHub credentials."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        """Initialize an unresolved submission client."""
        self.runner = runner
        self.repo_dir = repo_dir.resolve()
        self.repository = ""
        self.number = 0
        self.url = ""

    def initialize(self, pr_value: str) -> None:
        """Resolve and cross-check the local repository and target PR.

        Raises:
            LooprError: The repository or target PR is ambiguous, invalid,
                or unreachable.
        """
        root = self._git_text(["rev-parse", "--show-toplevel"]).strip()
        self.repo_dir = Path(root).resolve()
        fetch_repo = normalize_repo(self._git_text(["remote", "get-url", "origin"]))
        push_repo = normalize_repo(
            self._git_text(["remote", "get-url", "--push", "origin"])
        )
        if fetch_repo.lower() != push_repo.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin fetch and push URLs must identify the same repository",
            )
        self.repository, self.number, self.url = resolve_target(
            pr_value,
            fetch_repo,
        )
        if self.repository.lower() != fetch_repo.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match pull request repository",
            )

    def _git_text(self, args: Sequence[str]) -> str:
        try:
            result = self.runner.run(
                ["git", *args],
                cwd=self.repo_dir,
                env=self.runner.base_env(),
                max_output=1024 * 1024,
            )
            return result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc

    def _gh_text(self, args: Sequence[str]) -> str:
        try:
            result = self.runner.run(
                ["gh", *args],
                cwd=self.repo_dir,
                env=self.runner.gh_env(),
                max_output=1024 * 1024,
            )
            return result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_GITHUB, "github", str(exc)) from exc

    def snapshot(self) -> SubmissionSnapshot:
        """Return one strictly validated remote PR snapshot."""
        payload = self._gh_text(["pr", "view", self.url, "--json", PR_FIELDS])
        data = _json_object(payload)
        head_repository = _object(
            data.get("headRepository"),
            field="headRepository",
        )
        head_owner = _object(
            data.get("headRepositoryOwner"),
            field="headRepositoryOwner",
        )
        name_with_owner = head_repository.get("nameWithOwner")
        if isinstance(name_with_owner, str) and name_with_owner:
            head_repo = name_with_owner
        else:
            owner = _string(
                head_owner.get("login"),
                field="headRepositoryOwner.login",
            )
            name = _string(
                head_repository.get("name"),
                field="headRepository.name",
            )
            head_repo = f"{owner}/{name}"
        snapshot = SubmissionSnapshot(
            repository=self.repository,
            number=_integer(data.get("number"), field="number"),
            url=_string(data.get("url"), field="url"),
            state=_string(data.get("state"), field="state"),
            is_draft=bool(data.get("isDraft")),
            base_ref=_string(data.get("baseRefName"), field="baseRefName"),
            base_sha=_string(data.get("baseRefOid"), field="baseRefOid"),
            head_ref=_string(data.get("headRefName"), field="headRefName"),
            head_sha=_string(data.get("headRefOid"), field="headRefOid"),
            head_repository=head_repo,
            raw=data,
        )
        self._validate_snapshot(snapshot)
        return snapshot

    def _validate_snapshot(self, snapshot: SubmissionSnapshot) -> None:
        expected_url = self.url.lower()
        actual_url = snapshot.url.rstrip("/").lower()
        if snapshot.number != self.number or actual_url != expected_url:
            raise LooprError(
                EXIT_PRECONDITION,
                "identity",
                "ambiguous pull request identity",
            )
        if snapshot.state != "OPEN" or snapshot.is_draft:
            raise LooprError(
                EXIT_PRECONDITION,
                "state",
                "pull request must be open and non-draft",
            )
        if snapshot.head_repository.lower() != self.repository.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "fork pull requests are not supported",
            )
        if not _is_sha(snapshot.base_sha) or not _is_sha(snapshot.head_sha):
            raise LooprError(
                EXIT_PRECONDITION,
                "sha",
                "invalid base or head SHA",
            )
        validate_ref(snapshot.base_ref)
        validate_ref(snapshot.head_ref)

    def poll_result(
        self,
        initial: SubmissionSnapshot,
        commit_sha: str,
    ) -> SubmissionSnapshot:
        """Wait until GitHub exposes the pushed commit as the PR head.

        Returns:
            The confirmed post-push submission snapshot.

        Raises:
            LooprError: GitHub did not expose the pushed commit as the PR
                head before the poll deadline.
        """
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            current = self.snapshot()
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


def execute_submit(
    *,
    pr_value: str,
    expected_head: str,
    repo_dir: Path,
    runner: CommandRunner | None = None,
) -> SubmitResult:
    """Validate, commit, and lease-protect the complete workspace patch.

    Returns:
        The stable submit command result.

    Raises:
        LooprError: The workspace, patch, or PR lease violated a precondition.
    """
    command_runner = runner or CommandRunner()
    if not _is_sha(expected_head):
        raise LooprError(
            EXIT_PRECONDITION,
            "sha",
            "--expected-head must be a full lowercase commit SHA",
        )

    github = SubmitGitHubClient(command_runner, repo_dir)
    github.initialize(pr_value)
    initial = github.snapshot()
    if initial.head_sha != expected_head:
        raise LooprError(
            EXIT_RACE,
            "stale_head",
            "remote pull request head does not match --expected-head",
        )

    _validate_local_workspace(
        command_runner,
        github.repo_dir,
        expected_head,
    )
    _require_same_snapshot(initial, github.snapshot(), phase="before staging")
    _git(command_runner, github.repo_dir, ["add", "--all", "--"])
    _git(
        command_runner,
        github.repo_dir,
        ["diff", "--cached", "--check", "--"],
    )
    staged_patch = _git(
        command_runner,
        github.repo_dir,
        [
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--",
        ],
        max_output=MAX_PATCH_BYTES,
    )
    if not staged_patch:
        raise LooprError(
            EXIT_PRECONDITION,
            "empty_patch",
            "staged patch is empty",
        )
    _require_no_known_credentials_in_staged_blobs(
        command_runner,
        github.repo_dir,
    )
    _require_same_snapshot(initial, github.snapshot(), phase="before commit")

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
    commit_sha = _git_text(
        command_runner,
        github.repo_dir,
        ["rev-parse", "HEAD"],
    ).strip()
    parent_sha = _git_text(
        command_runner,
        github.repo_dir,
        ["rev-parse", "HEAD^"],
    ).strip()
    if not _is_sha(commit_sha) or parent_sha != expected_head:
        raise LooprError(
            EXIT_PRECONDITION,
            "commit",
            "created commit is not a single child of the expected head",
        )
    _require_same_snapshot(initial, github.snapshot(), phase="before push")
    remote_head = _remote_head(
        command_runner,
        github.repo_dir,
        initial.head_ref,
    )
    if remote_head != expected_head:
        raise LooprError(
            EXIT_RACE,
            "lease_lost",
            "remote branch head changed before push",
        )
    push_args = [
        "push",
        "--no-verify",
        f"--force-with-lease=refs/heads/{initial.head_ref}:{expected_head}",
        "origin",
        f"{commit_sha}:refs/heads/{initial.head_ref}",
    ]
    try:
        _git(
            command_runner,
            github.repo_dir,
            push_args,
            error_code=EXIT_GITHUB,
            category="push",
        )
    except LooprError as exc:
        current_remote = _remote_head(
            command_runner,
            github.repo_dir,
            initial.head_ref,
        )
        if current_remote == expected_head:
            raise
        if current_remote != commit_sha:
            raise LooprError(
                EXIT_RACE,
                "lease_lost",
                "remote head changed and the lease-protected push was rejected",
            ) from exc

    resulting = github.poll_result(initial, commit_sha)
    result = SubmitResult(
        repository=initial.repository,
        pr_number=initial.number,
        base_sha=initial.base_sha,
        previous_head_sha=expected_head,
        resulting_head_sha=resulting.head_sha,
        commit_sha=commit_sha,
        pushed_branch=initial.head_ref,
    )
    return result


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


def _require_no_known_credentials_in_staged_blobs(
    runner: CommandRunner,
    repo_dir: Path,
) -> None:
    """Scan bounded staged blob contents for known credential values.

    Raises:
        LooprError: Git returned malformed or oversized blob metadata, or a
            staged blob contains a known credential value.
    """
    raw = _git(
        runner,
        repo_dir,
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
    scanned_bytes = 0
    for object_id in _staged_object_ids(raw):
        object_type = _git_text(
            runner,
            repo_dir,
            ["cat-file", "-t", object_id],
        ).strip()
        if object_type != "blob":
            continue
        size_text = _git_text(
            runner,
            repo_dir,
            ["cat-file", "-s", object_id],
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
        content = _git(
            runner,
            repo_dir,
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
    if not _is_sha(object_id):
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


def _require_same_snapshot(
    initial: SubmissionSnapshot,
    current: SubmissionSnapshot,
    *,
    phase: str,
) -> None:
    if current.base_sha != initial.base_sha or current.head_sha != initial.head_sha:
        message = f"pull request base or head changed {phase}"
        raise LooprError(EXIT_RACE, "stale_state", message)


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
    if not _is_sha(sha):
        raise LooprError(
            EXIT_GITHUB,
            "remote_ref",
            "remote pull-request branch returned an invalid SHA",
        )
    return sha


def _git(
    runner: CommandRunner,
    repo_dir: Path,
    args: Sequence[str],
    *,
    max_output: int = 24 * 1024 * 1024,
    error_code: int = EXIT_PRECONDITION,
    category: str = "git",
) -> bytes:
    try:
        result = runner.run(
            ["git", *args],
            cwd=repo_dir,
            env=runner.base_env(),
            max_output=max_output,
        )
    except CommandError as exc:
        raise LooprError(error_code, category, str(exc)) from exc
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
            max_output=1024 * 1024,
        ).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise LooprError(
            EXIT_PRECONDITION,
            "git",
            "Git returned non-UTF-8 output",
        ) from exc


def _json_object(text: str) -> JsonObject:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LooprError(
            EXIT_GITHUB,
            "github_schema",
            "GitHub returned malformed JSON",
        ) from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LooprError(
            EXIT_GITHUB,
            "github_schema",
            "GitHub returned a non-object JSON response",
        )
    return cast("JsonObject", value)


def _object(value: JsonValue | None, *, field: str) -> JsonObject:
    if not isinstance(value, dict):
        message = f"GitHub field {field} must be an object"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _string(value: JsonValue | None, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        message = f"GitHub field {field} must be a string"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _integer(value: JsonValue | None, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"GitHub field {field} must be an integer"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _is_sha(value: str) -> bool:
    return SHA_RE.fullmatch(value) is not None
