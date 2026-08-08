"""Contract and race tests for Issue bootstrap orchestration."""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise Git directly
from pathlib import Path
from typing import ClassVar

import pytest
from scripts import bootstrap as bootstrap_module
from scripts.bootstrap import execute_bootstrap
from scripts.github import IssueClient
from scripts.models import (
    EXIT_PRECONDITION,
    EXIT_RACE,
    IssueSnapshot,
    JsonObject,
    LooprError,
    OracleBootstrap,
)
from scripts.process import CommandRunner

SHA_A = "a" * 40
SHA_B = "b" * 40


def _git(git: str, args: list[str], *, cwd: Path) -> str:
    """Run one test-controlled Git command and return stripped stdout."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed test argv
        [git, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def sample_issue(
    *,
    number: int = 7,
    updated_at: str = "2026-01-01T00:00:00Z",
    comments: tuple[JsonObject, ...] = (),
) -> IssueSnapshot:
    """Return one valid open Issue snapshot."""
    return IssueSnapshot(
        repository="owner/repository",
        number=number,
        url=f"https://github.com/owner/repository/issues/{number}",
        title="Title",
        body="Body",
        author="author",
        state="OPEN",
        updated_at=updated_at,
        comments=comments,
        raw={},
    )


class FakeIssueClient:
    """A deterministic Issue/branch/Git transport for orchestration tests."""

    instance: ClassVar[FakeIssueClient | None] = None
    snapshots: ClassVar[list[IssueSnapshot]] = []
    branches: ClassVar[list[str]] = []
    shas: ClassVar[list[str]] = []
    heads: ClassVar[list[bytes]] = []
    statuses: ClassVar[list[bytes]] = []

    def __init__(self, _runner: CommandRunner, repo_dir: Path) -> None:
        self.repo_dir = repo_dir
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)
        self._branches = list(type(self).branches)
        self._shas = list(type(self).shas)
        self._heads = list(type(self).heads)
        self._statuses = list(type(self).statuses)
        self.ensure_calls: list[str] = []
        self.status_args: list[list[str]] = []

    def initialize(self, _issue_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> IssueSnapshot:
        """Return the next deterministic Issue snapshot."""
        return self._snapshots.pop(0)

    def default_branch(self) -> str:
        """Return the next deterministic default branch name."""
        return self._branches.pop(0)

    def branch_sha(self, _branch: str) -> str:
        """Return the next deterministic branch SHA."""
        return self._shas.pop(0)

    def ensure_commit_object(self, sha: str) -> None:
        """Record the base commit availability check."""
        self.ensure_calls.append(sha)

    def git_bytes(self, args: list[str], *, max_output: int) -> bytes:
        """Return the next deterministic local `HEAD`/status probe result."""
        del max_output
        if args[:2] == ["rev-parse", "HEAD"]:
            return self._heads.pop(0)
        if args[:1] == ["status"]:
            self.status_args.append(args)
            return self._statuses.pop(0)
        message = f"unexpected git_bytes call: {args}"
        raise AssertionError(message)

    @staticmethod
    def tracked_paths_at(_sha: str) -> tuple[str, ...]:
        """Return an empty tracked-path inventory."""
        return ()

    @staticmethod
    def blob_bytes_at(_sha: str, _path: str, *, max_output: int) -> bytes | None:
        """Return no blob content."""
        del max_output
        return None


class FakeBootstrapOracleClient:
    """Return a deterministic bootstrap result without launching Oracle."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    @staticmethod
    def build_bundle(_issue: IssueSnapshot, _base_sha: str) -> tuple[Path, ...]:
        """Return an empty deterministic fake bundle."""
        return ()

    @staticmethod
    def generate(
        issue: IssueSnapshot,
        _base_ref: str,
        base_sha: str,
        _attachments: tuple[Path, ...],
    ) -> OracleBootstrap:
        """Return a valid bootstrap result bound to issue and base_sha."""
        return OracleBootstrap(
            repository=issue.repository,
            issue_number=issue.number,
            base_sha=base_sha,
            implementation_prompt="Implement the requested change.",
            raw={},
        )


