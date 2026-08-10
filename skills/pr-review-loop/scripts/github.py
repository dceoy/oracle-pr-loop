"""GitHub and immutable Git snapshot access."""

from __future__ import annotations

import json
import operator
import os
import re
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from .models import (
    EXIT_GITHUB,
    EXIT_PRECONDITION,
    EXIT_RACE,
    IssueSnapshot,
    JsonObject,
    JsonValue,
    PullRequest,
    PullRequestIdentity,
    ReviewComment,
    ReviewLoopError,
)
from .process import MAX_INPUT, CommandError

if TYPE_CHECKING:
    from .process import CommandRunner

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
INDEX_RE = re.compile(r"^index ([0-9a-f]{40})\.\.([0-9a-f]{40})(?: [0-7]{6})?$")
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
MAX_ANCHOR_BLOB_BYTES = 2 * 1024 * 1024
MAX_ANCHOR_ATTR_BYTES = 2 * 1024 * 1024
MAX_GITHUB_DIFF_BYTES = 1 * 1024 * 1024
MAX_GITHUB_DIFF_LINES = 20_000
MAX_GITHUB_FILE_DIFF_BYTES = 500 * 1024
MAX_GITHUB_FILE_DIFF_LINES = 20_000
NULL_SHA = "0" * 40
NO_NEWLINE_MARKER = "\\ No newline at end of file"
BODY_MARKERS = frozenset({" ", "+", "-"})
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
        The canonical `owner/repository` identifier.

    Raises:
        ReviewLoopError: The remote is ambiguous, malformed, or not GitHub.com.
    """
    if (
        any(
            character.isspace()
            or ord(character) < MIN_PRINTABLE_CODEPOINT
            or ord(character) == DEL_CODEPOINT
            for character in remote
        )
        or "?" in remote
        or "#" in remote
    ):
        message = "origin must be an unambiguous github.com URL"
        raise ReviewLoopError(EXIT_PRECONDITION, "repository", message)

    match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", remote)
    if match is not None:
        owner, name = match.groups()
    else:
        try:
            parsed = urllib.parse.urlsplit(remote)
        except ValueError:
            message = "origin must be an unambiguous github.com URL"
            raise ReviewLoopError(EXIT_PRECONDITION, "repository", message) from None
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.netloc not in {"github.com", "git@github.com"}
            or parsed.query
            or parsed.fragment
        ):
            message = "origin must be an unambiguous github.com URL"
            raise ReviewLoopError(EXIT_PRECONDITION, "repository", message)
        parts = parsed.path.split("/")
        if (
            len(parts) != OWNER_REPO_PART_COUNT + 1
            or parts[0]
            or not parts[1]
            or not parts[2]
        ):
            message = "origin must identify exactly one repository"
            raise ReviewLoopError(EXIT_PRECONDITION, "repository", message)
        _empty, owner, name = parts
        name = name.removesuffix(".git")

    if not PART_RE.fullmatch(owner) or not PART_RE.fullmatch(name):
        raise ReviewLoopError(
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
        The canonical repository identifier and positive target number.

    Raises:
        ReviewLoopError: The target is missing repository context or malformed.
    """
    if re.fullmatch(r"[0-9]+", value) is not None:
        if origin_repo is None:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "input",
                numeric_message,
            )
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "input",
                invalid_message,
            )
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "input",
                invalid_message,
            ) from None
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "input",
                invalid_message,
            )
        repository = f"{target.group(1)}/{target.group(2)}"
        number = int(target.group(3))

    if number <= 0:
        message = f"{target_name} number must be positive"
        raise ReviewLoopError(EXIT_PRECONDITION, "input", message)
    return repository, number


def resolve_target(value: str, origin_repo: str | None) -> tuple[str, int, str]:
    """Resolve a positive PR number or canonical GitHub pull URL.

    Returns:
        Repository, PR number, and canonical GitHub pull URL.
    """
    repository, number = _resolve_target(
        value,
        origin_repo,
        route="pull",
        numeric_message="numeric --pr requires an unambiguous local origin",
        invalid_message=("--pr must be a positive number or canonical GitHub pull URL"),
        target_name="pull request",
    )
    return repository, number, f"https://github.com/{repository}/pull/{number}"


def resolve_issue_target(
    value: str,
    origin_repo: str | None,
) -> tuple[str, int, str]:
    """Resolve a positive Issue number or canonical GitHub issue URL.

    Returns:
        Repository, Issue number, and canonical GitHub Issue URL.
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
    return repository, number, f"https://github.com/{repository}/issues/{number}"


def validate_ref(ref: str) -> None:
    """Reject Git refs that can alter command interpretation or traversal.

    Raises:
        ReviewLoopError: The ref is unsafe for deterministic Git invocation.
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
        raise ReviewLoopError(EXIT_PRECONDITION, "ref", "unsafe Git ref")


