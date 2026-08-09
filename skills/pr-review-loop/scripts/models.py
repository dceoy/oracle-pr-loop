"""Validated models and stable errors for skill commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

EXIT_PRECONDITION = 2
EXIT_ORACLE = 3
EXIT_GITHUB = 4
EXIT_RACE = 6

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class LooprError(RuntimeError):
    """A stable command failure with an exit code and machine category."""

    def __init__(self, code: int, category: str, message: str) -> None:
        """Initialize a stable command failure."""
        super().__init__(message)
        self.code = code
        self.category = category


@dataclass(frozen=True)
class PullRequest:
    """An immutable GitHub pull-request snapshot."""

    repository: str
    number: int
    url: str
    title: str
    body: str
    author: str
    state: str
    is_draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str
    changed_paths: tuple[str, ...]
    raw: JsonObject


@dataclass(frozen=True)
class PullRequestIdentity:
    """An immutable GitHub pull-request identity and ref snapshot."""

    repository: str
    number: int
    url: str
    state: str
    is_draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str
    raw: JsonObject


@dataclass(frozen=True)
class IssueSnapshot:
    """An immutable GitHub Issue snapshot used to bootstrap implementation work."""

    repository: str
    number: int
    url: str
    title: str
    body: str
    author: str
    state: str
    updated_at: str
    comments: tuple[JsonObject, ...]
    raw: JsonObject


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment anchored to a validated frozen-diff line."""

    path: str
    line: int
    side: str
    body: str

    def as_payload(self) -> JsonObject:
        """Return the GitHub create-review comment payload.

        Returns:
            The comment object accepted by GitHub's create-review API.
        """
        return {
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "body": self.body,
        }


@dataclass(frozen=True)
class OracleReview:
    """A strictly validated Oracle review verdict."""

    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    verdict: str
    review_body: str
    blocking_findings: tuple[JsonObject, ...]
    implementation_prompt: str | None
    non_blocking_notes: tuple[str, ...]
    raw: JsonObject


@dataclass(frozen=True)
class OracleBootstrap:
    """A strictly validated Oracle implementation-bootstrap result."""

    repository: str
    issue_number: int
    base_sha: str
    implementation_prompt: str
    raw: JsonObject


@dataclass(frozen=True)
class ReviewResult:
    """The machine-readable result returned by the review command."""

    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    verdict: str
    github_review_id: int
    blocking_findings: tuple[JsonObject, ...]
    implementation_prompt: str | None

    def as_json(self) -> JsonObject:
        """Return the stable command result schema."""
        return cast(
            "JsonObject",
            {
                "schema_version": 1,
                "command": "review",
                "repository": self.repository,
                "pr_number": self.pr_number,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "verdict": self.verdict,
                "github_review_id": self.github_review_id,
                "blocking_findings": list(self.blocking_findings),
                "implementation_prompt": self.implementation_prompt,
            },
        )


@dataclass(frozen=True)
class SubmitResult:
    """The machine-readable result returned by the submit command."""

    repository: str
    pr_number: int
    base_sha: str
    previous_head_sha: str
    resulting_head_sha: str
    commit_sha: str
    pushed_branch: str

    def as_json(self) -> JsonObject:
        """Return the stable command result schema."""
        return cast(
            "JsonObject",
            {
                "schema_version": 1,
                "command": "submit",
                "repository": self.repository,
                "pr_number": self.pr_number,
                "base_sha": self.base_sha,
                "previous_head_sha": self.previous_head_sha,
                "resulting_head_sha": self.resulting_head_sha,
                "commit_sha": self.commit_sha,
                "pushed_branch": self.pushed_branch,
            },
        )


@dataclass(frozen=True)
class BootstrapResult:
    """The machine-readable result returned by the bootstrap command."""

    repository: str
    issue_number: int
    issue_url: str
    issue_updated_at: str
    base_ref: str
    base_sha: str
    implementation_prompt: str

    def as_json(self) -> JsonObject:
        """Return the stable command result schema."""
        return cast(
            "JsonObject",
            {
                "schema_version": 1,
                "command": "bootstrap",
                "repository": self.repository,
                "issue_number": self.issue_number,
                "issue_url": self.issue_url,
                "issue_updated_at": self.issue_updated_at,
                "base_ref": self.base_ref,
                "base_sha": self.base_sha,
                "implementation_prompt": self.implementation_prompt,
            },
        )
