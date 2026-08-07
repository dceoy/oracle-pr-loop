"""Contract, race, and compensation tests for review orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from scripts import review as review_module
from scripts.artifacts import ArtifactWriter
from scripts.models import (
    EXIT_ORACLE,
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    JsonValue,
    LooprError,
    OracleReview,
    PullRequest,
)
from scripts.process import CommandRunner
from scripts.review import execute_review

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def sample_pr(*, base_sha: str = SHA_A, head_sha: str = SHA_B) -> PullRequest:
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
        base_sha=base_sha,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=("file.py",),
        raw={},
    )


def approve_review(pull_request: PullRequest) -> OracleReview:
    """Return a valid APPROVE result for a frozen snapshot."""
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


def test_run_directory_retries_on_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A colliding candidate run directory is retried with a fresh suffix."""
    pull_request = sample_pr()

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(review_module.dt, "datetime", _FixedDateTime)
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(
        review_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(tokens)),
    )
    stamp = "20260101T000000Z"
    prefix = f"review-pr-{pull_request.number}-{pull_request.head_sha[:12]}"
    colliding = tmp_path / "artifacts" / "runs" / f"{prefix}-{stamp}-aaaaaaaa"
    colliding.mkdir(parents=True)

    result = review_module._run_directory(tmp_path, Path("artifacts"), pull_request)

    assert result.name == f"{prefix}-{stamp}-bbbbbbbb"
    assert result.is_dir()


def test_run_directory_rejects_symlinked_artifacts_component(tmp_path: Path) -> None:
    """A repository-controlled symlink cannot redirect audit artifacts."""
    pull_request = sample_pr()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, Path("artifacts"), pull_request)

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_run_directory_rejects_relative_traversal(tmp_path: Path) -> None:
    """A relative artifact root cannot escape the checkout."""
    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, Path("../escape"), sample_pr())

    assert captured.value.category == "artifacts"


class FakeGitHubClient:
    """A deterministic PR/review transport for orchestration race tests."""

    instance: ClassVar[FakeGitHubClient | None] = None
    snapshots: ClassVar[list[PullRequest]] = []

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
        _token: str,
    ) -> None:
        self.repo_dir = repo_dir
        self.dismissed: list[int] = []
        self.post_count = 0
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)

    def initialize(self, _pr_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return the next deterministic snapshot."""
        return self._snapshots.pop(0)

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare base and head SHAs."""
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        _event: str,
        _body: str,
    ) -> tuple[int, JsonObject]:
        """Record a posted review anchored to the supplied head."""
        self.post_count += 1
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        _pull_request: PullRequest,
        _review_id: int,
    ) -> JsonObject:
        """Return a valid approved review state."""
        return {"state": "APPROVED"}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        """Record stale-review neutralization."""
        self.dismissed.append(review_id)


class FakeOracleClient:
    """Return a deterministic review without launching Oracle."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def build_bundle(self, _pull_request: PullRequest) -> tuple[Path, ...]:
        """Return an empty deterministic fake bundle."""
        return ()

    def review(
        self,
        pull_request: PullRequest,
        _attachments: tuple[Path, ...],
    ) -> OracleReview:
        """Return a valid approval for the supplied snapshot."""
        return approve_review(pull_request)


def install_orchestration_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace external review transports with deterministic fakes."""
    monkeypatch.setattr(review_module, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", FakeOracleClient)


def test_pre_post_snapshot_race_fails_before_review_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A base/head change before posting prevents the GitHub write."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, changed]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_post_write_race_dismisses_stale_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A base/head change after posting dismisses the stale GitHub review."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, initial, changed]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == [123]


def test_execute_review_rejects_oversized_posted_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The review body plus audit footer is bounded before the GitHub write."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial]
    install_orchestration_fakes(monkeypatch)

    class OversizedOracleClient(FakeOracleClient):
        def review(
            self,
            pull_request: PullRequest,
            _attachments: tuple[Path, ...],
        ) -> OracleReview:
            return replace(approve_review(pull_request), review_body="x" * 70_000)

    monkeypatch.setattr(review_module, "OracleClient", OversizedOracleClient)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


def test_execute_review_survives_post_write_artifact_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An artifact write failure after a verified post does not fail the command."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    install_orchestration_fakes(monkeypatch)
    original_json = ArtifactWriter.json

    def failing_json(self: ArtifactWriter, relative: str, value: JsonObject) -> Path:
        if relative in {"github-review.json", "result.json"}:
            raise LooprError(EXIT_PRECONDITION, "artifacts", "disk full")
        return original_json(self, relative, value)

    monkeypatch.setattr(ArtifactWriter, "json", failing_json)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
    )

    assert result.github_review_id == 123
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == []


class _StableGitHubClient:
    """Provide a stable review mutation and record compensation."""

    instance: ClassVar[_StableGitHubClient | None] = None

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
        pass

    def snapshot(self) -> PullRequest:
        return sample_pr()

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        pass

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        _event: str,
        _body: str,
    ) -> tuple[int, JsonObject]:
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        _pull_request: PullRequest,
        _review_id: int,
    ) -> JsonObject:
        return {"state": "APPROVED"}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        self.dismissed.append(review_id)


class _InterruptingArtifactWriter:
    """Raise an interrupt for one configured artifact write."""

    fail_relative: ClassVar[str] = ""

    def __init__(self, root: Path, _runner: CommandRunner) -> None:
        self.root = root

    def json(self, relative: str, _value: JsonValue) -> Path:
        if relative == self.fail_relative:
            raise KeyboardInterrupt
        return self.root / relative


def _install_interrupt_fakes(
    monkeypatch: pytest.MonkeyPatch,
    fail_relative: str,
) -> None:
    _InterruptingArtifactWriter.fail_relative = fail_relative
    _StableGitHubClient.instance = None
    monkeypatch.setattr(review_module, "GitHubClient", _StableGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(review_module, "ArtifactWriter", _InterruptingArtifactWriter)


def test_interrupt_before_verification_dismisses_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An interrupt before verification cannot leave an unreported review."""
    _install_interrupt_fakes(monkeypatch, "github-review.json")

    with pytest.raises(KeyboardInterrupt):
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert _StableGitHubClient.instance is not None
    assert _StableGitHubClient.instance.dismissed == [123]


def test_interrupt_after_verification_returns_review_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verified review success survives interruption during final persistence."""
    _install_interrupt_fakes(monkeypatch, "result.json")

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
    )

    assert result.github_review_id == 123
    assert _StableGitHubClient.instance is not None
    assert _StableGitHubClient.instance.dismissed == []


class _InterruptingGitHubClient(_StableGitHubClient):
    """Interrupt during the post-write snapshot verification."""

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
        _token: str,
    ) -> None:
        super().__init__(_runner, repo_dir, _token)
        self.snapshot_count = 0
        type(self).instance = self

    def snapshot(self) -> PullRequest:
        self.snapshot_count += 1
        if self.snapshot_count == 3:
            raise KeyboardInterrupt
        return sample_pr()


def test_post_write_snapshot_interrupt_dismisses_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A post-write KeyboardInterrupt neutralizes the unreported review."""
    monkeypatch.setattr(review_module, "GitHubClient", _InterruptingGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", FakeOracleClient)

    with pytest.raises(KeyboardInterrupt):
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )

    assert _InterruptingGitHubClient.instance is not None
    assert _InterruptingGitHubClient.instance.dismissed == [123]