def validate_path(path: str) -> str:
    """Reject changed paths that can escape the immutable Git snapshot.

    Returns:
        The unchanged validated POSIX path.

    Raises:
        ReviewLoopError: The path is empty, unsafe, or non-portable.
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
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "path",
            "invalid changed path",
        )
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or any(part.casefold() == ".git" for part in pure.parts)
    ):
        message = f"unsafe changed path: {path}"
        raise ReviewLoopError(EXIT_PRECONDITION, "path", message)
    return path


def _header_path(value: str, prefix: str) -> str | None:
    """Return one safe, prefix-stripped diff header path."""
    path = value.split("\t", 1)[0]
    if not path.startswith(prefix):
        return None
    try:
        return validate_path(path.removeprefix(prefix))
    except ReviewLoopError:
        return None


def _header_is_dev_null(value: str) -> bool:
    """Return whether a diff header explicitly names the absent-file sentinel."""
    return value.split("\t", 1)[0] == "/dev/null"


@dataclass(frozen=True)
class DiffFileAnalysis:
    """Structural facts from one canonical frozen-diff file section."""

    base_path: str
    old_sha: str
    new_sha: str
    byte_size: int
    line_count: int


@dataclass(frozen=True)
class FrozenDiffAnalysis:
    """Fail-closed structural analysis of the exact frozen unified diff."""

    anchors: frozenset[tuple[str, str, int]]
    files: dict[str, DiffFileAnalysis]


@dataclass
class _SectionState:
    """Mutable state for the one canonical structural diff pass."""

    byte_size: int = 0
    line_count: int = 0
    old_path: str | None = None
    head_path: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    old_line: int = 0
    new_line: int = 0
    remaining_old: int | None = None
    remaining_new: int | None = None
    anchors: set[tuple[str, str, int]] = field(default_factory=set)
    malformed: bool = False
    saw_old_header: bool = False
    saw_new_header: bool = False

    @property
    def in_hunk(self) -> bool:
        """Report whether a hunk still expects body lines."""
        return self.remaining_old is not None and self.remaining_new is not None


def _physical_line_count(value: bytes) -> int:
    """Count physical lines in exact bytes.

    Returns:
        The number of LF-delimited lines, including a final unterminated line.
    """
    return value.count(b"\n") + int(bool(value and not value.endswith(b"\n")))


def _finish_hunk_if_complete(state: _SectionState) -> None:
    """Clear hunk counters when both declared ranges are fully consumed."""
    if state.remaining_old == 0 and state.remaining_new == 0:
        state.remaining_old = None
        state.remaining_new = None


def _consume_hunk_body(line: str, state: _SectionState) -> bool:
    """Consume one exact hunk body line.

    Returns:
        Whether the line is a valid hunk body or no-newline marker.
    """
    if line == NO_NEWLINE_MARKER:
        return True
    marker = line[:1]
    if marker not in BODY_MARKERS:
        return False
    if state.remaining_old is None or state.remaining_new is None:
        return False

    old_delta = int(marker in {" ", "-"})
    new_delta = int(marker in {" ", "+"})
    if state.remaining_old < old_delta or state.remaining_new < new_delta:
        state.malformed = True
        return True

    if state.head_path is not None:
        if marker == "-" and state.old_line > 0:
            state.anchors.add((state.head_path, "LEFT", state.old_line))
        elif marker in {" ", "+"} and state.new_line > 0:
            state.anchors.add((state.head_path, "RIGHT", state.new_line))

    state.old_line += old_delta
    state.new_line += new_delta
    state.remaining_old -= old_delta
    state.remaining_new -= new_delta
    _finish_hunk_if_complete(state)
    return True


def _record_index(line: str, state: _SectionState) -> None:
    """Record one full-index header or mark the section malformed."""
    match = INDEX_RE.match(line)
    if match is None or state.old_sha is not None or state.new_sha is not None:
        state.malformed = True
        return
    state.old_sha, state.new_sha = match.groups()


def _record_old_header(line: str, state: _SectionState) -> None:
    """Record the base-side path header."""
    if state.saw_old_header:
        state.malformed = True
    state.saw_old_header = True
    value = line.removeprefix("--- ")
    if _header_is_dev_null(value):
        state.old_path = None
        return
    state.old_path = _header_path(value, "a/")
    if state.old_path is None:
        state.malformed = True


def _record_new_header(line: str, state: _SectionState) -> None:
    """Record the head-side path header and deletion fallback."""
    if state.saw_new_header:
        state.malformed = True
    state.saw_new_header = True
    value = line.removeprefix("+++ ")
    if _header_is_dev_null(value):
        state.head_path = state.old_path
        if state.old_path is None:
            state.malformed = True
        return
    state.head_path = _header_path(value, "b/")
    if state.head_path is None:
        state.malformed = True


def _start_hunk(line: str, state: _SectionState) -> None:
    """Start one declared hunk or fail the section closed."""
    match = HUNK_RE.match(line)
    if match is None or state.head_path is None:
        state.malformed = True
        return
    state.old_line = int(match.group(1))
    state.new_line = int(match.group(3))
    state.remaining_old = int(match.group(2) or "1")
    state.remaining_new = int(match.group(4) or "1")
    _finish_hunk_if_complete(state)


def _consume_section_line(line: str, state: _SectionState) -> None:
    """Consume one structural line within the current diff section."""
    if state.in_hunk:
        if _consume_hunk_body(line, state):
            return
        state.malformed = True
        state.remaining_old = None
        state.remaining_new = None

    if line == NO_NEWLINE_MARKER:
        return
    if line.startswith("index "):
        _record_index(line, state)
        return
    if line.startswith("--- "):
        _record_old_header(line, state)
        return
    if line.startswith("+++ "):
        _record_new_header(line, state)
        return
    if line.startswith("@@ "):
        _start_hunk(line, state)
        return
    if line[:1] in BODY_MARKERS or line.startswith("\\"):
        state.malformed = True


def _discard_path_anchors(
    anchors: set[tuple[str, str, int]],
    path: str,
) -> None:
    """Remove every previously accepted anchor for one duplicate path."""
    anchors.difference_update({anchor for anchor in anchors if anchor[0] == path})


def _finalize_diff_section(
    state: _SectionState | None,
    allowed_paths: frozenset[str],
    files: dict[str, DiffFileAnalysis],
    anchors: set[tuple[str, str, int]],
    invalid_paths: set[str],
) -> None:
    """Validate one completed section and merge its structural facts."""
    if state is None:
        return
    if state.in_hunk:
        state.malformed = True
    path = state.head_path
    if path is None or path not in allowed_paths or path in invalid_paths:
        return
    if (
        state.malformed
        or not state.saw_old_header
        or not state.saw_new_header
        or state.old_sha is None
        or state.new_sha is None
        or not SHA_RE.fullmatch(state.old_sha)
        or not SHA_RE.fullmatch(state.new_sha)
    ):
        return
    if path in files:
        files.pop(path, None)
        invalid_paths.add(path)
        _discard_path_anchors(anchors, path)
        return

    files[path] = DiffFileAnalysis(
        base_path=state.old_path or path,
        old_sha=state.old_sha,
        new_sha=state.new_sha,
        byte_size=state.byte_size,
        line_count=state.line_count,
    )
    if (
        state.byte_size < MAX_GITHUB_FILE_DIFF_BYTES
        and state.line_count < MAX_GITHUB_FILE_DIFF_LINES
    ):
        anchors.update(state.anchors)


def analyze_frozen_diff(
    patch: bytes,
    allowed_paths: frozenset[str],
) -> FrozenDiffAnalysis:
    """Derive all unified-diff structural facts in one primary pass.

    Returns:
        Exact inline anchors and per-file base-path, blob-SHA, and size facts.

    Raises:
        ReviewLoopError: The canonical patch is not valid UTF-8.
    """
    try:
        text = patch.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ReviewLoopError(
            EXIT_PRECONDITION,
            "bundle",
            "patch is not UTF-8",
        ) from exc

    files: dict[str, DiffFileAnalysis] = {}
    anchors: set[tuple[str, str, int]] = set()
    invalid_paths: set[str] = set()
    state: _SectionState | None = None
    lines = text.split("\n")

    for index, line in enumerate(lines):
        has_lf = index < len(lines) - 1
        if not has_lf and not line:
            break
        byte_size = len(line.encode("utf-8")) + int(has_lf)
        if line.startswith("diff --git "):
            _finalize_diff_section(
                state,
                allowed_paths,
                files,
                anchors,
                invalid_paths,
            )
            state = _SectionState(byte_size=byte_size, line_count=1)
            continue
        if state is None:
            continue
        state.byte_size += byte_size
        state.line_count += 1
        _consume_section_line(line, state)

    _finalize_diff_section(
        state,
        allowed_paths,
        files,
        anchors,
        invalid_paths,
    )
    if (
        len(patch) >= MAX_GITHUB_DIFF_BYTES
        or _physical_line_count(patch) >= MAX_GITHUB_DIFF_LINES
    ):
        anchors.clear()
    return FrozenDiffAnalysis(frozenset(anchors), files)


def parse_json_object(text: str, *, category: str) -> JsonObject:
    """Decode exactly one JSON object without repair or coercion.

    Returns:
        The decoded string-keyed JSON object.

    Raises:
        ReviewLoopError: GitHub returned malformed or non-object JSON.
    """
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewLoopError(
            EXIT_GITHUB,
            category,
            "GitHub returned malformed JSON",
        ) from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReviewLoopError(
            EXIT_GITHUB,
            category,
            "GitHub returned a non-object JSON response",
        )
    return cast("JsonObject", value)


def require_object(value: JsonValue | None, *, field: str) -> JsonObject:
    """Require a JSON object field.

    Returns:
        The validated object.

    Raises:
        ReviewLoopError: The field is not an object.
    """
    if not isinstance(value, dict):
        message = f"GitHub field {field} must be an object"
        raise ReviewLoopError(EXIT_GITHUB, "github_schema", message)
    return value


def require_string(
    value: JsonValue | None,
    *,
    field: str,
    allow_empty: bool = False,
) -> str:
    """Require a JSON string field.

    Returns:
        The validated string.

    Raises:
        ReviewLoopError: The field is not a permitted string.
    """
    if not isinstance(value, str) or (not allow_empty and not value):
        message = f"GitHub field {field} must be a string"
        raise ReviewLoopError(EXIT_GITHUB, "github_schema", message)
    return value


def require_integer(value: JsonValue | None, *, field: str) -> int:
    """Require a non-Boolean JSON integer field.

    Returns:
        The validated integer.

    Raises:
        ReviewLoopError: The field is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"GitHub field {field} must be an integer"
        raise ReviewLoopError(EXIT_GITHUB, "github_schema", message)
    return value


