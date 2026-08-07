"""Review command orchestration."""

from __future__ import annotations

import datetime as dt
import stat
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactWriter
from .github import GitHubClient
from .models import EXIT_ORACLE, EXIT_RACE, JsonValue, LooprError, ReviewResult
from .oracle import OracleClient
from .process import CommandRunner

if TYPE_CHECKING:
    from .models import PullRequest


def execute_review(
    *,
    pr_value: str,
    repo_dir: Path,
    artifacts_dir: Path,
    thinking_time: str,
    runner: CommandRunner | None = None,
) -> ReviewResult:
    """Review and post one exact pull-request snapshot."""
    command_runner = runner or CommandRunner()
    github = GitHubClient(
        command_runner,
        repo_dir,
        command_runner.source_env.get("GH_REVIEW_TOKEN", ""),
    )
    github.initialize(pr_value)
    initial = github.snapshot()
    github.ensure_objects(initial)
    writer = ArtifactWriter(
        _run_directory(repo_dir, artifacts_dir, initial),
        command_runner,
    )
    writer.json("initial-snapshot.json", initial.raw)
    oracle = OracleClient(command_runner, github, writer, thinking_time)
    verdict = oracle.review(initial, oracle.build_bundle(initial))

    before_post = github.snapshot()
    if not github.same_snapshot(initial, before_post):
        raise LooprError(
            EXIT_RACE,
            "stale_state",
            "pull request base or head changed before review posting",
        )
    body = (
        f"{verdict.review_body}\n\n---\n"
        f"Reviewed base: `{initial.base_sha}`\n"
        f"Reviewed head: `{initial.head_sha}`\n"
    )
    if len(body.encode("utf-8")) > MAX_POSTED_BODY_BYTES:
        raise LooprError(
            EXIT_ORACLE,
            "oracle_schema",
            "review body with audit footer exceeds GitHub's body limit",
        )
    event = "APPROVE" if verdict.verdict == "APPROVE" else "REQUEST_CHANGES"
    review_id, posted = github.post_review(initial, event, body)

    try:
        _persist_best_effort(writer, "github-review.json", posted)
        after_post = github.snapshot()
        verified = github.verify_posted(initial, review_id)
        expected_state = "APPROVED" if event == "APPROVE" else "CHANGES_REQUESTED"
        if verified.get("state") != expected_state or not github.same_snapshot(
            initial, after_post
        ):
            raise LooprError(
                EXIT_RACE,
                "stale_state",
                "posted review became stale or had an unexpected state",
            )
    except BaseException as exc:
        _dismiss_stale(github, initial, review_id, exc)
        raise

    result = ReviewResult(
        repository=initial.repository,
        pr_number=initial.number,
        base_sha=initial.base_sha,
        head_sha=initial.head_sha,
        verdict=verdict.verdict,
        github_review_id=review_id,
        blocking_findings=verdict.blocking_findings,
        implementation_prompt=verdict.implementation_prompt,
        artifacts_dir=str(writer.root),
    )
    _persist_best_effort(
        writer,
        "result.json",
        result.as_json(),
        suppress_interrupts=True,
    )
    return result


def _persist_best_effort(
    writer: ArtifactWriter,
    relative: str,
    value: JsonValue,
    *,
    suppress_interrupts: bool = False,
) -> None:
    """Persist an audit artifact without hiding an already-posted review.

    Before post-write verification, interrupts are re-raised so the surrounding
    compensation block can dismiss the unreported review. After verification,
    the caller may suppress every artifact-write failure so the verified review
    ID is still returned instead of encouraging a duplicate retry.
    """
    try:
        writer.json(relative, value)
    except LooprError as exc:
        message = (
            "pr-review-loop review: warning: failed to persist artifact "
            f"{relative}: {exc}"
        )
        sys.stderr.write(f"{message}\n")
    except BaseException as exc:
        if not suppress_interrupts:
            raise
        detail = str(exc) or type(exc).__name__
        message = (
            "pr-review-loop review: warning: failed to persist artifact "
            f"{relative}: {detail}"
        )
        sys.stderr.write(f"{message}\n")


MAX_POSTED_BODY_BYTES = 65_000
_RUN_DIRECTORY_ATTEMPTS = 8


def _trusted_runs_root(repo_dir: Path, artifacts_dir: Path) -> Path:
    """Descend to the run root from a trusted anchor without following symlinks.

    `artifacts_dir` is typically a repository-relative path (for example,
    `.pr-review-loop`), and the checked-out pull request controls its own repository
    contents, so a malicious head could plant a symlink there to redirect artifact
    writes outside the intended root. Each path component is created fresh or
    verified to already be a real directory before descending into it, and this
    applies to every component of `artifacts_dir` itself (not just a `runs` child)
    so an absolute path, or a symlink anywhere in its ancestry, cannot redirect the
    run root either. `..` components are rejected outright because they could
    otherwise walk the trusted anchor back out of it.
    """
    if ".." in artifacts_dir.parts:
        raise LooprError(
            EXIT_RACE,
            "artifacts",
            "artifact directory path may not contain '..'",
        )
    if artifacts_dir.is_absolute():
        anchor = Path(artifacts_dir.parts[0])
        parts = (*artifacts_dir.parts[1:], "runs")
    else:
        anchor = repo_dir.resolve()
        parts = (*artifacts_dir.parts, "runs")
    current = anchor
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise LooprError(
                EXIT_RACE,
                "artifacts",
                "artifact directory path contains a non-directory or symlink",
            )
    return current


def _run_directory(
    repo_dir: Path,
    artifacts_dir: Path,
    pull_request: PullRequest,
) -> Path:
    """Atomically claim a collision-resistant, unique run directory."""
    root = _trusted_runs_root(repo_dir, artifacts_dir)
    prefix = f"review-pr-{pull_request.number}-{pull_request.head_sha[:12]}"
    for _ in range(_RUN_DIRECTORY_ATTEMPTS):
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = root / f"{prefix}-{stamp}-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise LooprError(
        EXIT_RACE,
        "artifacts",
        "could not allocate a unique review run directory",
    )


def _dismiss_stale(
    github: GitHubClient,
    pull_request: PullRequest,
    review_id: int,
    original: BaseException,
) -> None:
    """Neutralize a stale review and preserve failure context if dismissal fails."""
    try:
        github.dismiss(pull_request, review_id)
    except LooprError as dismiss_error:
        original_message = str(original) or type(original).__name__
        message = (
            f"{original_message}; stale review {review_id} could not be dismissed: "
            f"{dismiss_error}"
        )
        raise LooprError(EXIT_RACE, "stale_state", message) from original
