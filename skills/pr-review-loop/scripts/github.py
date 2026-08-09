"""GitHub and immutable Git snapshot access."""

from __future__ import annotations

import json
import operator
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    IssueSnapshot,
    JsonObject,
    JsonValue,
    LooprError,
    PullRequest,
    PullRequestIdentity,
    ReviewComment,
)
from .process import CommandError

if TYPE_CHECKING:
    from .process import CommandRunner

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PART_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
OWNER_REPO_PART_COUNT = 2
LS_TREE_FIELD_COUNT = 4
MIN_PRINTABLE_CODEPOINT = 32
DEL_CODEPOINT = 127
MAX_ISSUE_COMMENTS = 30
MAX_ISSUE_COMMENT_BYTES = 20_000
MAX_ISSUE_COMMENTS_TOTAL_BYTES = 300_000
MAX_ISSUE_BODY_BYTES = 200_000
MAX_ANCHOR_PATCH_BYTES = 2 * 1024 * 1024
BODY_MARKERS = frozenset({" ", "+", "-", "\\"})
PR_FIELDS = (
    "url,number,title,body,author,state,isDraft,baseRefName,baseRefOid,"
    "headRefName,headRefOid,headRepository,headRepositoryOwner,files,changedFiles"
)
PR_IDENTITY_FIELDS = (
    "url,number,state,isDraft,baseRefName,baseRefOid,"
    "headRefName,headRefOid,headRepository,headRepositoryOwner"
)
ISSUE_GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $lastComments: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      url
      state
      title
      body
      author { login }
      updatedAt
      comments(last: $lastComments) {
        nodes {
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""


def normalize_repo(remote: str) -> str:
    """Normalize one unambiguous GitHub.com repository remote.

    Returns:
        The `owner/name` repository identifier.

    Raises:
        LooprError: remote is not an unambiguous github.com repository URL.
    """
    value = remote
    if (
        any(
            character.isspace()
            or ord(character) < MIN_PRINTABLE_CODEPOINT
            or ord(character) == DEL_CODEPOINT
            for character in value
        )
        or "?" in value
        or "#" in value
    ):
        raise LooprError(
            EXIT_PRECONDITION,
            "repository",
            "origin must be an unambiguous github.com URL",
        )
    match = re.fullmatch(
        r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?",
        value,
    )
    if match is not None:
        owner, name = match.groups()
    else:
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must be an unambiguous github.com URL",
            ) from None
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.netloc not in {"github.com", "git@github.com"}
            or parsed.query
            or parsed.fragment
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must be an unambiguous github.com URL",
            )
        parts = parsed.path.split("/")
        if (
            len(parts) != OWNER_REPO_PART_COUNT + 1
            or parts[0]
            or not parts[1]
            or not parts[2]
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must identify exactly one repository",
            )
        _empty, owner, name = parts
        name = name.removesuffix(".git")
    if not PART_RE.fullmatch(owner) or not PART_RE.fullmatch(name):
        raise LooprError(
            EXIT_PRECONDITION,
            "repository",
            "invalid repository name",
        )
    return f"{owner}/{name}"


def _resolve_target(
    value: str,
    origin_repo: str | None,
    *,
    route: str,
    numeric_message: str,
    invalid_message: str,
    target_name: str,
) -> tuple[str, int]:
    """Resolve one strict numeric or canonical GitHub target.

    Returns:
        The repository and positive target number.

    Raises:
        LooprError: value is not a positive number or canonical GitHub URL,
            or a numeric value is given without origin_repo.
    """
    ascii_number = re.fullmatch(r"[0-9]+", value)
    if ascii_number is not None:
        if origin_repo is None:
            raise LooprError(EXIT_PRECONDITION, "input", numeric_message)
        repository, number = origin_repo, int(value)
    else:
        if (
            any(
                character.isspace()
                or ord(character) < MIN_PRINTABLE_CODEPOINT
                or ord(character) == DEL_CODEPOINT
                for character in value
            )
            or "?" in value
            or "#" in value
        ):
            raise LooprError(EXIT_PRECONDITION, "input", invalid_message)
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise LooprError(EXIT_PRECONDITION, "input", invalid_message) from None
        target = re.fullmatch(
            rf"/([^/]+)/([^/]+)/{re.escape(route)}/([0-9]+)",
            parsed.path,
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or target is None
            or not PART_RE.fullmatch(target.group(1))
            or not PART_RE.fullmatch(target.group(2))
        ):
            raise LooprError(EXIT_PRECONDITION, "input", invalid_message)
        repository = f"{target.group(1)}/{target.group(2)}"
        number = int(target.group(3))
    if number <= 0:
        raise LooprError(
            EXIT_PRECONDITION,
            "input",
            f"{target_name} number must be positive",
        )
    return repository, number


