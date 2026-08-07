"""Regression coverage for pull-request ref rebinding during submit."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_submit_command import ScenarioRunner, _fixture_repo, _git

from scripts.models import EXIT_RACE, JsonObject, LooprError
from scripts.submit import execute_guarded as execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from scripts.process import CommandResult


class RefRebindingRunner(ScenarioRunner):
    """Change one PR ref name after the initial snapshot without moving its SHA."""

    def __init__(
        self,
        repo: Path,
        remote: Path,
        state: JsonObject,
        *,
        field: str,
        replacement: str,
    ) -> None:
        """Initialize one same-SHA ref rebinding race."""
        super().__init__(repo, remote, state)
        self.field = field
        self.replacement = replacement
        self.snapshot_count = 0

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = 24 * 1024 * 1024,
        watch_path: Path | None = None,
    ) -> CommandResult:
        """Rebind the selected ref before the second GitHub snapshot."""
        argv = [str(value) for value in args]
        if argv[:3] == ["gh", "pr", "view"]:
            self.snapshot_count += 1
            if self.snapshot_count == 2:
                self.state[self.field] = self.replacement
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("baseRefName", "renamed-main"),
        ("headRefName", "renamed-feature"),
    ],
)
def test_ref_rebinding_fails_before_staging(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    """A same-SHA base or head ref change cannot redirect the later push."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = RefRebindingRunner(
        repo,
        remote,
        state,
        field=field,
        replacement=replacement,
    )

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"
    assert not _git(repo, "diff", "--cached", "--name-only")
    assert _git(repo, "rev-parse", "HEAD") == head
    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == head
