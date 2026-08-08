"""Focused regression tests for immutable GitHub and Git evidence safety."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise Git directly
from typing import TYPE_CHECKING, cast

import pytest
from scripts.artifacts import TemporaryFileWriter
from scripts.github import (
    MAX_ISSUE_BODY_BYTES,
    MAX_ISSUE_COMMENT_BYTES,
    MAX_ISSUE_COMMENTS,
    MAX_ISSUE_COMMENTS_TOTAL_BYTES,
    GitHubClient,
    IssueClient,
    resolve_issue_target,
)
from scripts.models import EXIT_PRECONDITION, JsonObject, LooprError, PullRequest
from scripts.oracle import OracleClient
from scripts.process import CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def _git(
    git: str,
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> str:
    """Run one test-controlled Git command and return stripped stdout."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed test argv
        [git, *args],
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _sample_pr(
    base_sha: str,
    head_sha: str,
    paths: tuple[str, ...] = ("file.py",),
) -> PullRequest:
    """Return one valid frozen pull-request snapshot for local Git tests."""
    return PullRequest(
        repository="owner/repository",
        number=21,
        url="https://github.com/owner/repository/pull/21",
        title="Title",
        body="Body",
        author="author",
        state="OPEN",
        is_draft=False,
        base_ref="main",
        base_sha=base_sha,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=paths,
        raw={},
    )


def test_review_event_uses_comment_only_for_self_authored_prs(tmp_path: Path) -> None:
    """Self-authored PRs use comments while other authors retain formal events."""
    client = GitHubClient(CommandRunner(), tmp_path)
    pull_request = _sample_pr("a" * 40, "b" * 40)

    client.authenticated_login = pull_request.author
    assert client.review_event(pull_request, "APPROVE") == "COMMENT"
    assert client.review_event(pull_request, "REQUEST_CHANGES") == "COMMENT"

    client.authenticated_login = "another-user"
    assert client.review_event(pull_request, "APPROVE") == "APPROVE"
    assert client.review_event(pull_request, "REQUEST_CHANGES") == "REQUEST_CHANGES"


def test_verify_posted_checks_actor_commit_and_body_without_formal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Publication verification ignores formal state but binds all other data."""
    client = GitHubClient(CommandRunner(), tmp_path)
    pull_request = _sample_pr("a" * 40, "b" * 40)
    body = "review body\n\nReviewed head: `" + pull_request.head_sha + "`"
    client.authenticated_login = "author"
    response = {
        "id": 123,
        "user": {"login": "author"},
        "commit_id": pull_request.head_sha,
        "body": body,
        "state": "CHANGES_REQUESTED",
    }

    def fake_text(_args: list[str], **_kwargs: object) -> str:
        return json.dumps(response)

    monkeypatch.setattr(client, "_text", fake_text)

    assert client.verify_posted(pull_request, 123, body) == response


@pytest.mark.parametrize("mismatch", ["id", "actor", "commit_id", "body"])
def test_verify_posted_rejects_each_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mismatch: str,
) -> None:
    """Every post-write identity field is independently fail-closed."""
    client = GitHubClient(CommandRunner(), tmp_path)
    pull_request = _sample_pr("a" * 40, "b" * 40)
    body = "review body"
    client.authenticated_login = "author"
    response: JsonObject = {
        "id": 123,
        "user": {"login": "author"},
        "commit_id": pull_request.head_sha,
        "body": body,
        "state": "COMMENTED",
    }
    if mismatch == "id":
        response["id"] = 456
    elif mismatch == "actor":
        response["user"] = {"login": "other-user"}
    elif mismatch == "commit_id":
        response["commit_id"] = "c" * 40
    else:
        response["body"] = "different body"

    def fake_text(_args: list[str], **_kwargs: object) -> str:
        return json.dumps(response)

    monkeypatch.setattr(client, "_text", fake_text)

    with pytest.raises(LooprError, match="posted review revalidation failed"):
        client.verify_posted(pull_request, 123, body)


def test_post_review_publishes_selected_event_and_exact_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The GitHub payload carries the selected event and frozen review body."""
    client = GitHubClient(CommandRunner(), tmp_path)
    pull_request = _sample_pr("a" * 40, "b" * 40)
    body = "review body"
    captured: dict[str, JsonObject] = {}

    def fake_text(
        _args: list[str],
        *,
        input_text: str | None = None,
        **_kwargs: object,
    ) -> str:
        assert input_text is not None
        captured["payload"] = json.loads(input_text)
        return json.dumps({"id": 123, "commit_id": pull_request.head_sha})

    monkeypatch.setattr(client, "_text", fake_text)

    review_id, _posted = client.post_review(pull_request, "COMMENT", body)

    assert review_id == 123
    assert captured["payload"] == {
        "commit_id": pull_request.head_sha,
        "body": body,
        "event": "COMMENT",
    }


