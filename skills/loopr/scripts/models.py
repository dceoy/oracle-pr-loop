"""Validated models and stable errors for the loopr review command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

EXIT_PRECONDITION = 2
EXIT_ORACLE = 3
EXIT_GITHUB = 4
EXIT_RACE = 6

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class LooprError(RuntimeError):
    """A stable command failure with an exit code and machine category."""

    def __init__(self, code: int, category: str, message: str) -> None:
        """Initialize a stable loopr failure."""
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
class OracleReview:
    """A strictly validated Oracle review verdict."""

    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    verdict: str
    review_body: str
    blocking_findings: tuple[dict[str, str], ...]
    implementation_prompt: str | None
    non_blocking_notes: tuple[str, ...]
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
    blocking_findings: tuple[dict[str, str], ...]
    implementation_prompt: str | None
    artifacts_dir: str

    def as_json(self) -> JsonObject:
        """Return the stable command result schema."""
        return {
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
            "artifacts_dir": self.artifacts_dir,
        }