def require_boolean(value: JsonValue | None, *, field: str) -> bool:
    """Require a JSON Boolean field without coercion.

    Returns:
        The validated Boolean.

    Raises:
        ReviewLoopError: The field is not a Boolean.
    """
    if not isinstance(value, bool):
        message = f"GitHub field {field} must be a boolean"
        raise ReviewLoopError(EXIT_GITHUB, "github_schema", message)
    return value


def _optional_author_login(value: JsonValue | None, *, field: str) -> str:
    """Return one author's login, or an empty string for a deleted account."""
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
        """Bind immutable Git operations to one command runner and checkout."""
        self.runner = runner
        self.repo_dir = repo_dir

    def _git_text(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        max_output: int = 1024 * 1024,
    ) -> str:
        """Run one Git text command with the supplied environment.

        Returns:
            The command's decoded standard output.

        Raises:
            ReviewLoopError: The command fails or output is not valid UTF-8.
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                str(exc),
            ) from exc

    def _text(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        max_output: int = 24 * 1024 * 1024,
    ) -> str:
        """Run one authenticated GitHub CLI command and decode UTF-8 output.

        Returns:
            The command's decoded standard output.

        Raises:
            ReviewLoopError: The command fails or output is not valid UTF-8.
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
            raise ReviewLoopError(EXIT_GITHUB, "github", str(exc)) from exc

    def _initialize_repository(
        self,
        *,
        hardened: bool,
        require_push_url: bool,
    ) -> str:
        """Resolve and validate the local repository origin.

        Returns:
            The normalized repository identified by the fetch URL.

        Raises:
            ReviewLoopError: The local repository or its origin is invalid.
        """
        env = self._git_env() if hardened else self.runner.base_env()
        try:
            root = self._git_text(
                ["rev-parse", "--show-toplevel"],
                env=env,
            ).strip()
        except ReviewLoopError as exc:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                "cannot infer repository from local checkout",
            ) from exc
        if not root:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                "Git returned an empty repository root",
            )
        self.repo_dir = Path(root).resolve()
        fetch_repo = self._origin_repo(env=env, push=False)
        if require_push_url:
            push_repo = self._origin_repo(env=env, push=True)
            if fetch_repo.casefold() != push_repo.casefold():
                raise ReviewLoopError(
                    EXIT_PRECONDITION,
                    "repository",
                    "origin fetch and push URLs must identify the same repository",
                )
        return fetch_repo

    def _origin_repo(self, *, env: dict[str, str], push: bool) -> str:
        """Read exactly one GitHub.com origin fetch or push URL.

        Returns:
            The normalized repository identified by the requested URL.

        Raises:
            ReviewLoopError: The origin URL is missing, ambiguous, or invalid.
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
        except ReviewLoopError as exc:
            message = (
                "origin must have exactly one push URL"
                if push
                else "cannot infer repository from local checkout"
            )
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                message,
            ) from exc
        if len(urls) != 1 or not urls[0].strip():
            message = f"origin must have exactly one {kind} URL"
            raise ReviewLoopError(EXIT_PRECONDITION, "repository", message)
        return normalize_repo(urls[0])

    def _git_env(self) -> dict[str, str]:
        """Return a Git environment that ignores host-controlled config."""
        env = self.runner.allowlisted_env()
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
        })
        return env

    def git_bytes(
        self,
        args: list[str],
        *,
        max_output: int,
        input_text: str | None = None,
    ) -> bytes:
        """Run one hardened Git command and return exact bytes.

        Returns:
            Bounded command stdout.

        Raises:
            ReviewLoopError: Git execution failed.
        """
        try:
            return self.runner.run(
                ["git", *args],
                cwd=self.repo_dir,
                env=self._git_env(),
                max_output=max_output,
                input_text=input_text,
            ).stdout
        except CommandError as exc:
            raise ReviewLoopError(EXIT_PRECONDITION, "git", str(exc)) from exc

    def git_text(self, args: list[str], *, max_output: int) -> str:
        """Run one hardened Git command and decode strict UTF-8.

        Returns:
            Decoded command stdout.

        Raises:
            ReviewLoopError: Git fails or returns non-UTF-8 output.
        """
        try:
            return self.git_bytes(args, max_output=max_output).decode("utf-8", "strict")
        except UnicodeError as exc:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "Git returned non-UTF-8 output",
            ) from exc

    def ensure_commit_object(self, sha: str) -> None:
        """Require sha to identify a locally available commit object.

        Raises:
            ReviewLoopError: sha is unavailable or is not a commit object.
        """
        object_type = self.git_bytes(
            ["cat-file", "-t", sha],
            max_output=1024,
        ).decode("utf-8", "strict")
        if object_type.strip() != "commit":
            message = f"{sha} is not a commit object"
            raise ReviewLoopError(EXIT_PRECONDITION, "git", message)

    def tracked_paths_at(self, sha: str) -> tuple[str, ...]:
        """List all safe UTF-8 tracked paths at one immutable commit.

        Returns:
            Sorted unique tracked paths.

        Raises:
            ReviewLoopError: The tree contains unsafe, invalid, or duplicate paths.
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
                raise ReviewLoopError(
                    EXIT_PRECONDITION,
                    "path",
                    "Git tree returned a non-UTF-8 tracked path",
                ) from exc
            paths.append(validate_path(path))
        if len(paths) != len(set(paths)):
            raise ReviewLoopError(
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
        """Read one bounded immutable blob, or omit unsupported tree entries.

        Returns:
            Exact blob bytes, or None for missing, non-blob, or oversized content.

        Raises:
            ReviewLoopError: Git returns ambiguous or inconsistent metadata.
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned an ambiguous changed-file entry",
            )
        metadata, raw_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != LS_TREE_FIELD_COUNT:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned malformed changed-file metadata",
            )
        _mode, object_type, raw_object_sha, raw_size = fields
        try:
            listed_path = raw_path.decode("utf-8", "strict")
            object_sha = raw_object_sha.decode("ascii", "strict")
        except UnicodeError as exc:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "Git tree returned non-UTF-8 changed-file metadata",
            ) from exc
        if listed_path != path or not SHA_RE.fullmatch(object_sha):
            raise ReviewLoopError(
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "Git blob size changed during immutable evidence read",
            )
        return data

    def blob_is_binary(self, sha: str, *, max_output: int) -> bool:
        """Conservatively determine whether one immutable blob is binary.

        Returns:
            True when the blob is binary or cannot be read within the bound.
        """
        try:
            data = self.git_bytes(
                ["cat-file", "blob", sha],
                max_output=max_output,
            )
        except ReviewLoopError:
            return True
        return b"\0" in data

    def _check_attr_diff_isolated(
        self,
        sha: str,
        paths: frozenset[str],
        *,
        max_output: int,
    ) -> bytes:
        """Evaluate immutable `diff` attributes in an isolated bare repository.

        Returns:
            The bounded output from the isolated attribute-aware diff.
        """
        common_dir = Path(
            self
            .git_bytes(
                ["rev-parse", "--git-common-dir"],
                max_output=4096,
            )
            .decode("utf-8", "strict")
            .strip()
        )
        if not common_dir.is_absolute():
            common_dir = self.repo_dir / common_dir
        objects_dir = common_dir.resolve(strict=True) / "objects"
        with tempfile.TemporaryDirectory(prefix="pr-review-loop-attrs-") as tmp_dir:
            isolated_git_dir = Path(tmp_dir) / "attrs.git"
            self.git_bytes(
                [
                    "init",
                    "--quiet",
                    "--bare",
                    "--template=",
                    str(isolated_git_dir),
                ],
                max_output=4096,
            )
            alternates = isolated_git_dir / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(f"{objects_dir}\n", encoding="utf-8")
            return self.git_bytes(
                [
                    "--git-dir",
                    str(isolated_git_dir),
                    "-c",
                    "core.attributesFile=/dev/null",
                    "check-attr",
                    "--source",
                    sha,
                    "-z",
                    "--stdin",
                    "diff",
                ],
                max_output=max_output,
                input_text="".join(f"{path}\0" for path in paths),
            )

    def paths_with_diff_unset(
        self,
        sha: str,
        paths: frozenset[str],
        *,
        max_output: int,
    ) -> frozenset[str]:
        """Return paths whose immutable `diff` attribute is explicitly unset.

        Returns:
            Attribute-forced binary paths. Inspection failures conservatively
            return every candidate path.

        Raises:
            ReviewLoopError: Git emits malformed attribute records.
        """
        if not paths:
            return frozenset()
        try:
            output = self._check_attr_diff_isolated(
                sha,
                paths,
                max_output=max_output,
            )
        except (ReviewLoopError, OSError, UnicodeDecodeError):
            return frozenset(paths)
        fields = output.split(b"\0")
        if fields and fields[-1] == b"":
            fields = fields[:-1]
        if len(fields) % 3 != 0:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "git",
                "git check-attr returned a malformed -z record",
            )
        unset: set[str] = set()
        for index in range(0, len(fields), 3):
            path_bytes, _attribute, value = fields[index : index + 3]
            if value != b"unset":
                continue
            try:
                unset.add(path_bytes.decode("utf-8", "strict"))
            except UnicodeDecodeError as exc:
                raise ReviewLoopError(
                    EXIT_PRECONDITION,
                    "git",
                    "git check-attr returned a non-UTF-8 path",
                ) from exc
        return frozenset(unset)


class GitHubClient(_ImmutableGitMixin):
    """Read PR snapshots and post reviews through trusted CLI commands."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        """Initialize a client before target identity is resolved."""
        super().__init__(runner, repo_dir.resolve())
        self.repository = ""
        self.number = 0
        self.url = ""
        self.authenticated_login = ""

    def initialize(self, pr_value: str) -> None:
        """Bind ordinary review operations to one exact pull request.

        Raises:
            ReviewLoopError: Repository or authenticated identity validation fails.
        """
        origin_repo = self._initialize_repository(
            hardened=True,
            require_push_url=False,
        )
        self._set_target(pr_value, origin_repo)
        self.authenticated_login = self._text([
            "api",
            "--hostname",
            "github.com",
            "user",
            "--jq",
            ".login",
        ]).strip()
        if not self.authenticated_login:
            raise ReviewLoopError(
                EXIT_GITHUB,
                "identity",
                "authenticated GitHub login was empty",
            )

    def initialize_for_submit(self, pr_value: str) -> None:
        """Bind submit operations and require one matching origin push URL."""
        origin_repo = self._initialize_repository(
            hardened=False,
            require_push_url=True,
        )
        self._set_target(pr_value, origin_repo)

    def _set_target(self, pr_value: str, origin_repo: str) -> None:
        """Resolve the target and bind it to the local origin.

        Raises:
            ReviewLoopError: The target does not match the local origin.
        """
        self.repository, self.number, self.url = resolve_target(
            pr_value,
            origin_repo,
        )
        if origin_repo.casefold() != self.repository.casefold():
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match pull request repository",
            )

    def snapshot(self, *, require_open: bool = True) -> PullRequest:
        """Read and validate one complete immutable PR snapshot.

        Returns:
            The exact validated pull-request snapshot.

        Raises:
            ReviewLoopError: GitHub identity, state, or inventory is invalid.
        """
        data = self._pull_request_data(PR_FIELDS)
        identity = self._parse_identity(data, require_open=require_open)
        files_value = data.get("files")
        if not isinstance(files_value, list):
            raise ReviewLoopError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub field files must be an array",
            )
        paths = self._changed_paths(files_value)
        advertised_count = require_integer(
            data.get("changedFiles"),
            field="changedFiles",
        )
        if advertised_count < 0 or advertised_count != len(paths):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "inventory",
                "GitHub changed-file inventory was truncated or inconsistent",
            )
        if len(paths) != len(set(paths)):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "path",
                "duplicate changed paths",
            )
        author = require_object(data.get("author"), field="author")
        author_login = require_string(
            author.get("login"),
            field="author.login",
        )
        return PullRequest(
            repository=identity.repository,
            number=identity.number,
            url=identity.url,
            title=require_string(
                data.get("title"),
                field="title",
                allow_empty=True,
            ),
            body=require_string(
                data.get("body"),
                field="body",
                allow_empty=True,
            ),
            author=author_login,
            state=identity.state,
            is_draft=identity.is_draft,
            base_ref=identity.base_ref,
            base_sha=identity.base_sha,
            head_ref=identity.head_ref,
            head_sha=identity.head_sha,
            head_repository=identity.head_repository,
            changed_paths=tuple(sorted(paths)),
        )

    @staticmethod
    def _changed_paths(files_value: list[JsonValue]) -> list[str]:
        """Validate the complete GitHub changed-file inventory.

        Returns:
            The validated changed paths.

        Raises:
            ReviewLoopError: A changed-file entry or path is invalid.
        """
        paths: list[str] = []
        for item in files_value:
            if not isinstance(item, dict):
                raise ReviewLoopError(
                    EXIT_GITHUB,
                    "github_schema",
                    "GitHub changed-file entry must be an object",
                )
            path = require_string(item.get("path"), field="files.path")
            paths.append(validate_path(path))
        return paths

    def identity_snapshot(
        self,
        *,
        require_open: bool = True,
    ) -> PullRequestIdentity:
        """Read and validate only the PR identity and ref snapshot.

        Returns:
            The validated immutable identity snapshot.
        """
        data = self._pull_request_data(PR_IDENTITY_FIELDS)
        return self._parse_identity(data, require_open=require_open)

    def _pull_request_data(self, fields: str) -> JsonObject:
        """Fetch one bounded PR JSON object from GitHub CLI.

        Returns:
            The decoded PR JSON object.
        """
        output = self._text(
            ["pr", "view", self.url, "--json", fields],
            max_output=8 * 1024 * 1024,
        )
        return parse_json_object(output, category="github_schema")

    def _parse_identity(
        self,
        data: JsonObject,
        *,
        require_open: bool,
    ) -> PullRequestIdentity:
        """Parse and validate the ref-bound identity fields from GitHub.

        Returns:
            The validated immutable identity snapshot.
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
                head_owner.get("login"),
                field="headRepositoryOwner.login",
            )
            name = require_string(
                head_repository.get("name"),
                field="headRepository.name",
            )
            head_repo = f"{owner}/{name}"
        identity = PullRequestIdentity(
            repository=self.repository,
            number=require_integer(data.get("number"), field="number"),
            url=require_string(data.get("url"), field="url"),
            state=require_string(data.get("state"), field="state"),
            is_draft=require_boolean(data.get("isDraft"), field="isDraft"),
            base_ref=require_string(
                data.get("baseRefName"),
                field="baseRefName",
            ),
            base_sha=require_string(
                data.get("baseRefOid"),
                field="baseRefOid",
            ),
            head_ref=require_string(
                data.get("headRefName"),
                field="headRefName",
            ),
            head_sha=require_string(
                data.get("headRefOid"),
                field="headRefOid",
            ),
            head_repository=head_repo,
        )
        self._validate_snapshot_identity(identity, require_open=require_open)
        return identity

    def _validate_snapshot_identity(
        self,
        pull_request: PullRequestIdentity,
        *,
        require_open: bool,
    ) -> None:
        """Enforce exact target, state, repository, SHA, and ref invariants.

        Raises:
            ReviewLoopError: An identity, state, repository, SHA, or ref
                invariant is violated.
        """
        if (
            pull_request.number != self.number
            or pull_request.url.rstrip("/").lower() != self.url.lower()
        ):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "identity",
                "ambiguous pull request identity",
            )
        if require_open and (pull_request.state != "OPEN" or pull_request.is_draft):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "state",
                "pull request must be open and non-draft",
            )
        if pull_request.head_repository.lower() != self.repository.lower():
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                "fork pull requests are not supported",
            )
        if not SHA_RE.fullmatch(pull_request.base_sha) or not SHA_RE.fullmatch(
            pull_request.head_sha
        ):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "sha",
                "invalid base or head SHA",
            )
        validate_ref(pull_request.base_ref)
        validate_ref(pull_request.head_ref)

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        """Map a canonical verdict to the GitHub review event.

        Returns:
            COMMENT for self-review, otherwise the canonical formal verdict.

        Raises:
            ReviewLoopError: verdict is not APPROVE or REQUEST_CHANGES.
        """
        if verdict not in {"APPROVE", "REQUEST_CHANGES"}:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "verdict",
                "invalid review verdict",
            )
        if pull_request.author.casefold() == self.authenticated_login.casefold():
            return "COMMENT"
        return verdict

    def ensure_objects(self, pull_request: PullRequest) -> None:
        """Require the frozen base and head commits locally."""
        for sha in (pull_request.base_sha, pull_request.head_sha):
            self.ensure_commit_object(sha)

    def changed_file_bytes(
        self,
        pull_request: PullRequest,
        path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Read one changed path from the frozen head tree.

        Returns:
            Bounded blob bytes, or None when the path is not attachable.
        """
        return self.blob_bytes_at(
            pull_request.head_sha,
            path,
            max_output=max_output,
        )

    def patch(self, pull_request: PullRequest, *, max_output: int) -> bytes:
        """Read the exact base-to-head patch in pinned canonical Git form.

        Returns:
            The bounded raw unified diff.
        """
        return self.git_bytes(
            [
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.quotePath=false",
                "-c",
                "diff.suppressBlankEmpty=false",
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--full-index",
                "--find-renames",
                "-l0",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--unified=3",
                "--inter-hunk-context=0",
                "--diff-algorithm=myers",
                "--indent-heuristic",
                f"{pull_request.base_sha}...{pull_request.head_sha}",
            ],
            max_output=max_output,
        )

    @staticmethod
    def _base_paths_by_head(
        analysis: FrozenDiffAnalysis,
        candidate_paths: frozenset[str],
    ) -> dict[str, list[str]]:
        """Group candidate head paths by their parsed immutable base path.

        Returns:
            Candidate head paths grouped by immutable base path.
        """
        grouped: dict[str, list[str]] = {}
        for head_path in candidate_paths:
            file_analysis = analysis.files.get(head_path)
            if file_analysis is None:
                continue
            grouped.setdefault(file_analysis.base_path, []).append(head_path)
        return grouped

    def _attribute_forced_paths(
        self,
        pull_request: PullRequest,
        analysis: FrozenDiffAnalysis,
        candidate_paths: frozenset[str],
    ) -> frozenset[str]:
        """Map immutable base/head `-diff` attributes to head paths.

        Returns:
            Head paths whose immutable attributes force review treatment.
        """
        grouped = self._base_paths_by_head(analysis, candidate_paths)
        mapped_count = sum(len(values) for values in grouped.values())
        if mapped_count != len(candidate_paths):
            return candidate_paths
        base_forced = self.paths_with_diff_unset(
            pull_request.base_sha,
            frozenset(grouped),
            max_output=MAX_ANCHOR_ATTR_BYTES,
        )
        forced = {
            head_path for base_path in base_forced for head_path in grouped[base_path]
        }
        forced.update(
            self.paths_with_diff_unset(
                pull_request.head_sha,
                candidate_paths,
                max_output=MAX_ANCHOR_ATTR_BYTES,
            )
        )
        return frozenset(forced)

    def _nonbinary_anchors(
        self,
        analysis: FrozenDiffAnalysis,
        attribute_forced: frozenset[str],
    ) -> frozenset[tuple[str, str, int]]:
        """Filter parsed anchors through immutable blob binary checks.

        Returns:
            Anchors belonging to verified non-binary blobs.
        """
        binary_by_sha: dict[str, bool] = {}
        verified: set[tuple[str, str, int]] = set()
        for path, side, line in analysis.anchors:
            file_analysis = analysis.files.get(path)
            if file_analysis is None or path in attribute_forced:
                continue
            sha = file_analysis.new_sha if side == "RIGHT" else file_analysis.old_sha
            if sha == NULL_SHA:
                is_binary = False
            else:
                if sha not in binary_by_sha:
                    binary_by_sha[sha] = self.blob_is_binary(
                        sha,
                        max_output=MAX_ANCHOR_BLOB_BYTES,
                    )
                is_binary = binary_by_sha[sha]
            if not is_binary:
                verified.add((path, side, line))
        return frozenset(verified)

    def diff_anchors(
        self,
        pull_request: PullRequest,
    ) -> frozenset[tuple[str, str, int]]:
        """Validate exact frozen-diff anchors for GitHub inline review.

        Returns:
            Anchors proven by the canonical patch, attributes, and blob contents.
        """
        analysis = analyze_frozen_diff(
            self.patch(
                pull_request,
                max_output=MAX_ANCHOR_PATCH_BYTES,
            ),
            frozenset(pull_request.changed_paths),
        )
        if not analysis.anchors:
            return frozenset()
        candidate_paths = frozenset(path for path, _side, _line in analysis.anchors)
        forced = self._attribute_forced_paths(
            pull_request,
            analysis,
            candidate_paths,
        )
        return self._nonbinary_anchors(analysis, forced)

    def tracked_paths(self, pull_request: PullRequest) -> tuple[str, ...]:
        """List all safe paths in the frozen head tree.

        Returns:
            Sorted unique tracked paths.
        """
        return self.tracked_paths_at(pull_request.head_sha)

    def post_review(
        self,
        pull_request: PullRequest,
        event: str,
        body: str,
        comments: tuple[ReviewComment, ...] = (),
    ) -> tuple[int, JsonObject]:
        """Atomically create one commit-bound GitHub review.

        Returns:
            Created review ID and response object.

        Raises:
            ReviewLoopError: The request is oversized or GitHub anchors it wrongly.
        """
        payload: JsonObject = {
            "commit_id": pull_request.head_sha,
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = [comment.as_payload() for comment in comments]
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > MAX_INPUT:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "input",
                "serialized review request exceeds the command input bound",
            )
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
                input_text=serialized,
            ),
            category="github_schema",
        )
        review_id = require_integer(data.get("id"), field="id")
        commit_id = require_string(data.get("commit_id"), field="commit_id")
        if review_id <= 0 or commit_id != pull_request.head_sha:
            if review_id > 0 and event != "COMMENT":
                self.dismiss(pull_request, review_id)
            raise ReviewLoopError(
                EXIT_RACE,
                "race",
                "GitHub did not anchor the review to the expected head",
            )
        return review_id, data

    def dismiss(self, pull_request: PullRequest, review_id: int) -> None:
        """Dismiss one stale formal GitHub review."""
        payload: JsonObject = {
            "message": ("Dismissed automatically: reviewed PR snapshot became stale.")
        }
        self._text(
            [
                "api",
                "--hostname",
                "github.com",
                (
                    f"repos/{pull_request.repository}/pulls/"
                    f"{pull_request.number}/reviews/{review_id}/dismissals"
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
        """Re-read and validate an exact published review.

        Returns:
            The validated GitHub review response.

        Raises:
            ReviewLoopError: The published identity, author, commit, or body differs.
        """
        data = parse_json_object(
            self._text([
                "api",
                "--hostname",
                "github.com",
                (
                    f"repos/{pull_request.repository}/pulls/"
                    f"{pull_request.number}/reviews/{review_id}"
                ),
            ]),
            category="github_schema",
        )
        user = require_object(data.get("user"), field="user")
        login = require_string(user.get("login"), field="user.login")
        if (
            require_integer(data.get("id"), field="id") != review_id
            or login.casefold() != self.authenticated_login.casefold()
            or require_string(data.get("commit_id"), field="commit_id")
            != pull_request.head_sha
            or require_string(data.get("body"), field="body") != body
        ):
            raise ReviewLoopError(
                EXIT_GITHUB,
                "github",
                "posted review revalidation failed",
            )
        return data

    @staticmethod
    def same_snapshot(
        first: PullRequest | PullRequestIdentity,
        second: PullRequest | PullRequestIdentity,
    ) -> bool:
        """Compare only the frozen base/head identity.

        Returns:
            Whether both snapshots bind to the same base and head SHAs.
        """
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha


def _bounded_comments(value: list[JsonValue]) -> tuple[JsonObject, ...]:
    """Validate, order, and bound one Issue's comment collection.

    Returns:
        The newest bounded comments with oversized bodies explicitly omitted.

    Raises:
        ReviewLoopError: A GitHub comment entry has an invalid shape.
    """
    parsed: list[tuple[str, str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ReviewLoopError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub comment entry must be an object",
            )
        parsed.append((
            _optional_author_login(
                item.get("author"),
                field="comments.author",
            ),
            require_string(
                item.get("body"),
                field="comments.body",
                allow_empty=True,
            ),
            require_string(
                item.get("createdAt"),
                field="comments.createdAt",
            ),
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
    return tuple(
        {
            "author": author,
            "body": "" if omitted else body,
            "created_at": created_at,
            "omitted": omitted,
        }
        for (author, body, created_at), omitted in zip(
            kept,
            omitted_flags,
            strict=True,
        )
    )


class IssueClient(_ImmutableGitMixin):
    """Read Issue snapshots and repository base identity for bootstrap."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        """Initialize an Issue client before target identity is resolved."""
        super().__init__(runner, repo_dir.resolve())
        self.repository = ""
        self.number = 0
        self.url = ""

    def initialize(self, issue_value: str) -> None:
        """Bind bootstrap operations to one exact same-repository Issue.

        Raises:
            ReviewLoopError: Repository or Issue target validation fails.
        """
        origin_repo = self._initialize_repository(
            hardened=True,
            require_push_url=False,
        )
        self.repository, self.number, self.url = resolve_issue_target(
            issue_value,
            origin_repo,
        )
        if origin_repo.casefold() != self.repository.casefold():
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "repository",
                "local origin does not match issue repository",
            )

    def snapshot(self) -> IssueSnapshot:
        """Read and validate one bounded open Issue snapshot.

        Returns:
            The exact Issue snapshot and bounded newest comments.

        Raises:
            ReviewLoopError: GitHub identity, state, schema, or safety checks fail.
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
            repository_data.get("issue"),
            field="data.repository.issue",
        )
        comments_field = require_object(data.get("comments"), field="comments")
        comments_value = comments_field.get("nodes")
        if not isinstance(comments_value, list):
            raise ReviewLoopError(
                EXIT_GITHUB,
                "github_schema",
                "GitHub field comments.nodes must be an array",
            )
        body = require_string(
            data.get("body"),
            field="body",
            allow_empty=True,
        )
        if len(body.encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "bundle",
                "issue body exceeds bound",
            )
        issue = IssueSnapshot(
            repository=self.repository,
            number=require_integer(data.get("number"), field="number"),
            url=require_string(data.get("url"), field="url"),
            title=require_string(
                data.get("title"),
                field="title",
                allow_empty=True,
            ),
            body=body,
            author=_optional_author_login(data.get("author"), field="author"),
            state=require_string(data.get("state"), field="state"),
            updated_at=require_string(
                data.get("updatedAt"),
                field="updatedAt",
            ),
            comments=_bounded_comments(comments_value),
        )
        self._validate_issue_identity(issue)
        self._validate_content_safety(issue)
        return issue

    def _validate_issue_identity(self, issue: IssueSnapshot) -> None:
        """Require the Issue response to match the exact requested target.

        Raises:
            ReviewLoopError: The Issue identity or state is invalid.
        """
        if (
            issue.number != self.number
            or issue.url.rstrip("/").lower() != self.url.lower()
        ):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "identity",
                "ambiguous issue identity",
            )
        if issue.state != "OPEN":
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "state",
                "issue must be open",
            )

    def _validate_content_safety(self, issue: IssueSnapshot) -> None:
        """Reject known credentials from untrusted Issue evidence.

        Raises:
            ReviewLoopError: Issue content contains a known credential.
        """
        texts = [issue.title, issue.body]
        for comment in issue.comments:
            body = comment.get("body")
            if isinstance(body, str):
                texts.append(body)
        if any(self.runner.contains_secret(text) for text in texts):
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "credentials",
                "issue content contains a known credential",
            )

    def default_branch(self) -> str:
        """Read the repository's current default branch.

        Returns:
            A validated safe branch name.
        """
        data = parse_json_object(
            self._text([
                "repo",
                "view",
                self.repository,
                "--json",
                "defaultBranchRef",
            ]),
            category="github_schema",
        )
        ref = require_object(
            data.get("defaultBranchRef"),
            field="defaultBranchRef",
        )
        branch = require_string(
            ref.get("name"),
            field="defaultBranchRef.name",
        )
        validate_ref(branch)
        return branch

    def branch_sha(self, branch: str) -> str:
        """Read one current GitHub branch SHA.

        Returns:
            The validated full lowercase commit SHA.

        Raises:
            ReviewLoopError: GitHub returns an invalid commit SHA.
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
            raise ReviewLoopError(
                EXIT_PRECONDITION,
                "sha",
                "invalid base SHA",
            )
        return sha