def _repo_with_two_commits(tmp_path: Path) -> tuple[str, Path, str, str]:
    """Create a repository with distinct base and head blob contents."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["config", "user.email", "test@example.com"], cwd=repo)
    _git(git, ["config", "user.name", "Test"], cwd=repo)
    (repo / "file.py").write_text("base\n")
    _git(git, ["add", "file.py"], cwd=repo)
    _git(git, ["commit", "-q", "-m", "base"], cwd=repo)
    base = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    (repo / "file.py").write_text("expected\n")
    _git(git, ["commit", "-q", "-am", "head"], cwd=repo)
    head = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    return git, repo, base, head


def test_git_reads_ignore_replace_refs_and_injected_controls(tmp_path: Path) -> None:
    """Replace refs and inherited Git controls cannot redirect evidence reads."""
    git, repo, base, head = _repo_with_two_commits(tmp_path)
    malicious_blob = _git(
        git,
        ["hash-object", "-w", "--stdin"],
        cwd=repo,
        input_text="attacker\n",
    )
    malicious_tree = _git(
        git,
        ["mktree"],
        cwd=repo,
        input_text=f"100644 blob {malicious_blob}\tfile.py\n",
    )
    malicious_commit = _git(
        git,
        ["commit-tree", malicious_tree, "-p", base, "-m", "malicious"],
        cwd=repo,
    )
    _git(git, ["replace", head, malicious_commit], cwd=repo)

    runner = CommandRunner({
        **os.environ,
        "GIT_DIR": str(tmp_path / "redirected.git"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "redirected-worktree"),
        "GIT_NO_REPLACE_OBJECTS": "0",
    })
    client = GitHubClient(runner, repo)

    data = client.changed_file_bytes(
        _sample_pr(base, head),
        "file.py",
        max_output=1024,
    )

    assert data == b"expected\n"


def test_changed_file_bytes_returns_none_for_deleted_path(tmp_path: Path) -> None:
    """A path absent from the frozen head is an explicit omission."""
    git, repo, base, _head = _repo_with_two_commits(tmp_path)
    (repo / "file.py").unlink()
    _git(git, ["commit", "-q", "-am", "delete"], cwd=repo)
    deleted_head = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    client = GitHubClient(CommandRunner(), repo)

    assert (
        client.changed_file_bytes(
            _sample_pr(base, deleted_head),
            "file.py",
            max_output=1024,
        )
        is None
    )


class _FailingGitHub:
    """Provide valid bundle inputs but fail the changed-file Git read."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    @staticmethod
    def patch(_pull_request: PullRequest, *, max_output: int) -> bytes:
        """Return a minimal valid UTF-8 patch."""
        del max_output
        return b"diff --git a/file.py b/file.py\n"

    @staticmethod
    def tracked_paths(_pull_request: PullRequest) -> tuple[str, ...]:
        """Return the changed path as a tracked head path."""
        return ("file.py",)

    @staticmethod
    def changed_file_bytes(
        _pull_request: PullRequest,
        _path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Inject an unexpected Git failure rather than an omission."""
        del max_output
        raise LooprError(EXIT_PRECONDITION, "git", "injected git failure")


def test_generic_git_failure_aborts_bundle_construction(tmp_path: Path) -> None:
    """Unexpected Git failures cannot be converted into omission evidence."""
    runner = CommandRunner()
    writer = TemporaryFileWriter(tmp_path / "oracle", runner)
    github = _FailingGitHub(tmp_path)
    oracle = OracleClient(
        runner,
        cast("GitHubClient", github),
        writer,
        "heavy",
    )
    pull_request = _sample_pr("a" * 40, "b" * 40)

    with pytest.raises(LooprError, match="injected git failure"):
        oracle.build_bundle(pull_request)


def _repo_with_origin(tmp_path: Path, origin_url: str) -> Path:
    """Create an empty Git repository with a configured origin remote."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "issue-repo"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["remote", "add", "origin", origin_url], cwd=repo)
    return repo


def _issue_payload(
    *,
    number: int = 42,
    url: str = "https://github.com/acme/demo/issues/42",
    state: str = "OPEN",
    title: str = "Title",
    body: str = "Body",
    author: str | None = "author",
    updated_at: str = "2026-01-01T00:00:00Z",
    comments: list[JsonObject] | None = None,
) -> JsonObject:
    """Return one valid GraphQL Issue response payload."""
    return {
        "number": number,
        "url": url,
        "state": state,
        "title": title,
        "body": body,
        "author": None if author is None else {"login": author},
        "updatedAt": updated_at,
        "comments": [] if comments is None else list(comments),
    }


class FakeIssueGh(CommandRunner):
    """Fake GitHub responses while running real local Git.

    The fake returns its configured comments regardless of the requested
    GraphQL window. Tests for the outbound bound inspect `gh_calls` directly;
    the oversized response tests intentionally exercise local defenses too.
    """

    def __init__(
        self,
        *,
        issue: JsonObject,
        default_branch: str = "main",
        branch_sha: str = "a" * 40,
    ) -> None:
        """Initialize one fake Issue-bootstrap GitHub CLI transport."""
        super().__init__()
        self.issue = issue
        self.default_branch_name = default_branch
        self.branch_sha_value = branch_sha
        self.gh_calls: list[tuple[str, ...]] = []

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
        """Fake `gh` reads and delegate every other command to real Git."""
        argv = tuple(str(value) for value in args)
        if argv and argv[0] == "gh":
            self.gh_calls.append(argv)
            if argv[1] == "repo":
                payload = {"defaultBranchRef": {"name": self.default_branch_name}}
                return CommandResult(argv, 0, json.dumps(payload).encode(), "")
            if argv[1] == "api" and "graphql" in argv:
                issue_payload = dict(self.issue)
                issue_payload["comments"] = {
                    "nodes": issue_payload.get("comments", []),
                }
                envelope = {"data": {"repository": {"issue": issue_payload}}}
                return CommandResult(argv, 0, json.dumps(envelope).encode(), "")
            if argv[1] == "api":
                payload = {"commit": {"sha": self.branch_sha_value}}
                return CommandResult(argv, 0, json.dumps(payload).encode(), "")
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )


