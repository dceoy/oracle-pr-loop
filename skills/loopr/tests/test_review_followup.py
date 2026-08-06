"""Regression coverage for review follow-up findings."""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- local Git regression
from pathlib import Path
from typing import ClassVar

import pytest

from scripts import review as review_module
from scripts.github_client import GitHubClient
from scripts.models import JsonObject, JsonValue, OracleReview, PullRequest
from scripts.process import CommandRunner
from scripts.review import execute_review

SHA_A = "a" * 40
SHA_B = "b" * 40


def _sample_pr(*, head_sha: str = SHA_B, paths: tuple[str, ...] = ("file.py",)) -> PullRequest:
    """Return one valid frozen pull-request snapshot."""
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
        base_sha=SHA_A,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=paths,
        raw={},
    )


class _FakeGitHubClient:
    """Provide a stable review mutation and record compensation."""

    instance: ClassVar[_FakeGitHubClient | None] = None

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
        _token: str,
    ) -> None:
        self.repo_dir = repo_dir
        self.dismissed: list[int] = []
        type(self).instance = self

    def initialize(self, _pr_value: str) -> None:
        """Accept the fake target."""

    def snapshot(self) -> PullRequest:
        """Return an unchanged pull-request snapshot."""
        return _sample_pr()

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available objects."""

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare the frozen base and head SHAs."""
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        _event: str,
        _body: str,
    ) -> tuple[int, JsonObject]:
        """Return one successful review mutation."""
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        _pull_request: PullRequest,
        _review_id: int,
    ) -> JsonObject:
        """Return the expected approval state."""
        return {"state": "APPROVED"}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        """Record review neutralization."""
        self.dismissed.append(review_id)


class _ApprovingOracleClient:
    """Return a deterministic approval without launching Oracle."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def build_bundle(self, _pull_request: PullRequest) -> tuple[Path, ...]:
        """Return an empty fake bundle."""
        return ()

    def review(
        self,
        pull_request: PullRequest,
        _attachments: tuple[Path, ...],
    ) -> OracleReview:
        """Return a valid approval for the supplied snapshot."""
        return OracleReview(
            repository=pull_request.repository,
            pr_number=pull_request.number,
            base_sha=pull_request.base_sha,
            head_sha=pull_request.head_sha,
            verdict="APPROVE",
            review_body="Approved.",
            blocking_findings=(),
            implementation_prompt=None,
            non_blocking_notes=(),
            raw={},
        )


class _InterruptingArtifactWriter:
    """Raise an interrupt for one configured artifact write."""

    fail_relative: ClassVar[str] = ""

    def __init__(self, root: Path, _runner: CommandRunner) -> None:
        self.root = root

    def json(self, relative: str, _value: JsonValue) -> Path:
        """Return a fake path or interrupt at the configured write."""
        if relative == self.fail_relative:
            raise KeyboardInterrupt
        return self.root / relative


def _install_review_fakes(monkeypatch: pytest.MonkeyPatch, fail_relative: str) -> None:
    """Install deterministic orchestration fakes."""
    _InterruptingArtifactWriter.fail_relative = fail_relative
    _FakeGitHubClient.instance = None
    monkeypatch.setattr(review_module, "GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", _ApprovingOracleClient)
    monkeypatch.setattr(review_module, "ArtifactWriter", _InterruptingArtifactWriter)


def test_interrupt_during_posted_review_artifact_dismisses_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An interrupt before verification cannot leave an unreported review."""
    _install_review_fakes(monkeypatch, "github-review.json")

    with pytest.raises(KeyboardInterrupt):
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert _FakeGitHubClient.instance is not None
    assert _FakeGitHubClient.instance.dismissed == [123]


def test_interrupt_during_final_result_artifact_returns_review_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A verified review result is returned even if final persistence interrupts."""
    _install_review_fakes(monkeypatch, "result.json")

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
    )

    assert result.github_review_id == 123
    assert _FakeGitHubClient.instance is not None
    assert _FakeGitHubClient.instance.dismissed == []


def _git(git: str, args: list[str], *, cwd: Path) -> str:
    """Run one local test-controlled Git command."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv
        [git, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_tracked_paths_preserve_unicode_names(tmp_path: Path) -> None:
    """Frozen tree enumeration parses non-ASCII paths without Git quoting."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["config", "user.email", "test@example.com"], cwd=repo)
    _git(git, ["config", "user.name", "Test"], cwd=repo)
    unicode_path = "日本語.txt"
    (repo / unicode_path).write_text("content\n")
    _git(git, ["add", unicode_path], cwd=repo)
    _git(git, ["commit", "-q", "-m", "unicode path"], cwd=repo)
    head = _git(git, ["rev-parse", "HEAD"], cwd=repo)
    client = GitHubClient(CommandRunner(), repo, "token")

    paths = client.tracked_paths(_sample_pr(head_sha=head, paths=(unicode_path,)))

    assert paths == (unicode_path,)
