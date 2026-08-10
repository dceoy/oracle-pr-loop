"""Focused tests for review identity-only race revalidation."""

from __future__ import annotations

from scripts.models import PullRequestIdentity
from scripts.review import _same_review_identity

from .support import SHA_C, sample_pr


def _identity(*, head_sha: str | None = None) -> PullRequestIdentity:
    """Return the reduced identity shape used by review freshness reads."""
    pull_request = sample_pr()
    return PullRequestIdentity(
        repository=pull_request.repository,
        number=pull_request.number,
        url=pull_request.url,
        state=pull_request.state,
        is_draft=pull_request.is_draft,
        base_ref=pull_request.base_ref,
        base_sha=pull_request.base_sha,
        head_ref=pull_request.head_ref,
        head_sha=head_sha or pull_request.head_sha,
        head_repository=pull_request.head_repository,
    )


def test_review_identity_comparison_uses_reduced_snapshot_shape() -> None:
    """Review freshness compares exact base/head SHAs on identity snapshots."""
    initial = sample_pr()

    assert _same_review_identity(initial, _identity()) is True
    assert _same_review_identity(initial, _identity(head_sha=SHA_C)) is False