def install_orchestration_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace external bootstrap transports with deterministic fakes."""
    monkeypatch.setattr(bootstrap_module, "IssueClient", FakeIssueClient)
    monkeypatch.setattr(
        bootstrap_module,
        "BootstrapOracleClient",
        FakeBootstrapOracleClient,
    )


def test_execute_bootstrap_returns_result_bound_to_issue_and_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable Issue and base branch produce a bound implementation prompt."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b"", b""]
    install_orchestration_fakes(monkeypatch)

    result = execute_bootstrap(
        issue_value="7",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner(),
    )

    assert result.repository == "owner/repository"
    assert result.issue_number == 7
    assert result.issue_url == issue.url
    assert result.issue_updated_at == issue.updated_at
    assert result.base_ref == "main"
    assert result.base_sha == SHA_A
    assert result.implementation_prompt == "Implement the requested change."
    assert FakeIssueClient.instance is not None
    assert FakeIssueClient.instance.ensure_calls == [SHA_A]
    assert FakeIssueClient.instance.status_args == [
        ["status", "--porcelain", "--", ".", ":(exclude)artifacts"],
        ["status", "--porcelain", "--", ".", ":(exclude)artifacts"],
    ]
    artifacts = Path(result.artifacts_dir)
    assert artifacts.name.startswith("bootstrap-issue-7-")
    assert (artifacts / "initial-issue.json").is_file()
    assert (artifacts / "result.json").is_file()


def test_execute_bootstrap_rejects_stale_issue_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Issue edited during prompt generation fails closed as stale."""
    initial = sample_issue(updated_at="2026-01-01T00:00:00Z")
    changed = sample_issue(updated_at="2026-01-02T00:00:00Z")
    FakeIssueClient.snapshots = [initial, changed]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_comment_edited_during_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edited comment fails closed even when `updatedAt` does not change.

    GitHub does not bump the parent Issue's `updatedAt` when an existing
    comment's body is edited, so the recheck must compare the bounded
    comment snapshot itself, not just `updatedAt`.
    """
    original_comment: JsonObject = {
        "author": "commenter",
        "body": "original",
        "created_at": "2026-01-01T00:00:00Z",
        "omitted": False,
    }
    edited_comment: JsonObject = {**original_comment, "body": "edited"}
    initial = sample_issue(comments=(original_comment,))
    changed = sample_issue(comments=(edited_comment,))
    FakeIssueClient.snapshots = [initial, changed]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_stale_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base branch that moves during prompt generation fails closed as stale."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_B]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_renamed_default_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default branch renamed during prompt generation fails closed as stale."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "trunk"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_head_drift_during_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local checkout moved off base_sha during generation fails as stale."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_B.encode()]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_head_mismatched_with_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout not already at base_sha fails closed before Oracle runs."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_B.encode()]
    FakeIssueClient.statuses = []
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "workspace"
    assert "git switch" in str(captured.value)


def test_execute_bootstrap_rejects_dirty_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncommitted tracked changes on the base commit fail closed."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.statuses = [b" M some-tracked-file.py\n"]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "workspace"


def test_execute_bootstrap_rejects_untracked_files_on_base_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing untracked files on the base commit fail closed too.

    A prior implementation checked cleanliness with
    `git status --porcelain --untracked-files=no`, which silently accepted
    arbitrary pre-existing untracked files that could later contaminate the
    first PR the host builds from this checkout.
    """
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.statuses = [b"?? untracked-file.py\n"]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "workspace"
    assert FakeIssueClient.instance is not None
    assert FakeIssueClient.instance.status_args == [
        ["status", "--porcelain", "--", ".", ":(exclude)artifacts"],
    ]


