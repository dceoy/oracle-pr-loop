"""Tests for command result models and stable failures."""

from __future__ import annotations

from scripts.models import LooprError, ReviewResult, SubmitResult


def test_loop_error_keeps_machine_fields() -> None:
    """Stable failures retain their exit code, category, and message."""
    error = LooprError(6, "stale_state", "head changed")

    assert error.code == 6
    assert error.category == "stale_state"
    assert str(error) == "head changed"


def test_review_result_serializes_stable_schema() -> None:
    """Review results expose the version-1 machine contract."""
    result = ReviewResult(
        repository="acme/demo",
        pr_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
        verdict="APPROVE",
        github_review_id=42,
        blocking_findings=(),
        implementation_prompt=None,
        artifacts_dir="/private/review",
    )

    payload = result.as_json()

    assert payload["schema_version"] == 1
    assert payload["command"] == "review"
    assert payload["repository"] == "acme/demo"
    assert payload["blocking_findings"] == []


def test_submit_result_serializes_stable_schema() -> None:
    """Submit results expose the version-1 machine contract."""
    result = SubmitResult(
        repository="acme/demo",
        pr_number=1,
        base_sha="a" * 40,
        previous_head_sha="b" * 40,
        resulting_head_sha="c" * 40,
        commit_sha="c" * 40,
        pushed_branch="feature",
        artifacts_dir="/private/submit",
    )

    payload = result.as_json()

    assert payload["schema_version"] == 1
    assert payload["command"] == "submit"
    assert payload["resulting_head_sha"] == payload["commit_sha"]