def resolve_target(value: str, origin_repo: str | None) -> tuple[str, int, str]:
    """Resolve a positive PR number or canonical GitHub pull URL.

    Returns:
        The repository, PR number, and canonical pull URL.
    """
    repository, number = _resolve_target(
        value,
        origin_repo,
        route="pull",
        numeric_message="numeric --pr requires an unambiguous local origin",
        invalid_message="--pr must be a positive number or canonical GitHub pull URL",
        target_name="pull request",
    )
    url = f"https://github.com/{repository}/pull/{number}"
    return repository, number, url


def resolve_issue_target(value: str, origin_repo: str | None) -> tuple[str, int, str]:
    """Resolve a positive Issue number or canonical GitHub issue URL.

    Returns:
        The repository, Issue number, and canonical issue URL.
    """
    repository, number = _resolve_target(
        value,
        origin_repo,
        route="issues",
        numeric_message="numeric --issue requires an unambiguous local origin",
        invalid_message=(
            "--issue must be a positive number or canonical GitHub issue URL"
        ),
        target_name="issue",
    )
    url = f"https://github.com/{repository}/issues/{number}"
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


def _header_path(value: str, prefix: str) -> str | None:
    """Return one safe diff-header path, or None when it names no usable file.

    Returns:
        The prefix-stripped path, or None for /dev/null, a quoted path, or a
        path that is unsafe to address.
    """
    if not value.startswith(prefix):
        return None
    try:
        return validate_path(value.removeprefix(prefix))
    except LooprError:
        return None


@dataclass
class _DiffCursor:
    """Mutable scan position inside one unified-diff file section."""

    path: str | None = None
    old_line: int = 0
    new_line: int = 0
    in_hunk: bool = False


def _scan_body_line(
    marker: str,
    cursor: _DiffCursor,
    anchors: set[tuple[str, str, int]],
    allowed_paths: frozenset[str],
) -> None:
    """Record the anchors one hunk body line contributes and advance the cursor."""
    if marker == "\\":
        return
    path = cursor.path
    if path is not None and path in allowed_paths:
        if marker == "-":
            anchors.add((path, "LEFT", cursor.old_line))
        if marker in {" ", "+"}:
            anchors.add((path, "RIGHT", cursor.new_line))
    if marker in {" ", "-"}:
        cursor.old_line += 1
    if marker in {" ", "+"}:
        cursor.new_line += 1


def diff_anchors(
    patch: str, allowed_paths: frozenset[str]
) -> frozenset[tuple[str, str, int]]:
    """Enumerate every `(path, side, line)` GitHub can anchor a comment to.

    Only lines that the frozen base-to-head patch itself contains are
    enumerated, matching GitHub's own line-comment semantics: added and
    unchanged/context lines on `RIGHT` with their head-file line numbers, and
    only removed lines on `LEFT` with their base-file line numbers. A context
    line is never a valid `LEFT` anchor. A file is addressed by the path it
    has at head, which is the path GitHub's review API expects, so a rename's
    `LEFT` lines and a deletion's lines stay addressable. Any path outside
    allowed_paths, and any diff section this scanner cannot read
    unambiguously, contributes no anchors.

    Returns:
        The anchors the reviewed diff supports.
    """
    anchors: set[tuple[str, str, int]] = set()
    cursor = _DiffCursor()
    old_path: str | None = None
    for line in patch.split("\n"):
        marker = line[:1]
        if cursor.in_hunk and marker in BODY_MARKERS:
            _scan_body_line(marker, cursor, anchors, allowed_paths)
            continue
        cursor.in_hunk = False
        if line.startswith("diff --git "):
            old_path, cursor.path = None, None
        elif line.startswith("--- "):
            old_path = _header_path(line.removeprefix("--- "), "a/")
        elif line.startswith("+++ "):
            cursor.path = _header_path(line.removeprefix("+++ "), "b/") or old_path
        elif (match := HUNK_RE.match(line)) is not None and cursor.path is not None:
            cursor.old_line = int(match.group(1))
            cursor.new_line = int(match.group(2))
            cursor.in_hunk = True
    return frozenset(anchors)


