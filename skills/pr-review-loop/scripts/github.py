"""GitHub and immutable Git snapshot access."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    JsonValue,
    LooprError,
    PullRequest,
)
from .process import CommandError

if TYPE_CHECKING:
    from .process import CommandRunner

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
PART_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
OWNER_REPO_PART_COUNT = 2
PR_URL_PART_COUNT = 4
LS_TREE_FIELD_COUNT = 4
MIN_PRINTABLE_CODEPOINT = 32
DEL_CODEPOINT = 127
PR_FIELDS = (
    "url,number,title,body,author,state,isDraft,baseRefName,baseRefOid,"
    "headRefName,headRefOid,headRepository,headRepositoryOwner,files,changedFiles"
)


def normalize_repo(remote: str) -> str:
    """Normalize one unambiguous GitHub.com repository remote.

    Returns:
        The `owner/name` repository identifier.

    Raises:
        LooprError: remote is not an unambiguous github.com repository URL.
    """
    value = remote.strip()
    match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", value)
    if match is not None:
        owner, name = match.groups()
    else:
        parsed = urllib.parse.urlparse(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must be an unambiguous github.com URL",
            )
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != OWNER_REPO_PART_COUNT:
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must identify exactly one repository",
            )
        owner, name = parts
        name = name.removesuffix(".git")
    if not PART_RE.fullmatch(owner) or not PART_RE.fullmatch(name):
        raise LooprError(
            EXIT_PRECONDITION,
            "repository",
            "invalid repository name",
        )
    return f"{owner}/{name}"


def resolve_target(value: str, origin_repo: str | None) -> tuple[str, int, str]:
    """Resolve a positive PR number or canonical GitHub pull URL.

    Returns:
        The repository, PR number, and canonical pull URL.

    Raises:
        LooprError: value is not a positive number or canonical GitHub pull
            URL, a numeric value is given without origin_repo, or the
            resolved PR number is not positive.
    """
    if value.isdecimal():
        if origin_repo is None:
            raise LooprError(
                EXIT_PRECONDITION,
                "input",
                "numeric --pr requires an unambiguous local origin",
            )
        repository, number = origin_repo, int(value)
    else:
        parsed = urllib.parse.urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != PR_URL_PART_COUNT
            or parts[2] != "pull"
            or not parts[3].isdecimal()
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "input",
                "--pr must be a positive number or canonical GitHub pull URL",
            )
        repository = f"{parts[0]}/{parts[1]}"
        number = int(parts[3])
    if number <= 0:
        raise LooprError(
            EXIT_PRECONDITION,
            "input",
            "pull request number must be positive",
        )
    url = f"https://github.com/{repository}/pull/{number}"
    return repository, number, url


def validate_ref(ref: str) -> None:
    """Reject Git refs that can alter command interpretation or traversal.

    Raises:
        LooprError: ref is unsafe.
    """
    forbidden = any(
        ord(character) < MIN_PRINTABLE_CODEPOINT
        or ord(character) == DEL_CODEPOINT
        or character in " ~^:?*[\\"
        for character in ref
    )
    if (
        not ref
        or ref.startswith(("-", "."))
        or ref.endswith((".", "/", ".lock"))
        or ".." in ref
        or "@{" in ref
        or forbidden
    ):
        raise LooprError(EXIT_PRECONDITION, "ref", "unsafe Git ref")


def validate_path(path: str) -> str:
    """Reject changed paths that can escape the immutable Git snapshot.

    Returns:
        path, unchanged.

    Raises:
        LooprError: path is unsafe.
    """
    if (
        not path
        or "\\" in path
        or "\0" in path
        or any(
            ord(character) < MIN_PRINTABLE_CODEPOINT or ord(character) == DEL_CODEPOINT
            for character in path
        )
    ):
        raise LooprError(EXIT_PRECONDITION, "path", "invalid changed path")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or any(part.casefold() == ".git" for part in pure.parts)
    ):
        message = f"unsafe changed path: {path}"
        raise LooprError(EXIT_PRECONDITION, "path", message)
    return path


def _json_object(text: str, *, category: str) -> JsonObject:
    """Decode exactly one JSON object without repairing malformed data.

    Returns:
        The decoded JSON object.

    Raises:
        LooprError: text is not exactly one JSON object.
    """
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LooprError(
            EXIT_GITHUB,
            category,
            "GitHub returned malformed JSON",
        ) from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LooprError(
            EXIT_GITHUB,
            category,
            "GitHub returned a non-object JSON response",
        )
    return cast("JsonObject", value)


def _object(value: JsonValue | None, *, field: str) -> JsonObject:
    """Require a JSON object field.

    Returns:
        value.

    Raises:
        LooprError: value is not an object.
    """
    if not isinstance(value, dict):
        message = f"GitHub field {field} must be an object"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _string(value: JsonValue | None, *, field: str, allow_empty: bool = False) -> str:
    """Require a JSON string field.

    Returns:
        value.

    Raises:
        LooprError: value is not a string, or is empty and allow_empty is false.
    """
    if not isinstance(value, str) or (not allow_empty and not value):
        message = f"GitHub field {field} must be a string"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _integer(value: JsonValue | None, *, field: str) -> int:
    """Require a non-Boolean JSON integer field.

    Returns:
        value.

    Raises:
        LooprError: value is not a non-Boolean integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"GitHub field {field} must be an integer"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


