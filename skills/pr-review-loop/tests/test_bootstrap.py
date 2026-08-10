"""Contract and race tests for Issue bootstrap orchestration."""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise Git directly
from typing import TYPE_CHECKING, ClassVar

import pytest
from scripts import bootstrap as bootstrap_module
from scripts.bootstrap import execute_bootstrap
from scripts.github import IssueClient
from scripts.models import (
    EXIT_PRECONDITION,
    EXIT_RACE,
    BootstrapResult,
    IssueSnapshot,
    JsonObject,
    OracleBootstrap,
    ReviewLoopError,
)
from scripts.process import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

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
    )


class FakeIssueClient:
    """A deterministic Issue/branch/Git transport for orchestration tests."""

    instance: ClassVar[FakeIssueClient | None] = None
    snapshots: ClassVar[list[IssueSnapshot]] = []
    branches: ClassVar[list[str]] = []
    shas: ClassVar[list[str]] = []
    heads: ClassVar[list[bytes]] = []
    local_branches: ClassVar[list[bytes]] = []
    statuses: ClassVar[list[bytes]] = []

    def __init__(self, _runner: CommandRunner, repo_dir: Path) -> None:
        self.repo_dir = repo_dir
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)
        self._branches = list(type(self).branches)
        self._shas = list(type(self).shas)
        self._heads = list(type(self).heads)
        self._local_branches = list(type(self).local_branches)
        self._statuses = list(type(self).statuses)
        self.ensure_calls: list[str] = []
        self.status_args: list[list[str]] = []

    def initialize(self, _issue_value: str) -> None:
        """Accept the configured fake target."""

    def snapshot(self) -> IssueSnapshot:
        """Return the next deterministic snapshot."""
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
        """Return the next deterministic local HEAD/status probe result."""
        del max_output
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return self._local_branches.pop(0)
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


def fake_build_bootstrap_bundle(
    *_args: object,
    **_kwargs: object,
) -> tuple[Path, ...]:
    """Return an empty deterministic fake bundle."""
    return ()


def fake_oracle_invocation(*_args: object, **_kwargs: object) -> str:
    """Return a placeholder raw response without launching Oracle."""
    return "raw"


def fake_parse_bootstrap(
    _raw: str,
    issue: IssueSnapshot,
    base_sha: str,
) -> OracleBootstrap:
    """Return a valid bootstrap result bound to issue and base_sha."""
    return OracleBootstrap(
        repository=issue.repository,
        issue_number=issue.number,
        base_sha=base_sha,
        implementation_prompt="Implement the requested change.",
    )


def install_oracle_fakes(mocker: MockerFixture) -> None:
    """Replace Oracle bundle, transport, and parser functions."""
    mocker.patch.object(
        bootstrap_module,
        "build_bootstrap_bundle",
        fake_build_bootstrap_bundle,
    )
    mocker.patch.object(bootstrap_module, "invoke_oracle", fake_oracle_invocation)
    mocker.patch.object(bootstrap_module, "parse_bootstrap", fake_parse_bootstrap)


def install_orchestration_fakes(mocker: MockerFixture) -> None:
    """Replace external bootstrap transports with deterministic fakes."""
    mocker.patch.object(bootstrap_module, "IssueClient", FakeIssueClient)
    install_oracle_fakes(mocker)


def _configure_stable(issue: IssueSnapshot) -> None:
    """Configure a stable fake Issue and local workspace."""
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b"", b""]


def _execute(
    tmp_path: Path,
    *,
    thinking_time: str | None = "heavy",
    model: str | None = None,
) -> BootstrapResult:
    """Execute bootstrap with the current fake configuration."""
    return execute_bootstrap(
        issue_value="7",
        repo_dir=tmp_path,
        thinking_time=thinking_time,
        model=model,
        runner=CommandRunner(),
    )