def test_worktree_is_dirty_excludes_own_artifacts_directory(tmp_path: Path) -> None:
    """Creating this command's own artifacts directory must not read as dirty.

    A prior implementation relied on the host repository's `.gitignore` to
    keep `artifacts_dir` invisible to `git status`; that is not guaranteed
    for every host repository, so the check must exclude it directly rather
    than assume it is already ignored.
    """
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["config", "user.email", "test@example.com"], cwd=repo)
    _git(git, ["config", "user.name", "Test"], cwd=repo)
    (repo / "file.py").write_text("base\n")
    _git(git, ["add", "file.py"], cwd=repo)
    _git(git, ["commit", "-q", "-m", "base"], cwd=repo)
    client = IssueClient(CommandRunner(), repo)
    artifacts_dir = Path("artifacts")

    assert bootstrap_module._worktree_is_dirty(client, artifacts_dir) is False

    run_dir = repo / artifacts_dir / "bootstrap-issue-7-run"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text("{}\n")

    assert bootstrap_module._worktree_is_dirty(client, artifacts_dir) is False

    (repo / "other-untracked.txt").write_text("x\n")

    assert bootstrap_module._worktree_is_dirty(client, artifacts_dir) is True


def test_artifacts_exclude_pathspec_is_none_outside_repo(tmp_path: Path) -> None:
    """An artifacts directory outside repo_dir needs no exclusion pathspec.

    A path `git status` cannot report on cannot contaminate its output, so
    there is nothing to exclude.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"

    assert bootstrap_module._artifacts_exclude_pathspec(repo, outside) is None


def test_artifacts_exclude_pathspec_is_none_for_repo_root(tmp_path: Path) -> None:
    """An artifacts directory equal to repo_dir itself needs no pathspec.

    Excluding the whole repository would defeat the cleanliness check
    entirely, so this degenerate case is treated as nothing-to-exclude.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    assert bootstrap_module._artifacts_exclude_pathspec(repo, Path()) is None


def test_execute_bootstrap_rejects_workspace_dirtied_during_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace dirtied while Oracle was working fails closed as stale.

    The post-Oracle recheck must apply the same tracked-and-untracked
    cleanliness invariant as the pre-check, not only compare `HEAD`.
    """
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.statuses = [b"", b"?? new-file.py\n"]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


class ClosingIssueClient(FakeIssueClient):
    """Simulate the Issue closing between the initial and post-Oracle read."""

    def __init__(self, runner: CommandRunner, repo_dir: Path) -> None:
        super().__init__(runner, repo_dir)
        self.snapshot_calls = 0

    def snapshot(self) -> IssueSnapshot:
        """Raise on the second read, as a real closed-Issue re-fetch would."""
        self.snapshot_calls += 1
        if self.snapshot_calls == 2:
            raise LooprError(EXIT_PRECONDITION, "state", "issue must be open")
        return super().snapshot()


def test_execute_bootstrap_propagates_issue_closed_during_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Issue closed during prompt generation surfaces its own state failure."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.statuses = [b""]
    monkeypatch.setattr(bootstrap_module, "IssueClient", ClosingIssueClient)
    monkeypatch.setattr(
        bootstrap_module,
        "BootstrapOracleClient",
        FakeBootstrapOracleClient,
    )

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "state"


class MissingBaseIssueClient(FakeIssueClient):
    """Simulate a base commit absent from the local checkout."""

    def ensure_commit_object(self, sha: str) -> None:  # ruff: ignore[no-self-use] -- overrides base
        """Fail closed as the shared immutable-Git mixin would."""
        message = f"{sha} is not a commit object"
        raise LooprError(EXIT_PRECONDITION, "git", message)


def test_execute_bootstrap_names_fetch_remedy_when_base_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing local base commit names the fetch remedy, not just the SHA."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = []
    FakeIssueClient.statuses = []
    monkeypatch.setattr(bootstrap_module, "IssueClient", MissingBaseIssueClient)
    monkeypatch.setattr(
        bootstrap_module,
        "BootstrapOracleClient",
        FakeBootstrapOracleClient,
    )

    with pytest.raises(LooprError) as captured:
        execute_bootstrap(
            issue_value="7",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.category == "git"
    assert "git fetch origin main" in str(captured.value)