def test_resolve_issue_target_accepts_numeric_with_origin() -> None:
    """A numeric target resolves against the unambiguous local origin."""
    repository, number, url = resolve_issue_target("42", "acme/demo")

    assert (repository, number, url) == (
        "acme/demo",
        42,
        "https://github.com/acme/demo/issues/42",
    )


def test_resolve_issue_target_rejects_numeric_without_origin() -> None:
    """A numeric target requires an unambiguous local origin."""
    with pytest.raises(LooprError) as captured:
        resolve_issue_target("42", None)

    assert captured.value.category == "input"


def test_resolve_issue_target_parses_canonical_url() -> None:
    """A canonical Issue URL resolves without needing a local origin."""
    repository, number, url = resolve_issue_target(
        "https://github.com/acme/demo/issues/42",
        None,
    )

    assert (repository, number, url) == (
        "acme/demo",
        42,
        "https://github.com/acme/demo/issues/42",
    )


def test_resolve_issue_target_rejects_pull_url() -> None:
    """A pull-request URL is not an Issue target."""
    with pytest.raises(LooprError) as captured:
        resolve_issue_target("https://github.com/acme/demo/pull/42", None)

    assert captured.value.category == "input"


def test_resolve_issue_target_rejects_zero() -> None:
    """A zero Issue number is not positive."""
    with pytest.raises(LooprError) as captured:
        resolve_issue_target("0", "acme/demo")

    assert captured.value.category == "input"


def test_issue_client_snapshot_maps_fields(tmp_path: Path) -> None:
    """A valid open Issue snapshot maps every field from the GitHub response."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert snapshot.repository == "acme/demo"
    assert snapshot.number == 42
    assert snapshot.state == "OPEN"
    assert snapshot.title == "Title"
    assert snapshot.body == "Body"
    assert snapshot.author == "author"
    assert snapshot.updated_at == "2026-01-01T00:00:00Z"
    assert snapshot.comments == ()


def test_issue_client_snapshot_requests_bounded_comment_window(
    tmp_path: Path,
) -> None:
    """The GitHub transport requests only the newest bounded comment window."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, repo)
    client.initialize("42")

    client.snapshot()

    graphql_calls = [
        call
        for call in runner.gh_calls
        if call[1:5] == ("api", "--hostname", "github.com", "graphql")
    ]
    assert len(graphql_calls) == 1
    call = graphql_calls[0]
    assert "--paginate" not in call
    assert "view" not in call
    query = next(argument for argument in call if argument.startswith("query="))
    assert "$lastComments: Int!" in query
    assert "comments(last: $lastComments)" in query
    assert "comments(first:" not in query
    typed_values = [
        call[index + 1] for index, argument in enumerate(call[:-1]) if argument == "-F"
    ]
    assert f"lastComments={MAX_ISSUE_COMMENTS}" in typed_values