def test_execute_bootstrap_returns_result_bound_to_issue_and_base(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A stable Issue and base branch produce a bound implementation prompt."""
    issue = sample_issue()
    _configure_stable(issue)
    install_orchestration_fakes(mocker)

    result = _execute(tmp_path)

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
        ["status", "--porcelain", "--untracked-files=all"],
        ["status", "--porcelain", "--untracked-files=all"],
    ]
    assert "artifacts_dir" not in result.as_json()


def test_execute_bootstrap_forwards_oracle_overrides(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Bootstrap forwards model and effort values to the shared Oracle call."""
    issue = sample_issue()
    _configure_stable(issue)
    install_orchestration_fakes(mocker)
    calls: list[tuple[object, object]] = []

    def record_oracle(*args: object, **kwargs: object) -> str:
        calls.append((args[3], kwargs.get("model")))
        return "raw"

    mocker.patch.object(bootstrap_module, "invoke_oracle", record_oracle)

    _execute(tmp_path, thinking_time="extended", model="gpt-5.6-sol")

    assert calls == [("extended", "gpt-5.6-sol")]


def test_execute_bootstrap_binds_implicit_runner_to_repo_dir(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """An omitted runner uses the same repository directory as Oracle."""
    issue = sample_issue()
    _configure_stable(issue)
    install_orchestration_fakes(mocker)
    created_repo_dirs: list[Path] = []

    def make_runner(*, repo_dir: Path) -> CommandRunner:
        created_repo_dirs.append(repo_dir)
        return CommandRunner(repo_dir=repo_dir)

    mocker.patch.object(bootstrap_module, "CommandRunner", make_runner)

    _ = execute_bootstrap(
        issue_value="7",
        repo_dir=tmp_path,
        thinking_time="heavy",
    )

    assert created_repo_dirs == [tmp_path]


def test_execute_bootstrap_rejects_stale_issue_update(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """An Issue edited during prompt generation fails closed as stale."""
    initial = sample_issue()
    changed = sample_issue(updated_at="2026-01-02T00:00:00Z")
    FakeIssueClient.snapshots = [initial, changed]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_execute_bootstrap_rejects_comment_edited_during_generation(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """An edited bounded comment fails closed even when updatedAt is stable."""
    original: JsonObject = {
        "author": "commenter",
        "body": "original",
        "created_at": "2026-01-01T00:00:00Z",
        "omitted": False,
    }
    initial = sample_issue(comments=(original,))
    changed = sample_issue(comments=({**original, "body": "edited"},))
    FakeIssueClient.snapshots = [initial, changed]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


@pytest.mark.parametrize(
    ("branches", "shas", "heads", "expected"),
    [
        (
            ["main", "main"],
            [SHA_A, SHA_B],
            [SHA_A.encode(), SHA_A.encode()],
            "stale_state",
        ),
        (
            ["main", "trunk"],
            [SHA_A, SHA_A],
            [SHA_A.encode(), SHA_A.encode()],
            "stale_state",
        ),
        (
            ["main", "main"],
            [SHA_A, SHA_A],
            [SHA_A.encode(), SHA_B.encode()],
            "stale_state",
        ),
    ],
)
def test_execute_bootstrap_rejects_base_or_head_drift(
    tmp_path: Path,
    mocker: MockerFixture,
    branches: list[str],
    shas: list[str],
    heads: list[bytes],
    expected: str,
) -> None:
    """A changed default branch, base SHA, or local HEAD fails closed."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = branches
    FakeIssueClient.shas = shas
    FakeIssueClient.heads = heads
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b""]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == expected


@pytest.mark.parametrize("local_branch", [b"main", b"HEAD"])
def test_execute_bootstrap_rejects_default_or_detached_branch(
    tmp_path: Path,
    mocker: MockerFixture,
    local_branch: bytes,
) -> None:
    """The implementation handoff requires a named non-default branch."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.local_branches = [local_branch]
    FakeIssueClient.statuses = []
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "workspace"


@pytest.mark.parametrize("status", [b" M changed.py\n", b"?? untracked.py\n"])
def test_execute_bootstrap_rejects_dirty_workspace(
    tmp_path: Path,
    mocker: MockerFixture,
    status: bytes,
) -> None:
    """Tracked and untracked pre-existing files fail closed."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [status]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "workspace"


def _init_repo(git: str, repo: Path) -> None:
    """Initialize a test-controlled Git repository with an empty base commit."""
    repo.mkdir()
    _git(git, ["init", "-q"], cwd=repo)
    _git(git, ["config", "user.email", "test@example.com"], cwd=repo)
    _git(git, ["config", "user.name", "Test"], cwd=repo)
    _git(git, ["commit", "-q", "--allow-empty", "-m", "base"], cwd=repo)


def test_worktree_is_dirty_checks_the_whole_tree(tmp_path: Path) -> None:
    """The workspace check has no pathspec exclusions."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    _init_repo(git, repo)
    client = IssueClient(CommandRunner(), repo)

    assert bootstrap_module._worktree_is_dirty(client) is False
    (repo / "untracked-file.py").write_text("x\n")
    assert bootstrap_module._worktree_is_dirty(client) is True


def test_worktree_is_dirty_honors_explicit_untracked_files(tmp_path: Path) -> None:
    """A repo-local status.showUntrackedFiles setting cannot hide files."""
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    _init_repo(git, repo)
    _git(git, ["config", "status.showUntrackedFiles", "no"], cwd=repo)
    client = IssueClient(CommandRunner(), repo)

    (repo / "untracked-file.py").write_text("x\n")
    assert bootstrap_module._worktree_is_dirty(client) is True


def test_execute_bootstrap_rejects_workspace_dirtied_during_generation(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A workspace change during Oracle generation fails closed as stale."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue, issue]
    FakeIssueClient.branches = ["main", "main"]
    FakeIssueClient.shas = [SHA_A, SHA_A]
    FakeIssueClient.heads = [SHA_A.encode(), SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b"", b"?? new-file.py\n"]
    install_orchestration_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

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
            raise ReviewLoopError(EXIT_PRECONDITION, "state", "issue must be open")
        return super().snapshot()


def test_execute_bootstrap_propagates_issue_closed_during_generation(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """An Issue closed during prompt generation surfaces its state failure."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = [SHA_A.encode()]
    FakeIssueClient.local_branches = [b"feature"]
    FakeIssueClient.statuses = [b""]
    mocker.patch.object(bootstrap_module, "IssueClient", ClosingIssueClient)
    install_oracle_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "state"


class MissingBaseIssueClient(FakeIssueClient):
    """Simulate a base commit absent from the local checkout."""

    def ensure_commit_object(self, sha: str) -> None:  # ruff: ignore[no-self-use] -- overrides base
        """Fail closed as the shared immutable-Git mixin would."""
        raise ReviewLoopError(
            EXIT_PRECONDITION, "git", f"{sha} is not a commit object"
        )


def test_execute_bootstrap_names_fetch_remedy_when_base_missing(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A missing local base commit names the fetch remedy, not just the SHA."""
    issue = sample_issue()
    FakeIssueClient.snapshots = [issue]
    FakeIssueClient.branches = ["main"]
    FakeIssueClient.shas = [SHA_A]
    FakeIssueClient.heads = []
    FakeIssueClient.local_branches = []
    FakeIssueClient.statuses = []
    mocker.patch.object(bootstrap_module, "IssueClient", MissingBaseIssueClient)
    install_oracle_fakes(mocker)

    with pytest.raises(ReviewLoopError) as captured:
        _execute(tmp_path)

    assert captured.value.category == "git"
    assert "git fetch origin main" in str(captured.value)
