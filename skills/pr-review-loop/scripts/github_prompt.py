"""Direct ChatGPT GitHub app review prompt construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .oracle import PROMPT

if TYPE_CHECKING:
    from .models import PullRequest


def review_prompt(pull_request: PullRequest) -> str:
    """Return the trusted review prompt with direct GitHub app invocation."""
    return "@GitHub\n" + PROMPT.format(
        repository=pull_request.repository,
        pr_number=pull_request.number,
        base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
    )