def test_issue_client_snapshot_accepts_null_author(tmp_path: Path) -> None:
    """A deleted Issue author (`"author": null`) maps to an empty login."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(author=None))
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert snapshot.author == ""


def test_issue_client_snapshot_accepts_null_author_login(tmp_path: Path) -> None:
    """An author object with a null login also maps to an empty login."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    payload = _issue_payload()
    payload["author"] = {"login": None}
    runner = FakeIssueGh(issue=payload)
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert snapshot.author == ""


def test_issue_client_rejects_closed_issue(tmp_path: Path) -> None:
    """A closed Issue is rejected before it reaches the bootstrap bundle."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(state="CLOSED"))
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.snapshot()

    assert captured.value.category == "state"


def test_issue_client_rejects_pull_request_identity(tmp_path: Path) -> None:
    """A number that names a pull request, not an Issue, fails identity."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    payload = _issue_payload(url="https://github.com/acme/demo/pull/42")
    runner = FakeIssueGh(issue=payload)
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.snapshot()

    assert captured.value.category == "identity"


def test_issue_client_rejects_repository_mismatch(tmp_path: Path) -> None:
    """A canonical Issue URL cannot redirect bootstrap to another repository."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, repo)

    with pytest.raises(LooprError) as captured:
        client.initialize("https://github.com/other/repo/issues/42")

    assert captured.value.category == "repository"


def test_issue_client_numeric_requires_local_origin(tmp_path: Path) -> None:
    """A numeric Issue target cannot be resolved outside a Git checkout."""
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, tmp_path)

    with pytest.raises(LooprError) as captured:
        client.initialize("42")

    assert captured.value.category == "repository"


def test_issue_client_url_requires_local_origin_outside_git_checkout(
    tmp_path: Path,
) -> None:
    """A canonical Issue URL cannot bypass the matching-origin check either.

    Bootstrap hands the returned base commit to the host for implementation
    in this checkout, so an Issue URL must not be able to skip the
    unambiguous-local-origin requirement that numeric input already enforces.
    """
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, tmp_path)

    with pytest.raises(LooprError) as captured:
        client.initialize("https://github.com/acme/demo/issues/42")

    assert captured.value.category == "repository"


def test_issue_client_url_requires_origin_remote(tmp_path: Path) -> None:
    """A canonical Issue URL still requires a configured origin remote."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "issue-repo-no-origin"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, repo)

    with pytest.raises(LooprError) as captured:
        client.initialize("https://github.com/acme/demo/issues/42")

    assert captured.value.category == "repository"


def test_issue_client_rejects_known_credential_in_body(tmp_path: Path) -> None:
    """Issue body content cannot carry a known credential value forward."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(body="token=known-secret-value"))
    runner.secrets.add("known-secret-value")
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.snapshot()

    assert captured.value.category == "credentials"


def test_issue_client_rejects_known_credential_in_comment(tmp_path: Path) -> None:
    """Issue comment content cannot carry a known credential value forward."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    comment: JsonObject = {
        "author": {"login": "commenter"},
        "body": "known-secret-value",
        "createdAt": "2026-01-01T00:00:00Z",
    }
    runner = FakeIssueGh(issue=_issue_payload(comments=[comment]))
    runner.secrets.add("known-secret-value")
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.snapshot()

    assert captured.value.category == "credentials"


