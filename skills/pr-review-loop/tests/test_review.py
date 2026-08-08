"""Contract, race, and compensation tests for review orchestration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
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


class FakeGitHubClient:
    """A deterministic PR/review transport for orchestration race tests."""

    instance: ClassVar[FakeGitHubClient | None] = None
    snapshots: ClassVar[list[PullRequest]] = []
    authenticated_login_value: ClassVar[str] = "reviewer"

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
    ) -> None:
        self.repo_dir = repo_dir
        self.authenticated_login = type(self).authenticated_login_value
        self.dismissed: list[int] = []
        self.post_count = 0
        self.posted_events: list[str] = []
        self.posted_bodies: list[str] = []
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)

    def initialize(self, _pr_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return the next deterministic snapshot."""
        return self._snapshots.pop(0)

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        """Use formal events unless the fake authenticated user authored it."""
        return "COMMENT" if pull_request.author == self.authenticated_login else verdict

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare base and head SHAs."""
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        event: str,
        body: str,
    ) -> tuple[int, JsonObject]:
        """Record a posted review anchored to the supplied head."""
        self.post_count += 1
        self.posted_events.append(event)
        self.posted_bodies.append(body)
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        _pull_request: PullRequest,
        _review_id: int,
        _body: str,
    ) -> JsonObject:
        """Return the review state matching the most recently posted event."""
        expected_state = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
        }
        return {"state": expected_state[self.posted_events[-1]]}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        """Record stale-review neutralization."""
        self.dismissed.append(review_id)


class FakeOracleClient:
    """Return a deterministic review without launching Oracle."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    @staticmethod
    def build_bundle(_pull_request: PullRequest) -> tuple[Path, ...]:
        """Return an empty deterministic fake bundle."""
        return ()

    @staticmethod
    def review(
        pull_request: PullRequest,
        _attachments: tuple[Path, ...],
    ) -> OracleReview:
        """Return a valid approval for the supplied snapshot."""
        return approve_review(pull_request)


def install_orchestration_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace external review transports with deterministic fakes."""
    monkeypatch.setattr(review_module, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", FakeOracleClient)


def test_execute_review_claims_run_directory_named_for_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The claimed run directory is prefixed with the PR number and head SHA."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    install_orchestration_fakes(monkeypatch)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner(),
    )

    prefix = f"review-pr-{initial.number}-{initial.head_sha[:12]}-"
    assert Path(result.artifacts_dir).name.startswith(prefix)


@pytest.mark.parametrize(
    ("oracle_verdict", "expected_findings"),
    [("APPROVE", ()), ("REQUEST_CHANGES", ({"id": "F1"},))],
)
def test_self_authored_review_uses_comment_and_preserves_oracle_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    oracle_verdict: str,
    expected_findings: tuple[dict[str, str], ...],
) -> None:
    """A PR author can publish either canonical Oracle verdict as a comment."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(monkeypatch)

    class OracleVerdict(FakeOracleClient):
        @staticmethod
        def review(
            pull_request: PullRequest,
            _attachments: tuple[Path, ...],
        ) -> OracleReview:
            return replace(
                approve_review(pull_request),
                verdict=oracle_verdict,
                blocking_findings=expected_findings,
                implementation_prompt=(
                    "Fix F1." if oracle_verdict == "REQUEST_CHANGES" else None
                ),
            )

    monkeypatch.setattr(review_module, "OracleClient", OracleVerdict)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner(),
    )

    assert result.verdict == oracle_verdict
    assert result.blocking_findings == expected_findings
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.posted_events == ["COMMENT"]
    assert (
        f"Reviewed base: `{initial.base_sha}`"
        in FakeGitHubClient.instance.posted_bodies[0]
    )
    assert (
        f"Reviewed head: `{initial.head_sha}`"
        in FakeGitHubClient.instance.posted_bodies[0]
    )


def test_formal_review_rejects_mismatched_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A formal review whose re-read state does not match the event is rejected."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", "another-user")
    install_orchestration_fakes(monkeypatch)

    class MismatchedStateGitHubClient(FakeGitHubClient):
        def verify_posted(
            self,
            _pull_request: PullRequest,
            _review_id: int,
            _body: str,
        ) -> JsonObject:
            del self
            return {"state": "COMMENTED"}

    monkeypatch.setattr(review_module, "GitHubClient", MismatchedStateGitHubClient)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"
    assert MismatchedStateGitHubClient.instance is not None
    assert MismatchedStateGitHubClient.instance.dismissed == [123]


def test_self_authored_review_rejects_body_disagreeing_with_verdict_via_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A self-authored comment whose persisted state is not COMMENTED is rejected.

    This guards the audit-integrity regression Oracle flagged: a REQUEST_CHANGES
    verdict must not be publishable while GitHub's re-read state disagrees with
    the selected COMMENT transport event, regardless of what free-form
    ``review_body`` text Oracle supplied (for example a contradictory
    "Approved." body attached to a REQUEST_CHANGES verdict).
    """
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(monkeypatch)

    class ContradictoryBodyOracleClient(FakeOracleClient):
        @staticmethod
        def review(
            pull_request: PullRequest,
            _attachments: tuple[Path, ...],
        ) -> OracleReview:
            return replace(
                approve_review(pull_request),
                verdict="REQUEST_CHANGES",
                review_body="Approved.",
                blocking_findings=({"id": "F1"},),
                implementation_prompt="Fix F1.",
            )

    monkeypatch.setattr(review_module, "OracleClient", ContradictoryBodyOracleClient)

    class WrongStateGitHubClient(FakeGitHubClient):
        def verify_posted(
            self,
            _pull_request: PullRequest,
            _review_id: int,
            _body: str,
        ) -> JsonObject:
            del self
            return {"state": "APPROVED"}

    monkeypatch.setattr(review_module, "GitHubClient", WrongStateGitHubClient)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_self_authored_post_write_race_skips_dismissal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale comment is reported without an impossible formal dismissal."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, initial, changed]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == []


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
            runner=CommandRunner(),
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
            runner=CommandRunner(),
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
        @staticmethod
        def review(
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
            runner=CommandRunner(),
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
        runner=CommandRunner(),
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
    ) -> None:
        self.repo_dir = repo_dir
        self.authenticated_login = "reviewer"
        self.dismissed: list[int] = []
        type(self).instance = self

    def initialize(self, _pr_value: str) -> None:
        pass

    def snapshot(self) -> PullRequest:  # ruff: ignore[no-self-use] -- overridden with instance state below
        return sample_pr()

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        pass

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        return "COMMENT" if pull_request.author == self.authenticated_login else verdict

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    @staticmethod
    def post_review(
        pull_request: PullRequest,
        _event: str,
        _body: str,
    ) -> tuple[int, JsonObject]:
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    @staticmethod
    def verify_posted(
        _pull_request: PullRequest,
        _review_id: int,
        _body: str,
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
            runner=CommandRunner(),
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
        runner=CommandRunner(),
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
    ) -> None:
        super().__init__(_runner, repo_dir)
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
            runner=CommandRunner(),
        )

    assert _InterruptingGitHubClient.instance is not None
    assert _InterruptingGitHubClient.instance.dismissed == [123]
