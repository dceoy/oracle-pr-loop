"""Regression tests for review resource bounds."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from scripts.artifacts import ArtifactWriter
from scripts.github import GitHubClient
from scripts.models import LooprError, PullRequest
from scripts.oracle import (
    MAX_INSTRUCTION_FILES,
    MAX_ORACLE_ARG_BYTES,
    MAX_ORACLE_ATTACHMENTS,
    OracleClient,
)
from scripts.process import CommandRunner

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


class _TooManyInstructionsGitHub:
    """Expose an excessive repository-wide instruction-file inventory."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def tracked_paths(self, _pull_request: PullRequest) -> tuple[str, ...]:
        """Return more instruction files than the bundle contract allows."""
        return tuple(
            f"docs/{index}/AGENTS.md" for index in range(MAX_INSTRUCTION_FILES + 1)
        )

    def patch(self, _pull_request: PullRequest, *, max_output: int) -> bytes:
        """Return a minimal valid patch before instruction discovery is bounded."""
        del max_output
        return b"diff --git a/file.py b/file.py\n"


def test_bundle_rejects_excessive_instruction_file_inventory(tmp_path: Path) -> None:
    """Repository-wide instruction discovery is bounded before attachment reads."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = cast(
        "GitHubClient",
        _TooManyInstructionsGitHub(tmp_path),
    )
    oracle = OracleClient(runner, github, writer, "heavy")

    with pytest.raises(LooprError) as captured:
        oracle.build_bundle(_sample_pr())

    assert captured.value.category == "bundle"
    assert "instruction-file limit" in str(captured.value)


def test_oracle_review_rejects_excessive_attachment_count(tmp_path: Path) -> None:
    """The Oracle command cannot receive an unbounded number of --file arguments."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    attachments = tuple(
        Path(f"attachment-{index}.txt") for index in range(MAX_ORACLE_ATTACHMENTS + 1)
    )

    with pytest.raises(LooprError) as captured:
        oracle.review(_sample_pr(), attachments)

    assert captured.value.category == "bundle"
    assert "attachment count" in str(captured.value)


def test_oracle_review_rejects_excessive_argument_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The complete Oracle argv is byte-bounded before subprocess execution."""
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    oversized_path = Path("x" * MAX_ORACLE_ARG_BYTES)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Oracle subprocess must not run with oversized arguments")

    monkeypatch.setattr(runner, "run", unexpected_run)

    with pytest.raises(LooprError) as captured:
        oracle.review(_sample_pr(), (oversized_path,))

    assert captured.value.category == "bundle"
    assert "arguments exceed" in str(captured.value)
