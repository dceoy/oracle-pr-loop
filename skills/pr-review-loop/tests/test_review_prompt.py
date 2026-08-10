"""Contract tests for direct ChatGPT GitHub app invocation."""

from scripts.models import PullRequest
from scripts.review import review_prompt

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


def test_review_prompt_invokes_github_directly() -> None:
    """The exact Oracle-delivered review prompt requests GitHub directly."""
    prompt = review_prompt(_sample_pr())

    assert prompt.startswith("@GitHub\n")
    assert "--browser-github-app" not in prompt
    assert "Review only repository\nowner/repository, PR #21" in prompt
    assert SHA_A in prompt
    assert SHA_B in prompt
