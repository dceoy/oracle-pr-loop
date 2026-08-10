"""Tests for command result models and stable failures."""

from __future__ import annotations

from scripts.models import (
    BlockingFinding,
    BootstrapResult,
    FindingLocation,
    ReviewLoopError,
    ReviewResult,
    SubmitResult,
)


def test_review_loop_error_keeps_machine_fields() -> None:
    """Stable failures retain their exit code, category, and message."""
    error = ReviewLoopError(6, "stale_state", "head changed")

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
        verdict="REQUEST_CHANGES",
        github_review_id=42,
        blocking_findings=(
            BlockingFinding(
                id="F1",
                title="Bug",
                description="Description",
                required_change="Fix it",
                location=FindingLocation(path="file.py", line=7, side="RIGHT"),
            ),
        ),
        implementation_prompt="Fix it.",
    )

    payload = result.as_json()

    assert payload["schema_version"] == 1
    assert payload["command"] == "review"
    assert payload["repository"] == "acme/demo"
    assert payload["blocking_findings"] == [
        {
            "id": "F1",
            "title": "Bug",
            "description": "Description",
            "required_change": "Fix it",
            "location": {"path": "file.py", "line": 7, "side": "RIGHT"},
        }
    ]


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
    )

    payload = result.as_json()

    assert payload["schema_version"] == 1
    assert payload["command"] == "submit"
    assert payload["resulting_head_sha"] == payload["commit_sha"]


def test_bootstrap_result_serializes_stable_schema() -> None:
    """Bootstrap results expose the version-1 machine contract."""
    result = BootstrapResult(
        repository="acme/demo",
        issue_number=7,
        issue_url="https://github.com/acme/demo/issues/7",
        issue_updated_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha="a" * 40,
        implementation_prompt="Implement the requested change.",
    )

    payload = result.as_json()

    assert payload == {
        "schema_version": 1,
        "command": "bootstrap",
        "repository": "acme/demo",
        "issue_number": 7,
        "issue_url": "https://github.com/acme/demo/issues/7",
        "issue_updated_at": "2026-01-01T00:00:00Z",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "implementation_prompt": "Implement the requested change.",
    }
