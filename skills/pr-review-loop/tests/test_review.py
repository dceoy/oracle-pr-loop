"""Contract, race, and compensation tests for review orchestration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, override

import pytest
from scripts import review as review_module
from scripts.models import (
    EXIT_ORACLE,
    EXIT_RACE,
    BlockingFinding,
    FindingLocation,
    JsonObject,
    OracleReview,
    PullRequest,
    PullRequestIdentity,
    ReviewComment,
    ReviewLoopError,
)
from scripts.process import CommandRunner
from scripts.review import execute_review

from .support import SHA_B, SHA_C, sample_pr

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def finding(
    identifier: str,
    location: FindingLocation | None = None,
) -> BlockingFinding:
    """Return one validated-shape blocking finding with the given location."""
    return BlockingFinding(
        id=identifier,
        title=f"Title {identifier}",
        description=f"Description {identifier}.",
        required_change=f"Change {identifier}.",
        location=location,
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
    )


def identity_snapshot(pull_request: PullRequest) -> PullRequestIdentity:
    """Return the reduced PR identity shape used by review race checks."""
    return PullRequestIdentity(
        repository=pull_request.repository,
        number=pull_request.number,
        url=pull_request.url,
        state=pull_request.state,
        is_draft=pull_request.is_draft,
        base_ref=pull_request.base_ref,
        base_sha=pull_request.base_sha,
        head_ref=pull_request.head_ref,
        head_sha=pull_request.head_sha,
        head_repository=pull_request.head_repository,
    )


class FakeGitHubClient:
    """A deterministic PR/review transport for orchestration race tests."""

    instance: ClassVar[FakeGitHubClient | None] = None
    snapshots: ClassVar[list[PullRequest]] = []
    authenticated_login_value: ClassVar[str] = "reviewer"
    anchors: ClassVar[frozenset[tuple[str, str, int]]] = frozenset()

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
    ) -> None:
        """Initialize the fake client with queued review state."""
        self.repo_dir = repo_dir
        self.authenticated_login = type(self).authenticated_login_value
        self.dismissed: list[int] = []
        self.post_count = 0
        self.full_snapshot_calls = 0
        self.identity_snapshot_calls = 0
        self.posted_events: list[str] = []
        self.posted_bodies: list[str] = []
        self.posted_comments: list[tuple[ReviewComment, ...]] = []
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)

    def initialize(self, _pr_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return the next deterministic full snapshot."""
        self.full_snapshot_calls += 1
        return self._snapshots.pop(0)

    def identity_snapshot(self) -> PullRequestIdentity:
        """Return the next deterministic identity-only state."""
        self.identity_snapshot_calls += 1
        return identity_snapshot(self._snapshots.pop(0))

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    def diff_anchors(
        self,
        _pull_request: PullRequest,
    ) -> frozenset[tuple[str, str, int]]:
        """Return the configured anchors of the frozen fake diff."""
        return type(self).anchors

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        """Use formal events unless the fake authenticated user authored it.

        Returns:
            The GitHub review event for the supplied verdict.
        """
        return "COMMENT" if pull_request.author == self.authenticated_login else verdict

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare base and head SHAs.

        Returns:
            Whether both snapshots have identical base and head SHAs.
        """
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        event: str,
        body: str,
        comments: tuple[ReviewComment, ...] = (),
    ) -> tuple[int, JsonObject]:
        """Record a posted review anchored to the supplied head.

        Returns:
            The deterministic review ID and persisted review payload.
        """
        self.post_count += 1
        self.posted_events.append(event)
        self.posted_bodies.append(body)
        self.posted_comments.append(comments)
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


def fake_build_review_bundle(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
    """Return an empty deterministic fake bundle."""
    return ()


def fake_oracle_invocation(*_args: object, **_kwargs: object) -> str:
    """Return a placeholder raw response without launching Oracle."""
    return "raw"


def fake_parse_review(_raw: str, pull_request: PullRequest) -> OracleReview:
    """Return a valid approval for the supplied snapshot."""
    return approve_review(pull_request)


def install_orchestration_fakes(mocker: MockerFixture) -> None:
    """Replace external review transports with deterministic fakes."""
    mocker.patch.object(review_module, "GitHubClient", FakeGitHubClient)
    mocker.patch.object(review_module, "build_review_bundle", fake_build_review_bundle)
    mocker.patch.object(review_module, "invoke_oracle", fake_oracle_invocation)
    mocker.patch.object(review_module, "parse_review", fake_parse_review)


@pytest.mark.parametrize(
    ("oracle_verdict", "expected_findings"),
    [("APPROVE", ()), ("REQUEST_CHANGES", (finding("F1"),))],
)
def test_self_authored_review_uses_comment_and_preserves_oracle_verdict(
    mocker: MockerFixture,
    tmp_path: Path,
    oracle_verdict: str,
    expected_findings: tuple[BlockingFinding, ...],
) -> None:
    """A PR author can publish either canonical Oracle verdict as a comment."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(mocker)

    def parse_verdict(_raw: str, pull_request: PullRequest) -> OracleReview:
        return replace(
            approve_review(pull_request),
            verdict=oracle_verdict,
            blocking_findings=expected_findings,
            implementation_prompt=(
                "Fix F1." if oracle_verdict == "REQUEST_CHANGES" else None
            ),
        )

    mocker.patch.object(review_module, "parse_review", parse_verdict)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        thinking_time="heavy",
        runner=CommandRunner(),
    )

    assert result.verdict == oracle_verdict
    assert result.blocking_findings == expected_findings
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.full_snapshot_calls == 1
    assert FakeGitHubClient.instance.identity_snapshot_calls == 2
    assert FakeGitHubClient.instance.posted_events == ["COMMENT"]
    assert (
        f"Reviewed base: `{initial.base_sha}`"
        in FakeGitHubClient.instance.posted_bodies[0]
    )
    assert (
        f"Reviewed head: `{initial.head_sha}`"
        in FakeGitHubClient.instance.posted_bodies[0]
    )


def test_execute_review_forwards_oracle_overrides(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Review forwards model and effort values to the shared Oracle call."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    install_orchestration_fakes(mocker)
    calls: list[tuple[object, object]] = []

    def record_oracle(*args: object, **kwargs: object) -> str:
        calls.append((args[3], kwargs.get("model")))
        return "raw"

    mocker.patch.object(review_module, "invoke_oracle", record_oracle)

    execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        thinking_time="extended",
        model="gpt-5.6-sol",
        runner=CommandRunner(),
    )

    assert calls == [("extended", "gpt-5.6-sol")]


def test_formal_review_rejects_mismatched_persisted_state(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A formal review whose re-read state does not match the event is rejected."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", "another-user")
    install_orchestration_fakes(mocker)

    class MismatchedStateGitHubClient(FakeGitHubClient):
        def verify_posted(
            self,
            _pull_request: PullRequest,
            _review_id: int,
            _body: str,
        ) -> JsonObject:
            del self
            return {"state": "COMMENTED"}

    mocker.patch.object(review_module, "GitHubClient", MismatchedStateGitHubClient)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"
    assert MismatchedStateGitHubClient.instance is not None
    assert MismatchedStateGitHubClient.instance.dismissed == [123]


def test_self_authored_review_rejects_body_disagreeing_with_verdict_via_state(
    mocker: MockerFixture,
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
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(mocker)

    def parse_contradictory_review(
        _raw: str,
        pull_request: PullRequest,
    ) -> OracleReview:
        return replace(
            approve_review(pull_request),
            verdict="REQUEST_CHANGES",
            review_body="Approved.",
            blocking_findings=(finding("F1"),),
            implementation_prompt="Fix F1.",
        )

    mocker.patch.object(review_module, "parse_review", parse_contradictory_review)

    class WrongStateGitHubClient(FakeGitHubClient):
        def verify_posted(
            self,
            _pull_request: PullRequest,
            _review_id: int,
            _body: str,
        ) -> JsonObject:
            del self
            return {"state": "APPROVED"}

    mocker.patch.object(review_module, "GitHubClient", WrongStateGitHubClient)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_self_authored_post_write_race_skips_dismissal(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A stale comment is reported without an impossible formal dismissal."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, initial, changed]
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == []


def test_pre_post_snapshot_race_fails_before_review_write(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A base/head change before posting prevents the GitHub write."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, changed]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_post_write_race_dismisses_stale_review(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A base/head change after posting dismisses the stale GitHub review."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, initial, changed]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == [123]


def test_execute_review_rejects_oversized_posted_body(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """The review body plus audit footer is bounded before the GitHub write."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial]
    install_orchestration_fakes(mocker)

    def parse_oversized_review(
        _raw: str,
        pull_request: PullRequest,
    ) -> OracleReview:
        return replace(approve_review(pull_request), review_body="x" * 70_000)

    mocker.patch.object(review_module, "parse_review", parse_oversized_review)

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


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

    def snapshot(self) -> PullRequest:  # ruff: ignore[no-self-use] -- test fake
        return sample_pr()

    def identity_snapshot(self) -> PullRequestIdentity:  # ruff: ignore[no-self-use] -- test fake
        return identity_snapshot(sample_pr())

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        pass

    @staticmethod
    def diff_anchors(_pull_request: PullRequest) -> frozenset[tuple[str, str, int]]:
        return frozenset()

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
        _comments: tuple[ReviewComment, ...] = (),
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


class _InterruptingGitHubClient(_StableGitHubClient):
    """Interrupt during the post-write identity verification."""

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
    ) -> None:
        super().__init__(_runner, repo_dir)
        self.identity_count = 0
        type(self).instance = self

    def identity_snapshot(self) -> PullRequestIdentity:
        self.identity_count += 1
        if self.identity_count == 2:
            raise KeyboardInterrupt
        return identity_snapshot(sample_pr())


def test_post_write_snapshot_interrupt_dismisses_review(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A post-write KeyboardInterrupt neutralizes the unreported review."""
    mocker.patch.object(review_module, "GitHubClient", _InterruptingGitHubClient)
    mocker.patch.object(review_module, "build_review_bundle", fake_build_review_bundle)
    mocker.patch.object(review_module, "invoke_oracle", fake_oracle_invocation)
    mocker.patch.object(review_module, "parse_review", fake_parse_review)

    with pytest.raises(KeyboardInterrupt):
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert _InterruptingGitHubClient.instance is not None
    assert _InterruptingGitHubClient.instance.dismissed == [123]


def _install_findings(
    mocker: MockerFixture,
    findings: tuple[BlockingFinding, ...],
    anchors: frozenset[tuple[str, str, int]],
) -> None:
    """Install a REQUEST_CHANGES verdict carrying findings over a fake diff."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", "another-user")
    mocker.patch.object(FakeGitHubClient, "anchors", anchors)
    install_orchestration_fakes(mocker)

    def parse_findings(_raw: str, pull_request: PullRequest) -> OracleReview:
        return replace(
            approve_review(pull_request),
            verdict="REQUEST_CHANGES",
            review_body="Overall: changes required.",
            blocking_findings=findings,
            implementation_prompt="Fix the findings.",
        )

    mocker.patch.object(review_module, "parse_review", parse_findings)


def _published(tmp_path: Path) -> FakeGitHubClient:
    """Run one review and return the GitHub transport that published it.

    Returns:
        The fake GitHub client used for publication.
    """
    execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        thinking_time="heavy",
        runner=CommandRunner(),
    )
    github = FakeGitHubClient.instance
    assert github is not None
    return github


def test_line_specific_finding_becomes_one_inline_comment(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A finding anchored to a real diff line is published inline, not in the body."""
    location = FindingLocation(path="file.py", line=7, side="RIGHT")
    _install_findings(
        mocker,
        (finding("F1", location),),
        frozenset({("file.py", "RIGHT", 7)}),
    )

    github = _published(tmp_path)

    comments = github.posted_comments[0]
    assert [(item.path, item.side, item.line) for item in comments] == [
        ("file.py", "RIGHT", 7)
    ]
    assert "Title F1" in comments[0].body
    assert "Change F1." in comments[0].body
    assert "Title F1" not in github.posted_bodies[0]


def test_multiple_anchored_findings_share_one_review_submission(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Every anchored finding rides the same single create-review write."""
    _install_findings(
        mocker,
        (
            finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),
            finding("F2", FindingLocation(path="file.py", line=3, side="LEFT")),
            finding("F3", FindingLocation(path="file.py", line=9, side="RIGHT")),
        ),
        frozenset({
            ("file.py", "RIGHT", 7),
            ("file.py", "LEFT", 3),
            ("file.py", "RIGHT", 9),
        }),
    )

    github = _published(tmp_path)

    assert github.post_count == 1
    assert [
        (item.path, item.side, item.line) for item in github.posted_comments[0]
    ] == [
        ("file.py", "RIGHT", 7),
        ("file.py", "LEFT", 3),
        ("file.py", "RIGHT", 9),
    ]


def test_global_findings_stay_in_the_aggregate_body(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A null-location finding is published in the body with no inline comment."""
    _install_findings(mocker, (finding("F1"),), frozenset())

    github = _published(tmp_path)

    body = github.posted_bodies[0]
    assert github.posted_comments[0] == ()
    assert "Findings without a diff anchor" in body
    assert "Title F1" in body
    assert "Overall: changes required." in body


@pytest.mark.parametrize(
    ("oracle_verdict", "expected_findings"),
    [("APPROVE", ()), ("REQUEST_CHANGES", (finding("F1"),))],
)
def test_anchor_discovery_is_skipped_when_no_finding_requests_a_location(
    mocker: MockerFixture,
    tmp_path: Path,
    oracle_verdict: str,
    expected_findings: tuple[BlockingFinding, ...],
) -> None:
    """``diff_anchors()`` never runs when no finding can be published inline.

    An ``APPROVE`` verdict carries zero findings, and this ``REQUEST_CHANGES``
    verdict's only finding has a null location; either way, no finding could
    ever receive an inline anchor, so the diff/blob/attribute validation work
    in ``diff_anchors()`` must not run at all.
    """
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    mocker.patch.object(FakeGitHubClient, "authenticated_login_value", "another-user")
    install_orchestration_fakes(mocker)

    def parse_verdict(_raw: str, pull_request: PullRequest) -> OracleReview:
        return replace(
            approve_review(pull_request),
            verdict=oracle_verdict,
            blocking_findings=expected_findings,
            implementation_prompt=("Fix F1." if expected_findings else None),
        )

    mocker.patch.object(review_module, "parse_review", parse_verdict)

    class ForbidAnchorDiscoveryGitHubClient(FakeGitHubClient):
        @override
        def diff_anchors(
            self,
            _pull_request: PullRequest,
        ) -> frozenset[tuple[str, str, int]]:
            msg = "diff_anchors() must not run when no finding requests a location"
            raise AssertionError(msg)

    mocker.patch.object(
        review_module, "GitHubClient", ForbidAnchorDiscoveryGitHubClient
    )

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        thinking_time="heavy",
        runner=CommandRunner(),
    )

    assert result.verdict == oracle_verdict


def test_mixed_findings_are_partitioned_without_duplication(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Each finding appears exactly once, inline or in the body, never both."""
    _install_findings(
        mocker,
        (
            finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),
            finding("F2"),
        ),
        frozenset({("file.py", "RIGHT", 7)}),
    )

    github = _published(tmp_path)

    body = github.posted_bodies[0]
    comments = github.posted_comments[0]
    assert [item.line for item in comments] == [7]
    assert "Title F1" in comments[0].body
    assert "Title F2" not in comments[0].body
    assert "Title F1" not in body
    assert "Title F2" in body


@pytest.mark.parametrize(
    "location",
    [
        FindingLocation(path="file.py", line=8, side="RIGHT"),
        FindingLocation(path="file.py", line=7, side="LEFT"),
        FindingLocation(path="other.py", line=7, side="RIGHT"),
        FindingLocation(path="file.py", line=7, side="MIDDLE"),
    ],
)
def test_unanchorable_location_never_attaches_to_another_line(
    mocker: MockerFixture,
    tmp_path: Path,
    location: FindingLocation,
) -> None:
    """A stale or semantically invalid anchor degrades to aggregate output."""
    _install_findings(
        mocker,
        (finding("F1", location),),
        frozenset({("file.py", "RIGHT", 7)}),
    )

    github = _published(tmp_path)

    assert github.posted_comments[0] == ()
    assert "Title F1" in github.posted_bodies[0]


def test_context_line_left_location_degrades_to_aggregate_body(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A LEFT location for an unchanged context line is never sent as inline.

    The reviewed diff's anchor set (as `GitHubClient.diff_anchors` would
    report it) contains this context line only as `RIGHT`, matching GitHub's
    own review-comment semantics; a proposed `LEFT` location for it must not
    validate, or GitHub would reject the whole atomic review with HTTP 422.
    """
    _install_findings(
        mocker,
        (finding("F1", FindingLocation(path="file.py", line=1, side="LEFT")),),
        frozenset({("file.py", "RIGHT", 1)}),
    )

    github = _published(tmp_path)

    assert github.posted_comments[0] == ()
    assert "Title F1" in github.posted_bodies[0]


@pytest.mark.parametrize(
    ("side", "line"),
    [("RIGHT", 12), ("LEFT", 4), ("RIGHT", 5)],
)
def test_added_deleted_and_context_lines_anchor_on_their_own_side(
    mocker: MockerFixture,
    tmp_path: Path,
    side: str,
    line: int,
) -> None:
    """Added, deleted, and modified-file context lines each anchor as themselves."""
    _install_findings(
        mocker,
        (finding("F1", FindingLocation(path="file.py", line=line, side=side)),),
        frozenset({
            ("file.py", "RIGHT", 12),
            ("file.py", "LEFT", 4),
            ("file.py", "RIGHT", 5),
        }),
    )

    github = _published(tmp_path)

    comments = github.posted_comments[0]
    assert [(item.side, item.line) for item in comments] == [(side, line)]


def test_one_oversized_inline_comment_is_bounded_before_publication(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A single inline comment over GitHub's per-comment body limit fails closed."""
    oversized = replace(
        finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),
        description="x" * 70_000,
    )
    _install_findings(mocker, (oversized,), frozenset({("file.py", "RIGHT", 7)}))

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_many_individually_bounded_inline_comments_are_not_rejected_in_aggregate(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """GitHub bounds each comment body independently, not their combined size.

    Each of these findings is well within GitHub's per-comment body limit on
    its own, but their combined size exceeds it; the publication must not
    reject the whole review over a limit GitHub's create-review contract does
    not impose.
    """
    findings = tuple(
        replace(
            finding(
                f"F{index}",
                FindingLocation(path="file.py", line=index, side="RIGHT"),
            ),
            description="x" * 40_000,
        )
        for index in range(1, 4)
    )
    anchors = frozenset(("file.py", "RIGHT", index) for index in range(1, 4))
    _install_findings(mocker, findings, anchors)

    github = _published(tmp_path)

    assert len(github.posted_comments[0]) == len(findings)


def test_post_write_race_dismisses_review_carrying_inline_comments(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Inline publication keeps the stale-review dismissal safeguard intact."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    _install_findings(
        mocker,
        (finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),),
        frozenset({("file.py", "RIGHT", 7)}),
    )
    FakeGitHubClient.snapshots = [initial, initial, changed]

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.posted_comments[0] != ()
    assert FakeGitHubClient.instance.dismissed == [123]


def test_pre_post_race_blocks_inline_publication(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A head change before posting prevents any inline comment from being written."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    _install_findings(
        mocker,
        (finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),),
        frozenset({("file.py", "RIGHT", 7)}),
    )
    FakeGitHubClient.snapshots = [initial, changed]

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_head_change_during_anchor_discovery_blocks_publication(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """A head change while ``diff_anchors()`` runs is caught before the write.

    ``_publication()`` runs the bounded diff plus per-blob/attribute
    validation subprocesses inside ``diff_anchors()``. The pre-write
    freshness snapshot must be taken after that work completes, immediately
    before ``post_review()``, so a drift introduced during anchor discovery
    is still caught instead of silently posting a review against a commit
    that is no longer current.
    """
    _install_findings(
        mocker,
        (finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),),
        frozenset({("file.py", "RIGHT", 7)}),
    )

    class DriftDuringAnchorsGitHubClient(FakeGitHubClient):
        """Simulate the PR head changing while `diff_anchors()` runs."""

        def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
            super().__init__(runner, repo_dir)
            self._drifted = False

        @override
        def snapshot(self) -> PullRequest:
            """Return the original full review snapshot."""
            return sample_pr()

        @override
        def identity_snapshot(self) -> PullRequestIdentity:
            """Expose the drift to the identity-only freshness read.

            Returns:
                The current simulated PR identity.
            """
            return identity_snapshot(
                sample_pr(head_sha=SHA_C if self._drifted else SHA_B)
            )

        def diff_anchors(
            self,
            pull_request: PullRequest,
        ) -> frozenset[tuple[str, str, int]]:
            self._drifted = True
            return super().diff_anchors(pull_request)

    mocker.patch.object(review_module, "GitHubClient", DriftDuringAnchorsGitHubClient)
    FakeGitHubClient.snapshots = []

    with pytest.raises(ReviewLoopError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"
    assert DriftDuringAnchorsGitHubClient.instance is not None
    assert DriftDuringAnchorsGitHubClient.instance.post_count == 0


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
def test_formal_event_semantics_are_unchanged_by_inline_comments(
    mocker: MockerFixture,
    tmp_path: Path,
    verdict: str,
) -> None:
    """APPROVE and REQUEST_CHANGES still select their own formal review event."""
    findings = (
        (finding("F1", FindingLocation(path="file.py", line=7, side="RIGHT")),)
        if verdict == "REQUEST_CHANGES"
        else ()
    )
    _install_findings(mocker, findings, frozenset({("file.py", "RIGHT", 7)}))
    if verdict == "APPROVE":
        install_orchestration_fakes(mocker)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        thinking_time="heavy",
        runner=CommandRunner(),
    )
    github = FakeGitHubClient.instance
    assert github is not None

    assert result.verdict == verdict
    assert github.posted_events == [verdict]
    assert len(github.posted_comments[0]) == len(findings)