def parse_json_object(text: str, *, category: str) -> JsonObject:
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


def require_object(value: JsonValue | None, *, field: str) -> JsonObject:
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


def require_string(
    value: JsonValue | None, *, field: str, allow_empty: bool = False
) -> str:
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


def require_integer(value: JsonValue | None, *, field: str) -> int:
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


def require_boolean(value: JsonValue | None, *, field: str) -> bool:
    """Require a JSON Boolean field without coercing other falsey values.

    Returns:
        value.

    Raises:
        LooprError: value is not a JSON Boolean.
    """
    if not isinstance(value, bool):
        message = f"GitHub field {field} must be a boolean"
        raise LooprError(EXIT_GITHUB, "github_schema", message)
    return value


def _optional_author_login(value: JsonValue | None, *, field: str) -> str:
    """Return one Issue/comment author's login, or "" for a deleted account.

    GitHub's schema defines `Issue.author`/`IssueComment.author` as a
    nullable `Actor`, and `gh` may surface a deleted account as either a
    null author or an author object with a null/empty login.

    Returns:
        The author's login, or "" when GitHub reports no author.
    """
    if value is None:
        return ""
    author = require_object(value, field=field)
    login = author.get("login")
    if login is None:
        login = ""
    return require_string(login, field=f"{field}.login", allow_empty=True)


