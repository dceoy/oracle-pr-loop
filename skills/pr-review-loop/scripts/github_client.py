"""Hardened GitHub client behavior for the review command."""

from __future__ import annotations

from .github import GitHubClient as BaseGitHubClient
from .github import validate_path
from .models import EXIT_PRECONDITION, LooprError, PullRequest


class GitHubClient(BaseGitHubClient):
    """Read tracked paths without Git's display-oriented path quoting."""

    def tracked_paths(self, pull_request: PullRequest) -> tuple[str, ...]:
        """List every tracked UTF-8 path in the frozen head tree."""
        output = self.git_bytes(
            ["ls-tree", "-r", "-z", "--name-only", pull_request.head_sha],
            max_output=4 * 1024 * 1024,
        )
        paths: list[str] = []
        for raw_path in output.split(b"\0"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise LooprError(
                    EXIT_PRECONDITION,
                    "path",
                    "Git tree returned a non-UTF-8 tracked path",
                ) from exc
            paths.append(validate_path(path))
        if len(paths) != len(set(paths)):
            raise LooprError(
                EXIT_PRECONDITION,
                "path",
                "Git tree returned duplicate tracked paths",
            )
        return tuple(sorted(paths))
