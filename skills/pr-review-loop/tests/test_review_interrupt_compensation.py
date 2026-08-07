"""Regression tests for post-write interruption compensation."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from scripts import review as review_module
from scripts.models import JsonObject, OracleReview, PullRequest
from scripts.process import CommandRunner
from scripts.review import execute_review

SHA_A = "a" * 40
SHA_B = "b" * 40


def _sample_pr() -> PullRequest:
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
        head_sha=SHA_B,
        head_repository="owner/repository",
        changed_paths=("file.py",),
        raw={},
    )


class _InterruptingGitHubClient:
    """Post a review, then interrupt during post-write snapshot verification."""

    instance: ClassVar[_InterruptingGitHubClient | None] = None

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
        _token: str,
    ) -> None:
        self.repo_dir = repo_dir
        self.snapshot_count = 0
        self.dismissed: list[int] = []
        type(self).instance = self

    def initialize(self, _pr_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return pre-write snapshots, then interrupt after the review POST."""
        self.snapshot_count += 1
        if self.snapshot_count == 3:
            raise KeyboardInterrupt
        return _sample_pr()

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare frozen base and head SHAs."""
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
        """Return a valid state if snapshot verification does not interrupt."""
        return {"state": "APPROVED"}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        """Record review neutralization."""
        self.dismissed.append(review_id)


class _ApprovingOracleClient:
    """Return a deterministic approval without launching Oracle."""

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


def test_post_write_interrupt_dismisses_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A post-write KeyboardInterrupt neutralizes the unreported review."""
    monkeypatch.setattr(review_module, "GitHubClient", _InterruptingGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", _ApprovingOracleClient)

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