class _ImmutableGitMixin:
    """Shared immutable Git object reads for identity-bound GitHub clients."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        """Initialize the shared immutable Git reader state."""
        self.runner = runner
        self.repo_dir = repo_dir

    def _git_text(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        max_output: int = 1024 * 1024,
    ) -> str:
        """Run one bounded Git read.

        Returns:
            Strictly decoded stdout.

        Raises:
            LooprError: Git failed or returned invalid UTF-8.
        """
        try:
            result = self.runner.run(
                ["git", *args],
                cwd=self.repo_dir,
                env=env,
                max_output=max_output,
            )
            return result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_PRECONDITION, "git", str(exc)) from exc

    def _text(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        max_output: int = 24 * 1024 * 1024,
    ) -> str:
        """Run one bounded GitHub CLI read.

        Returns:
            Strictly decoded stdout.

        Raises:
            LooprError: GitHub failed or returned invalid UTF-8.
        """
        try:
            result = self.runner.run(
                ["gh", *args],
                cwd=self.repo_dir,
                env=self.runner.gh_env(),
                input_text=input_text,
                max_output=max_output,
            )
            return result.stdout.decode("utf-8", "strict")
        except (CommandError, UnicodeError) as exc:
            raise LooprError(EXIT_GITHUB, "github", str(exc)) from exc

    def _initialize_repository(
        self,
        *,
        hardened: bool,
        require_push_url: bool,
    ) -> str:
        """Resolve the repository root and one unambiguous origin.

        Returns:
            Normalized fetch repository.

        Raises:
            LooprError: The checkout or origin is invalid or ambiguous.
        """
        env = self._git_env() if hardened else self.runner.base_env()
        try:
            root = self._git_text(["rev-parse", "--show-toplevel"], env=env).strip()
        except LooprError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "cannot infer repository from local checkout",
            ) from exc
        if not root:
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "Git returned an empty repository root",
            )
        self.repo_dir = Path(root).resolve()
        fetch_repo = self._origin_repo(env=env, push=False)
        if require_push_url:
            push_repo = self._origin_repo(env=env, push=True)
            if fetch_repo.casefold() != push_repo.casefold():
                raise LooprError(
                    EXIT_PRECONDITION,
                    "repository",
                    "origin fetch and push URLs must identify the same repository",
                )
        return fetch_repo

    def _origin_repo(self, *, env: dict[str, str], push: bool) -> str:
        """Read exactly one normalized origin URL.

        Returns:
            Normalized repository identifier.

        Raises:
            LooprError: The origin is missing, ambiguous, or invalid.
        """
        kind = "push" if push else "fetch"
        try:
            urls = self._git_text(
                [
                    "remote",
                    "get-url",
                    *(["--push"] if push else []),
                    "--all",
                    "origin",
                ],
                env=env,
            ).splitlines()
        except LooprError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "origin must have exactly one push URL"
                if push
                else "cannot infer repository from local checkout",
            ) from exc
        if len(urls) != 1 or not urls[0].strip():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                f"origin must have exactly one {kind} URL",
            )
        return normalize_repo(urls[0])

    def _git_env(self) -> dict[str, str]:
        """Return a Git environment that cannot redirect immutable object reads."""
        env = self.runner.allowlisted_env()
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
        })
        return env

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

    def git_text(self, args: list[str], *, max_output: int) -> str:
        """Read one immutable Git response as UTF-8 text.

        Returns:
            Strictly decoded stdout.

        Raises:
            LooprError: Git failed or returned invalid UTF-8.
        """
        try:
            return self.git_bytes(args, max_output=max_output).decode(
                "utf-8",
                "strict",
            )
        except UnicodeError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "git",
                "Git returned non-UTF-8 output",
            ) from exc

    def ensure_commit_object(self, sha: str) -> None:
        """Require sha to name a local commit object.

        Raises:
            LooprError: sha does not name a local commit object.
        """
        object_type = self.git_bytes(
            ["cat-file", "-t", sha],
            max_output=1024,
        ).decode("utf-8", "strict")
        if object_type.strip() != "commit":
            message = f"{sha} is not a commit object"
            raise LooprError(EXIT_PRECONDITION, "git", message)

    def tracked_paths_at(self, sha: str) -> tuple[str, ...]:
        """List every tracked UTF-8 path in the frozen tree at sha.

        Returns:
            The sorted, distinct tracked paths.

        Raises:
            LooprError: A tracked path is non-UTF-8, unsafe, or duplicated.
        """
        output = self.git_bytes(
            ["ls-tree", "-r", "-z", "--name-only", sha],
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

    def blob_bytes_at(
        self,
        sha: str,
        path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Read one blob at sha, or return None for an explicit omission.

        Returns:
            The blob's exact bytes, or None if it is absent, not a blob, or
            exceeds max_output.

        Raises:
            LooprError: Git returned ambiguous, malformed, or mismatched
                tree metadata.
        """
        listing = self.git_bytes(
            [
                "ls-tree",
                "-r",
                "-l",
                "-z",
                "--full-tree",
                sha,
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


class GitHubClient(_ImmutableGitMixin):
    """Read PR snapshots and post reviews through trusted CLI commands."""

    def __init__(
        self,
        runner: CommandRunner,
        repo_dir: Path,
    ) -> None:
        """Initialize an unresolved GitHub client."""
        super().__init__(runner, repo_dir.resolve())
        self.repository = ""
        self.number = 0
        self.url = ""
        self.authenticated_login = ""

    def initialize(self, pr_value: str) -> None:
        """Resolve the local repository and target PR.

        Raises:
            LooprError: The repository or PR could not be resolved or is
                inconsistent.
        """
        origin_repo = self._initialize_repository(
            hardened=True,
            require_push_url=False,
        )
        self._set_target(pr_value, origin_repo)
        self.authenticated_login = self._text(
            ["api", "--hostname", "github.com", "user", "--jq", ".login"],
        ).strip()
        if not self.authenticated_login:
            raise LooprError(
                EXIT_GITHUB,
                "identity",
                "authenticated GitHub login was empty",
            )

    def initialize_for_submit(self, pr_value: str) -> None:
        """Resolve a submit target without performing reviewer identity lookup.

        Submit uses ordinary Git credentials for repository identity and push
        URL reads, while all PR schema and snapshot validation stays canonical
        in this client.
        """
        origin_repo = self._initialize_repository(
            hardened=False,
            require_push_url=True,
        )
        self._set_target(pr_value, origin_repo)

    def _set_target(self, pr_value: str, origin_repo: str) -> None:
        """Resolve and bind one PR target to the local origin repository.

        Raises:
            LooprError: The target is invalid or does not match origin_repo.
        """
        self.repository, self.number, self.url = resolve_target(
            pr_value,
            origin_repo,
        )
        if origin_repo.casefold() != self.repository.casefold():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match pull request repository",
            )

    def snapshot(self, *, require_open: bool = True) -> PullRequest:
        """Collect and validate one complete PR snapshot.

        Returns:
            The validated pull-request snapshot. When require_open is false,
                closed or draft state is permitted for post-push confirmation.

        Raises:
            LooprError: GitHub's response was malformed, inconsistent, or
                failed identity validation.
        """
        data = self._pull_request_data(PR_FIELDS)
        identity = self._parse_identity(data, require_open=require_open)
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
            paths.append(
                validate_path(require_string(item.get("path"), field="files.path"))
            )
        advertised_count = require_integer(
            data.get("changedFiles"), field="changedFiles"
        )
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

        author = require_object(data.get("author"), field="author")
        author_login = require_string(author.get("login"), field="author.login")
        if not author_login:
            raise LooprError(
                EXIT_PRECONDITION,
                "identity",
                "pull request author was empty",
            )
        pull_request = PullRequest(
            repository=identity.repository,
            number=identity.number,
            url=identity.url,
            title=require_string(data.get("title"), field="title", allow_empty=True),
            body=require_string(data.get("body"), field="body", allow_empty=True),
            author=author_login,
            state=identity.state,
            is_draft=identity.is_draft,
            base_ref=identity.base_ref,
            base_sha=identity.base_sha,
            head_ref=identity.head_ref,
            head_sha=identity.head_sha,
            head_repository=identity.head_repository,
            changed_paths=tuple(sorted(paths)),
            raw=data,
        )
        return pull_request

    def identity_snapshot(
        self,
        *,
        require_open: bool = True,
    ) -> PullRequestIdentity:
        """Collect and validate PR identity, state, and ref fields only.

        Submit uses this smaller snapshot because GitHub CLI caps the
        `files` field at 100 entries, while review requires the complete
        changed-file inventory.

        Returns:
            The validated pull-request identity and ref snapshot.
        """
        return self._parse_identity(
            self._pull_request_data(PR_IDENTITY_FIELDS),
            require_open=require_open,
        )

    def _pull_request_data(self, fields: str) -> JsonObject:
        """Fetch one bounded GitHub pull-request response.

        Returns:
            The parsed GitHub pull-request response.
        """
        return parse_json_object(
            self._text(
                ["pr", "view", self.url, "--json", fields],
                max_output=8 * 1024 * 1024,
            ),
            category="github_schema",
        )

    def _parse_identity(
        self,
        data: JsonObject,
        *,
        require_open: bool,
    ) -> PullRequestIdentity:
        """Parse and validate the shared PR identity fields.

        Returns:
            The validated pull-request identity and ref snapshot.
        """
        head_repository = require_object(
            data.get("headRepository"),
            field="headRepository",
        )
        head_owner = require_object(
            data.get("headRepositoryOwner"),
            field="headRepositoryOwner",
        )
        if "nameWithOwner" in head_repository:
            head_repo = require_string(
                head_repository.get("nameWithOwner"),
                field="headRepository.nameWithOwner",
            )
        else:
            owner = require_string(
                head_owner.get("login"), field="headRepositoryOwner.login"
            )
            name = require_string(
                head_repository.get("name"), field="headRepository.name"
            )
            head_repo = f"{owner}/{name}"
        identity = PullRequestIdentity(
            repository=self.repository,
            number=require_integer(data.get("number"), field="number"),
            url=require_string(data.get("url"), field="url"),
            state=require_string(data.get("state"), field="state"),
            is_draft=require_boolean(data.get("isDraft"), field="isDraft"),
            base_ref=require_string(data.get("baseRefName"), field="baseRefName"),
            base_sha=require_string(data.get("baseRefOid"), field="baseRefOid"),
            head_ref=require_string(data.get("headRefName"), field="headRefName"),
            head_sha=require_string(data.get("headRefOid"), field="headRefOid"),
            head_repository=head_repo,
            raw=data,
        )
        self._validate_snapshot_identity(identity, require_open=require_open)
        return identity

    def _validate_snapshot_identity(
        self,
        pull_request: PullRequestIdentity,
        *,
        require_open: bool,
    ) -> None:
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
        if require_open and (pull_request.state != "OPEN" or pull_request.is_draft):
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

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        """Select the GitHub transport event for a validated Oracle verdict.

        Formal review events remain available for PRs authored by someone
        else. GitHub rejects formal self-reviews, so self-authored PRs use a
        commit-anchored comment while the Oracle verdict stays canonical in
        the command result.

        Returns:
            `COMMENT` for a self-authored PR, otherwise the Oracle verdict.

        Raises:
            LooprError: The verdict is not an accepted Oracle verdict.
        """
        if verdict not in {"APPROVE", "REQUEST_CHANGES"}:
            raise LooprError(
                EXIT_PRECONDITION,
                "verdict",
                "invalid review verdict",
            )
        if pull_request.author.casefold() == self.authenticated_login.casefold():
            return "COMMENT"
        return verdict

    def ensure_objects(self, pull_request: PullRequest) -> None:
        """Require both frozen SHAs to name local commit objects."""
        for sha in (pull_request.base_sha, pull_request.head_sha):
            self.ensure_commit_object(sha)

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
        """
        return self.blob_bytes_at(
            pull_request.head_sha,
            path,
            max_output=max_output,
        )

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

    def diff_anchors(
        self, pull_request: PullRequest
    ) -> frozenset[tuple[str, str, int]]:
        """Enumerate the comment anchors the frozen base-to-head diff supports.

        Returns:
            The anchors, restricted to the snapshot's validated changed paths.

        Raises:
            LooprError: The patch is not UTF-8.
        """
        patch = self.patch(pull_request, max_output=MAX_ANCHOR_PATCH_BYTES)
        try:
            text = patch.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "bundle",
                "patch is not UTF-8",
            ) from exc
        return diff_anchors(text, frozenset(pull_request.changed_paths))

    def tracked_paths(self, pull_request: PullRequest) -> tuple[str, ...]:
        """List every tracked UTF-8 path in the frozen head tree.

        Returns:
            The sorted, distinct tracked paths.
        """
        return self.tracked_paths_at(pull_request.head_sha)

    def post_review(
        self,
        pull_request: PullRequest,
        event: str,
        body: str,
        comments: tuple[ReviewComment, ...] = (),
    ) -> tuple[int, JsonObject]:
        """Post one review, with any inline comments, anchored to the frozen head.

        The aggregate body and every inline comment are published by the same
        create-review request, so publication stays a single atomic write that
        GitHub either accepts whole or rejects whole.

        Returns:
            The posted review's ID and the raw GitHub response.

        Raises:
            LooprError: GitHub did not anchor the review to the expected head.
        """
        payload: JsonObject = {
            "commit_id": pull_request.head_sha,
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = [comment.as_payload() for comment in comments]
        data = parse_json_object(
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
                input_text=json.dumps(payload),
            ),
            category="github_schema",
        )
        review_id = require_integer(data.get("id"), field="id")
        commit_id = require_string(data.get("commit_id"), field="commit_id")
        if review_id <= 0 or commit_id != pull_request.head_sha:
            if review_id > 0 and event != "COMMENT":
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
            input_text=json.dumps(payload),
        )

    def verify_posted(
        self,
        pull_request: PullRequest,
        review_id: int,
        body: str,
    ) -> JsonObject:
        """Re-read and validate the posted review identity, commit, and body.

        Returns:
            The re-read, validated review response.

        Raises:
            LooprError: The re-read review's identity or commit does not match.
        """
        data = parse_json_object(
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
            ),
            category="github_schema",
        )
        if (
            require_integer(data.get("id"), field="id") != review_id
            or require_string(
                require_object(data.get("user"), field="user").get("login"),
                field="user.login",
            ).casefold()
            != self.authenticated_login.casefold()
            or require_string(data.get("commit_id"), field="commit_id")
            != pull_request.head_sha
            or require_string(data.get("body"), field="body") != body
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


def _bounded_comments(value: list[JsonValue]) -> tuple[JsonObject, ...]:
    """Validate, order, and bound one Issue's comment collection.

    Comments are sorted by `createdAt` (GitHub does not contractually
    guarantee response order), then only the most recent
    `MAX_ISSUE_COMMENTS` are kept. The aggregate byte budget is then
    allocated newest-first, so when it is exceeded the oldest retained
    comments are the ones replaced with an omission marker; the emitted
    collection is restored to chronological order afterward. Each kept
    comment's body is replaced with an omission marker if it, or the
    running total, exceeds its byte bound, so oversized text is dropped
    outright rather than silently truncated.

    Returns:
        The validated, ordered, bounded comment collection.

    Raises:
        LooprError: value is not a well-formed comment array.
    """
    parsed: list[tuple[str, str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise LooprError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub comment entry must be an object",
            )
        parsed.append((
            _optional_author_login(item.get("author"), field="comments.author"),
            require_string(item.get("body"), field="comments.body", allow_empty=True),
            require_string(item.get("createdAt"), field="comments.createdAt"),
        ))
    parsed.sort(key=operator.itemgetter(2))
    kept = parsed[-MAX_ISSUE_COMMENTS:] if len(parsed) > MAX_ISSUE_COMMENTS else parsed
    omitted_flags = [False] * len(kept)
    total = 0
    for index in range(len(kept) - 1, -1, -1):
        body_bytes = len(kept[index][1].encode("utf-8"))
        omitted = (
            body_bytes > MAX_ISSUE_COMMENT_BYTES
            or total + body_bytes > MAX_ISSUE_COMMENTS_TOTAL_BYTES
        )
        omitted_flags[index] = omitted
        if not omitted:
            total += body_bytes
    comments: list[JsonObject] = []
    for (author, body, created_at), omitted in zip(kept, omitted_flags, strict=True):
        comments.append({
            "author": author,
            "body": "" if omitted else body,
            "created_at": created_at,
            "omitted": omitted,
        })
    return tuple(comments)


class IssueClient(_ImmutableGitMixin):
    """Read Issue snapshots and repository base-branch identity for bootstrap."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        """Initialize an unresolved Issue client."""
        super().__init__(runner, repo_dir.resolve())
        self.repository = ""
        self.number = 0
        self.url = ""

    def initialize(self, issue_value: str) -> None:
        """Resolve the local repository and target Issue.

        Bootstrap always hands the returned base commit to the host for
        implementation in this checkout, so, unlike PR review, an
        unambiguous local `origin` is required for both numeric and URL
        input; a canonical Issue URL never bypasses the matching-origin
        check.

        Raises:
            LooprError: The repository or Issue could not be resolved or is
                inconsistent with the local checkout.
        """
        origin_repo = self._initialize_repository(
            hardened=True,
            require_push_url=False,
        )
        self.repository, self.number, self.url = resolve_issue_target(
            issue_value,
            origin_repo,
        )
        if origin_repo.lower() != self.repository.lower():
            raise LooprError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match issue repository",
            )

    def snapshot(self) -> IssueSnapshot:
        """Collect and validate one bounded Issue snapshot.

        Returns:
            The validated Issue snapshot.

        Raises:
            LooprError: GitHub's response was malformed, inconsistent, failed
                identity validation, or contained a known credential.
        """
        owner, _, name = self.repository.partition("/")
        envelope = parse_json_object(
            self._text(
                [
                    "api",
                    "--hostname",
                    "github.com",
                    "graphql",
                    "-f",
                    f"query={ISSUE_GRAPHQL_QUERY}",
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={name}",
                    "-F",
                    f"number={self.number}",
                    "-F",
                    f"lastComments={MAX_ISSUE_COMMENTS}",
                ],
                max_output=8 * 1024 * 1024,
            ),
            category="github_schema",
        )
        response_data = require_object(envelope.get("data"), field="data")
        repository_data = require_object(
            response_data.get("repository"),
            field="data.repository",
        )
        data = require_object(
            repository_data.get("issue"), field="data.repository.issue"
        )
        comments_field = require_object(data.get("comments"), field="comments")
        comments_value = comments_field.get("nodes")
        if not isinstance(comments_value, list):
            raise LooprError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub field comments.nodes must be an array",
            )
        body = require_string(data.get("body"), field="body", allow_empty=True)
        if len(body.encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
            raise LooprError(
                EXIT_PRECONDITION,
                "bundle",
                "issue body exceeds bound",
            )
        issue = IssueSnapshot(
            repository=self.repository,
            number=require_integer(data.get("number"), field="number"),
            url=require_string(data.get("url"), field="url"),
            title=require_string(data.get("title"), field="title", allow_empty=True),
            body=body,
            author=_optional_author_login(data.get("author"), field="author"),
            state=require_string(data.get("state"), field="state"),
            updated_at=require_string(data.get("updatedAt"), field="updatedAt"),
            comments=_bounded_comments(comments_value),
            raw=data,
        )
        self._validate_snapshot_identity(issue)
        self._validate_content_safety(issue)
        return issue

    def _validate_snapshot_identity(self, issue: IssueSnapshot) -> None:
        """Validate identity and state.

        Raises:
            LooprError: issue fails any identity or state check.
        """
        if (
            issue.number != self.number
            or issue.url.rstrip("/").lower() != self.url.lower()
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "identity",
                "ambiguous issue identity",
            )
        if issue.state != "OPEN":
            raise LooprError(
                EXIT_PRECONDITION,
                "state",
                "issue must be open",
            )

    def _validate_content_safety(self, issue: IssueSnapshot) -> None:
        """Reject Issue content that carries a known credential value.

        Raises:
            LooprError: issue title, body, or a comment body contains a
                known credential value.
        """
        texts = [issue.title, issue.body]
        for comment in issue.comments:
            body = comment.get("body")
            if isinstance(body, str):
                texts.append(body)
        if any(self.runner.contains_secret(text) for text in texts):
            raise LooprError(
                EXIT_PRECONDITION,
                "credentials",
                "issue content contains a known credential",
            )

    def default_branch(self) -> str:
        """Return the repository's current default branch name.

        Returns:
            The validated default branch name.
        """
        data = parse_json_object(
            self._text(["repo", "view", self.repository, "--json", "defaultBranchRef"]),
            category="github_schema",
        )
        ref = require_object(data.get("defaultBranchRef"), field="defaultBranchRef")
        branch = require_string(ref.get("name"), field="defaultBranchRef.name")
        validate_ref(branch)
        return branch

    def branch_sha(self, branch: str) -> str:
        """Return one branch's exact current commit SHA.

        Returns:
            The validated 40-character commit SHA.

        Raises:
            LooprError: GitHub's response was malformed or the SHA is invalid.
        """
        encoded_branch = urllib.parse.quote(branch, safe="")
        data = parse_json_object(
            self._text([
                "api",
                "--hostname",
                "github.com",
                f"repos/{self.repository}/branches/{encoded_branch}",
            ]),
            category="github_schema",
        )
        commit = require_object(data.get("commit"), field="commit")
        sha = require_string(commit.get("sha"), field="commit.sha")
        if not SHA_RE.fullmatch(sha):
            raise LooprError(
                EXIT_PRECONDITION,
                "sha",
                "invalid base SHA",
            )
        return sha