class GitHubClient:
    """Read PR snapshots and post reviews through trusted CLI commands."""

    def __init__(
        self,
        runner: CommandRunner,
        repo_dir: Path,
        reviewer_token: str,
    ) -> None:
        """Initialize an unresolved GitHub client."""
        self.runner = runner
        self.repo_dir = repo_dir.resolve()
        self.reviewer_token = reviewer_token
        self.repository = ""
        self.number = 0
        self.url = ""
        self.reviewer_login = ""

    def _text(
        self,
        args: list[str],
        *,
        reviewer: bool = False,
        input_text: str | None = None,
        max_output: int = 24 * 1024 * 1024,
    ) -> str:
        """Run a GitHub CLI command and decode strict UTF-8 output.

        Returns:
            The command's decoded stdout.

        Raises:
            LooprError: The command failed or its output was not UTF-8.
        """
        try:
            result = self.runner.run(
                ["gh", *args],
                cwd=self.repo_dir,
                env=self.runner.gh_env(self.reviewer_token if reviewer else None),
                input_text=input_text,
                max_output=max_output,
            )
            return result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_GITHUB, "github", str(exc)) from exc

    def _git_env(self) -> dict[str, str]:
        """Return a Git environment that cannot redirect immutable object reads."""
        env = self.runner.allowlisted_env()
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
        })
        return env

    def initialize(self, pr_value: str) -> None:
        """Resolve the local repository, target PR, and reviewer identity.

        Raises:
            LooprError: The repository, PR, or reviewer identity could not
                be resolved or is inconsistent.
        """
        origin_repo: str | None = None
        try:
            root = self.runner.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.repo_dir,
                env=self._git_env(),
            ).stdout.decode("utf-8", "strict")
            self.repo_dir = Path(root.strip()).resolve()
            origin = self.runner.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.repo_dir,
                env=self._git_env(),
            ).stdout.decode("utf-8", "strict")
            origin_repo = normalize_repo(origin)
        except (CommandError, UnicodeError):
            if pr_value.isdecimal():
                raise LooprError(
                    EXIT_PRECONDITION,
                    "repository",
                    "cannot infer repository from local checkout",
                ) from None
        self.repository, self.number, self.url = resolve_target(
            pr_value,
            origin_repo,
        )
        if origin_repo is not None and origin_repo.lower() != self.repository.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match pull request repository",
            )
        if not self.reviewer_token:
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "GH_REVIEW_TOKEN is required",
            )
        self.reviewer_login = self._text(
            ["api", "--hostname", "github.com", "user", "--jq", ".login"],
            reviewer=True,
        ).strip()
        if not self.reviewer_login:
            raise LooprError(
                EXIT_GITHUB,
                "identity",
                "reviewer identity was empty",
            )

    def snapshot(self) -> PullRequest:
        """Collect and validate one complete PR snapshot.

        Returns:
            The validated pull-request snapshot.

        Raises:
            LooprError: GitHub's response was malformed, inconsistent, or
                failed identity validation.
        """
        data = _json_object(
            self._text(
                ["pr", "view", self.url, "--json", PR_FIELDS],
                max_output=8 * 1024 * 1024,
            ),
            category="github_schema",
        )
        files_value = data.get("files")
        if not isinstance(files_value, list):
            raise LooprError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub field files must be an array",
            )
        paths: list[str] = []
        for item in files_value:
            if not isinstance(item, dict):
                raise LooprError(
                    EXIT_GITHUB,
                    "github_schema",
                    "GitHub changed-file entry must be an object",
                )
            paths.append(validate_path(_string(item.get("path"), field="files.path")))
        advertised_count = _integer(data.get("changedFiles"), field="changedFiles")
        if advertised_count < 0 or advertised_count != len(paths):
            raise LooprError(
                EXIT_PRECONDITION,
                "inventory",
                "GitHub changed-file inventory was truncated or inconsistent",
            )
        if len(paths) != len(set(paths)):
            raise LooprError(
                EXIT_PRECONDITION,
                "path",
                "duplicate changed paths",
            )

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
            owner = _string(head_owner.get("login"), field="headRepositoryOwner.login")
            name = _string(head_repository.get("name"), field="headRepository.name")
            head_repo = f"{owner}/{name}"
        author = _object(data.get("author"), field="author")
        pull_request = PullRequest(
            repository=self.repository,
            number=_integer(data.get("number"), field="number"),
            url=_string(data.get("url"), field="url"),
            title=_string(data.get("title"), field="title", allow_empty=True),
            body=_string(data.get("body") or "", field="body", allow_empty=True),
            author=_string(author.get("login"), field="author.login"),
            state=_string(data.get("state"), field="state"),
            is_draft=bool(data.get("isDraft")),
            base_ref=_string(data.get("baseRefName"), field="baseRefName"),
            base_sha=_string(data.get("baseRefOid"), field="baseRefOid"),
            head_ref=_string(data.get("headRefName"), field="headRefName"),
            head_sha=_string(data.get("headRefOid"), field="headRefOid"),
            head_repository=head_repo,
            changed_paths=tuple(sorted(paths)),
            raw=data,
        )
        self._validate_snapshot_identity(pull_request)
        return pull_request

    def _validate_snapshot_identity(self, pull_request: PullRequest) -> None:
        """Validate state, repository identity, refs, and commit IDs.

        Raises:
            LooprError: pull_request fails any identity, state, or safety check.
        """
        if (
            pull_request.number != self.number
            or pull_request.url.rstrip("/").lower() != self.url.lower()
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "identity",
                "ambiguous pull request identity",
            )
        if pull_request.state != "OPEN" or pull_request.is_draft:
            raise LooprError(
                EXIT_PRECONDITION,
                "state",
                "pull request must be open and non-draft",
            )
        if pull_request.head_repository.lower() != self.repository.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "fork pull requests are not supported",
            )
        if (
            not pull_request.author
            or pull_request.author.lower() == self.reviewer_login.lower()
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "identity",
                "self-review is forbidden",
            )
        if not SHA_RE.fullmatch(pull_request.base_sha) or not SHA_RE.fullmatch(
            pull_request.head_sha
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "sha",
                "invalid base or head SHA",
            )
        validate_ref(pull_request.base_ref)
        validate_ref(pull_request.head_ref)

    def git_bytes(self, args: list[str], *, max_output: int) -> bytes:
        """Read immutable Git data with a strict output bound.

        Returns:
            The command's raw stdout bytes.

        Raises:
            LooprError: The command failed.
        """
        try:
            return self.runner.run(
                ["git", *args],
                cwd=self.repo_dir,
                env=self._git_env(),
                max_output=max_output,
            ).stdout
        except CommandError as exc:
            raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc

    def ensure_objects(self, pull_request: PullRequest) -> None:
        """Require both frozen SHAs to name local commit objects.

        Raises:
            LooprError: Either SHA does not name a local commit object.
        """
        for sha in (pull_request.base_sha, pull_request.head_sha):
            object_type = self.git_bytes(
                ["cat-file", "-t", sha],
                max_output=1024,
            ).decode("utf-8", "strict")
            if object_type.strip() != "commit":
                message = f"{sha} is not a commit object"
                raise LooprError(EXIT_PRECONDITION, "git", message)

    def changed_file_bytes(
        self,
        pull_request: PullRequest,
        path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Read one changed blob, or return None for an explicit omission.

        Returns:
            The blob's exact bytes, or None if it is absent, not a blob, or
            exceeds max_output.

        Raises:
            LooprError: Git returned ambiguous, malformed, or mismatched
                changed-file metadata.
        """
        listing = self.git_bytes(
            [
                "ls-tree",
                "-r",
                "-l",
                "-z",
                "--full-tree",
                pull_request.head_sha,
                "--",
                f":(literal){path}",
            ],
            max_output=4096,
        )
        records = [record for record in listing.split(b"\0") if record]
        if not records:
            return None
        if len(records) != 1 or b"\t" not in records[0]:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned an ambiguous changed-file entry",
            )
        metadata, raw_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != LS_TREE_FIELD_COUNT:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned malformed changed-file metadata",
            )
        _mode, object_type, raw_object_sha, raw_size = fields
        try:
            listed_path = raw_path.decode("utf-8", "strict")
            object_sha = raw_object_sha.decode("ascii", "strict")
        except UnicodeError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned non-UTF-8 changed-file metadata",
            ) from exc
        if listed_path != path or not SHA_RE.fullmatch(object_sha):
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git tree changed-file identity mismatched",
            )
        if object_type != b"blob" or not raw_size.isdigit():
            return None
        size = int(raw_size)
        if size > max_output:
            return None
        data = self.git_bytes(
            ["cat-file", "blob", object_sha],
            max_output=max_output,
        )
        if len(data) != size:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git blob size changed during immutable evidence read",
            )
        return data

    def patch(self, pull_request: PullRequest, *, max_output: int) -> bytes:
        """Read the exact base-to-head merge-base patch.

        Returns:
            The patch's raw bytes.
        """
        return self.git_bytes(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--full-index",
                "--find-renames",
                f"{pull_request.base_sha}...{pull_request.head_sha}",
            ],
            max_output=max_output,
        )

    def tracked_paths(self, pull_request: PullRequest) -> tuple[str, ...]:
        """List every tracked UTF-8 path in the frozen head tree.

        Returns:
            The sorted, distinct tracked paths.

        Raises:
            LooprError: A tracked path is non-UTF-8, unsafe, or duplicated.
        """
        output = self.git_bytes(
            ["ls-tree", "-r", "-z", "--name-only", pull_request.head_sha],
            max_output=4 * 1024 * 1024,
        )
        paths: list[str] = []
        for raw_path in output.split(b"\0"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise LooprError(
                    EXIT_PRECONDITION,
                    "path",
                    "Git tree returned a non-UTF-8 tracked path",
                ) from exc
            paths.append(validate_path(path))
        if len(paths) != len(set(paths)):
            raise LooprError(
                EXIT_PRECONDITION,
                "path",
                "Git tree returned duplicate tracked paths",
            )
        return tuple(sorted(paths))

    def post_review(
        self,
        pull_request: PullRequest,
        verdict: str,
        body: str,
    ) -> tuple[int, JsonObject]:
        """Post one aggregate review anchored to the frozen head SHA.

        Returns:
            The posted review's ID and the raw GitHub response.

        Raises:
            LooprError: GitHub did not anchor the review to the expected head.
        """
        payload: JsonObject = {
            "commit_id": pull_request.head_sha,
            "body": body,
            "event": verdict,
        }
        data = _json_object(
            self._text(
                [
                    "api",
                    "--hostname",
                    "github.com",
                    (
                        f"repos/{pull_request.repository}/pulls/"
                        f"{pull_request.number}/reviews"
                    ),
                    "--method",
                    "POST",
                    "--input",
                    "-",
                ],
                reviewer=True,
                input_text=json.dumps(payload),
            ),
            category="github_schema",
        )
        review_id = _integer(data.get("id"), field="id")
        commit_id = _string(data.get("commit_id"), field="commit_id")
        if review_id <= 0 or commit_id != pull_request.head_sha:
            if review_id > 0:
                self.dismiss(pull_request, review_id)
            raise LooprError(
                EXIT_RACE,
                "race",
                "GitHub did not anchor the review to the expected head",
            )
        return review_id, data

    def dismiss(self, pull_request: PullRequest, review_id: int) -> None:
        """Dismiss a posted review that became stale."""
        payload: JsonObject = {
            "message": "Dismissed automatically: reviewed PR snapshot became stale."
        }
        self._text(
            [
                "api",
                "--hostname",
                "github.com",
                (
                    f"repos/{pull_request.repository}/pulls/{pull_request.number}/"
                    f"reviews/{review_id}/dismissals"
                ),
                "--method",
                "PUT",
                "--input",
                "-",
            ],
            reviewer=True,
            input_text=json.dumps(payload),
        )

    def verify_posted(
        self,
        pull_request: PullRequest,
        review_id: int,
    ) -> JsonObject:
        """Re-read and validate the posted review identity and commit.

        Returns:
            The re-read, validated review response.

        Raises:
            LooprError: The re-read review's identity or commit does not match.
        """
        data = _json_object(
            self._text(
                [
                    "api",
                    "--hostname",
                    "github.com",
                    (
                        f"repos/{pull_request.repository}/pulls/{pull_request.number}/"
                        f"reviews/{review_id}"
                    ),
                ],
                reviewer=True,
            ),
            category="github_schema",
        )
        user = _object(data.get("user"), field="user")
        if (
            _integer(data.get("id"), field="id") != review_id
            or _string(user.get("login"), field="user.login").lower()
            != self.reviewer_login.lower()
            or _string(data.get("commit_id"), field="commit_id")
            != pull_request.head_sha
        ):
            raise LooprError(
                EXIT_GITHUB,
                "github",
                "posted review revalidation failed",
            )
        return data

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Return whether two snapshots have identical base and head SHAs."""
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha
