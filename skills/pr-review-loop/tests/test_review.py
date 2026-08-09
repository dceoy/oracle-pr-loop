"""Contract, race, and compensation tests for review orchestration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar

import pytest
from scripts import review as review_module
from scripts.models import (
    EXIT_ORACLE,
    EXIT_RACE,
    JsonObject,
    JsonValue,
    LooprError,
    OracleReview,
    PullRequest,
    ReviewComment,
)
from scripts.process import CommandRunner
from scripts.review import execute_review

if TYPE_CHECKING:
    from pathlib import Path

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


def finding(
    identifier: str,
    location: JsonValue = None,
) -> JsonObject:
    """Return one validated-shape blocking finding with the given location."""
    return {
        "id": identifier,
        "title": f"Title {identifier}",
        "description": f"Description {identifier}.",
        "required_change": f"Change {identifier}.",
        "location": location,
    }


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
    anchors: ClassVar[frozenset[tuple[str, str, int]]] = frozenset()

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
        self.posted_comments: list[tuple[ReviewComment, ...]] = []
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)

    def initialize(self, _pr_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return the next deterministic snapshot."""
        return self._snapshots.pop(0)

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    def diff_anchors(
        self,
        _pull_request: PullRequest,
    ) -> frozenset[tuple[str, str, int]]:
        """Return the configured anchors of the frozen fake diff."""
        return type(self).anchors

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
        comments: tuple[ReviewComment, ...] = (),
    ) -> tuple[int, JsonObject]:
        """Record a posted review anchored to the supplied head."""
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


def install_orchestration_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace external review transports with deterministic fakes."""
    monkeypatch.setattr(review_module, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(review_module, "build_review_bundle", fake_build_review_bundle)
    monkeypatch.setattr(review_module, "invoke_oracle", fake_oracle_invocation)
    monkeypatch.setattr(review_module, "parse_review", fake_parse_review)


@pytest.mark.parametrize(
    ("oracle_verdict", "expected_findings"),
    [("APPROVE", ()), ("REQUEST_CHANGES", (finding("F1"),))],
)
def test_self_authored_review_uses_comment_and_preserves_oracle_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    oracle_verdict: str,
    expected_findings: tuple[JsonObject, ...],
) -> None:
    """A PR author can publish either canonical Oracle verdict as a comment."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", initial.author)
    install_orchestration_fakes(monkeypatch)

    def parse_verdict(_raw: str, pull_request: PullRequest) -> OracleReview:
        return replace(
            approve_review(pull_request),
            verdict=oracle_verdict,
            blocking_findings=expected_findings,
            implementation_prompt=(
                "Fix F1." if oracle_verdict == "REQUEST_CHANGES" else None
            ),
        )

    monkeypatch.setattr(review_module, "parse_review", parse_verdict)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
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

    monkeypatch.setattr(review_module, "parse_review", parse_contradictory_review)

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

    def parse_oversized_review(
        _raw: str,
        pull_request: PullRequest,
    ) -> OracleReview:
        return replace(approve_review(pull_request), review_body="x" * 70_000)

    monkeypatch.setattr(review_module, "parse_review", parse_oversized_review)

    with pytest.raises(LooprError) as captured:
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

    def snapshot(self) -> PullRequest:  # ruff: ignore[no-self-use] -- overridden with instance state below
        return sample_pr()

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
    monkeypatch.setattr(review_module, "build_review_bundle", fake_build_review_bundle)
    monkeypatch.setattr(review_module, "invoke_oracle", fake_oracle_invocation)
    monkeypatch.setattr(review_module, "parse_review", fake_parse_review)

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
    monkeypatch: pytest.MonkeyPatch,
    findings: tuple[JsonObject, ...],
    anchors: frozenset[tuple[str, str, int]],
) -> None:
    """Install a REQUEST_CHANGES verdict carrying findings over a fake diff."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    monkeypatch.setattr(FakeGitHubClient, "authenticated_login_value", "another-user")
    monkeypatch.setattr(FakeGitHubClient, "anchors", anchors)
    install_orchestration_fakes(monkeypatch)

    class FindingsOracleClient(FakeOracleClient):
        @staticmethod
        def review(
            pull_request: PullRequest,
            _attachments: tuple[Path, ...],
        ) -> OracleReview:
            return replace(
                approve_review(pull_request),
                verdict="REQUEST_CHANGES",
                review_body="Overall: changes required.",
                blocking_findings=findings,
                implementation_prompt="Fix the findings.",
            )

    monkeypatch.setattr(review_module, "OracleClient", FindingsOracleClient)


def _published(tmp_path: Path) -> FakeGitHubClient:
    """Run one review and return the GitHub transport that published it."""
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A finding anchored to a real diff line is published inline, not in the body."""
    location: JsonObject = {"path": "file.py", "line": 7, "side": "RIGHT"}
    _install_findings(
        monkeypatch,
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every anchored finding rides the same single create-review write."""
    _install_findings(
        monkeypatch,
        (
            finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"}),
            finding("F2", {"path": "file.py", "line": 3, "side": "LEFT"}),
            finding("F3", {"path": "file.py", "line": 9, "side": "RIGHT"}),
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A null-location finding is published in the body with no inline comment."""
    _install_findings(monkeypatch, (finding("F1"),), frozenset())

    github = _published(tmp_path)

    body = github.posted_bodies[0]
    assert github.posted_comments[0] == ()
    assert "Findings without a diff anchor" in body
    assert "Title F1" in body
    assert "Overall: changes required." in body


def test_mixed_findings_are_partitioned_without_duplication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each finding appears exactly once, inline or in the body, never both."""
    _install_findings(
        monkeypatch,
        (
            finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"}),
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
        {"path": "file.py", "line": 8, "side": "RIGHT"},
        {"path": "file.py", "line": 7, "side": "LEFT"},
        {"path": "other.py", "line": 7, "side": "RIGHT"},
        {"path": "file.py", "line": 7, "side": "MIDDLE"},
        {"path": "file.py", "line": "7", "side": "RIGHT"},
        {"path": "file.py", "line": True, "side": "RIGHT"},
        "file.py:7",
    ],
)
def test_unanchorable_location_never_attaches_to_another_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    location: JsonValue,
) -> None:
    """A stale, ambiguous, or malformed anchor degrades to aggregate output."""
    _install_findings(
        monkeypatch,
        (finding("F1", location),),
        frozenset({("file.py", "RIGHT", 7)}),
    )

    github = _published(tmp_path)

    assert github.posted_comments[0] == ()
    assert "Title F1" in github.posted_bodies[0]


@pytest.mark.parametrize(
    ("side", "line"),
    [("RIGHT", 12), ("LEFT", 4), ("RIGHT", 5)],
)
def test_added_deleted_and_context_lines_anchor_on_their_own_side(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    side: str,
    line: int,
) -> None:
    """Added, deleted, and modified-file context lines each anchor as themselves."""
    _install_findings(
        monkeypatch,
        (finding("F1", {"path": "file.py", "line": line, "side": side}),),
        frozenset({
            ("file.py", "RIGHT", 12),
            ("file.py", "LEFT", 4),
            ("file.py", "RIGHT", 5),
        }),
    )

    github = _published(tmp_path)

    comments = github.posted_comments[0]
    assert [(item.side, item.line) for item in comments] == [(side, line)]


def test_inline_comments_are_bounded_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Oversized inline content fails before any GitHub write."""
    oversized = finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"})
    oversized["description"] = "x" * 70_000
    _install_findings(monkeypatch, (oversized,), frozenset({("file.py", "RIGHT", 7)}))

    with pytest.raises(LooprError) as captured:
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


def test_post_write_race_dismisses_review_carrying_inline_comments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Inline publication keeps the stale-review dismissal safeguard intact."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    _install_findings(
        monkeypatch,
        (finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"}),),
        frozenset({("file.py", "RIGHT", 7)}),
    )
    FakeGitHubClient.snapshots = [initial, initial, changed]

    with pytest.raises(LooprError) as captured:
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A head change before posting prevents any inline comment from being written."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    _install_findings(
        monkeypatch,
        (finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"}),),
        frozenset({("file.py", "RIGHT", 7)}),
    )
    FakeGitHubClient.snapshots = [initial, changed]

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
def test_formal_event_semantics_are_unchanged_by_inline_comments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: str,
) -> None:
    """APPROVE and REQUEST_CHANGES still select their own formal review event."""
    findings = (
        (finding("F1", {"path": "file.py", "line": 7, "side": "RIGHT"}),)
        if verdict == "REQUEST_CHANGES"
        else ()
    )
    _install_findings(monkeypatch, findings, frozenset({("file.py", "RIGHT", 7)}))
    if verdict == "APPROVE":
        install_orchestration_fakes(monkeypatch)

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