def test_issue_client_rejects_oversized_body(tmp_path: Path) -> None:
    """An Issue body beyond the bound fails closed rather than truncating."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    oversized = "x" * (MAX_ISSUE_BODY_BYTES + 1)
    runner = FakeIssueGh(issue=_issue_payload(body=oversized))
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.snapshot()

    assert captured.value.category == "bundle"


def test_issue_client_bounds_and_orders_comments(tmp_path: Path) -> None:
    """Local defenses bound and order an oversized fake response."""
    total = MAX_ISSUE_COMMENTS + 5
    comments: list[JsonObject] = [
        {
            "author": {"login": f"user{index}"},
            "body": f"comment {index}",
            "createdAt": f"2026-01-01T00:00:{index:02d}Z",
        }
        for index in range(total)
    ]
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(comments=list(reversed(comments))))
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert len(snapshot.comments) == MAX_ISSUE_COMMENTS
    kept_bodies = [comment["body"] for comment in snapshot.comments]
    expected_bodies = [
        f"comment {index}" for index in range(total - MAX_ISSUE_COMMENTS, total)
    ]
    assert kept_bodies == expected_bodies


def test_issue_client_omits_oversized_comment_body(tmp_path: Path) -> None:
    """An individual oversized comment is omitted, not truncated."""
    comment: JsonObject = {
        "author": {"login": "author"},
        "body": "x" * (MAX_ISSUE_COMMENT_BYTES + 1),
        "createdAt": "2026-01-01T00:00:00Z",
    }
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(comments=[comment]))
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert snapshot.comments[0]["omitted"] is True
    assert snapshot.comments[0]["body"] == ""


def test_issue_client_snapshot_accepts_null_comment_author(tmp_path: Path) -> None:
    """A comment from a deleted account (`"author": null`) is not rejected."""
    comment: JsonObject = {
        "author": None,
        "body": "still relevant",
        "createdAt": "2026-01-01T00:00:00Z",
    }
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(comments=[comment]))
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    assert snapshot.comments[0]["author"] == ""
    assert snapshot.comments[0]["body"] == "still relevant"


def test_issue_client_omits_comments_past_aggregate_byte_bound(
    tmp_path: Path,
) -> None:
    """The aggregate byte bound is spent newest-first, keeping the newest."""
    per_comment_bytes = 15_000
    comments: list[JsonObject] = [
        {
            "author": {"login": f"user{index}"},
            "body": "x" * per_comment_bytes,
            "createdAt": f"2026-01-01T00:{index:02d}:00Z",
        }
        for index in range(MAX_ISSUE_COMMENTS)
    ]
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(comments=comments))
    client = IssueClient(runner, repo)
    client.initialize("42")

    snapshot = client.snapshot()

    included = MAX_ISSUE_COMMENTS_TOTAL_BYTES // per_comment_bytes
    excluded = MAX_ISSUE_COMMENTS - included
    assert [comment["omitted"] for comment in snapshot.comments[:excluded]] == (
        [True] * excluded
    )
    assert [comment["omitted"] for comment in snapshot.comments[excluded:]] == (
        [False] * included
    )
    assert snapshot.comments[0]["body"] == ""
    assert snapshot.comments[-1]["body"] == "x" * per_comment_bytes
    assert [comment["author"] for comment in snapshot.comments] == [
        f"user{index}" for index in range(MAX_ISSUE_COMMENTS)
    ]


def test_issue_client_default_branch_rejects_unsafe_ref(tmp_path: Path) -> None:
    """An unsafe default branch name fails closed."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(), default_branch="unsafe branch")
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.default_branch()

    assert captured.value.category == "ref"


def test_issue_client_branch_sha_rejects_invalid_sha(tmp_path: Path) -> None:
    """A malformed branch SHA from GitHub fails closed."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload(), branch_sha="not-a-sha")
    client = IssueClient(runner, repo)
    client.initialize("42")

    with pytest.raises(LooprError) as captured:
        client.branch_sha("main")

    assert captured.value.category == "sha"


def test_issue_client_branch_sha_encodes_slash_containing_branch(
    tmp_path: Path,
) -> None:
    """A branch name containing a slash is sent as one encoded path segment."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    runner = FakeIssueGh(issue=_issue_payload())
    client = IssueClient(runner, repo)
    client.initialize("42")

    client.branch_sha("release/1.0")

    api_calls = [call for call in runner.gh_calls if call[1] == "api"]
    assert api_calls[-1][-1] == "repos/acme/demo/branches/release%2F1.0"


def test_issue_client_reads_tracked_paths_and_blobs_at_base_sha(
    tmp_path: Path,
) -> None:
    """IssueClient reads repository evidence at an arbitrary base commit."""
    _git_exe, repo, base, _head = _repo_with_two_commits(tmp_path)
    client = IssueClient(CommandRunner(), repo)

    client.ensure_commit_object(base)

    assert client.tracked_paths_at(base) == ("file.py",)
    assert client.blob_bytes_at(base, "file.py", max_output=1024) == b"base\n"


def test_issue_client_ensure_commit_object_rejects_missing_sha(
    tmp_path: Path,
) -> None:
    """A base SHA absent from the local checkout fails closed."""
    repo = _repo_with_origin(tmp_path, "https://github.com/acme/demo.git")
    client = IssueClient(CommandRunner(), repo)

    with pytest.raises(LooprError) as captured:
        client.ensure_commit_object("a" * 40)

    assert captured.value.category == "git"
