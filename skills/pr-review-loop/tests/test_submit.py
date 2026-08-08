"""Regression tests for guarded submission transport behavior."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts import submission as submission_module
from scripts.models import EXIT_PRECONDITION, EXIT_RACE, JsonObject, LooprError
from scripts.process import CommandError, CommandResult
from scripts.submit import execute_guarded, execute_submit
from test_submission import GIT, ScenarioRunner, _fixture_repo, _git, _run_process

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class MultiplePushUrlRunner(ScenarioRunner):
    """Expose two configured push destinations."""

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
        """Return every configured push URL for the guarded preflight."""
        argv = [str(value) for value in args]
        if argv == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            return CommandResult(
                tuple(argv),
                0,
                b"https://github.com/acme/demo.git\nhttps://github.com/acme/mirror.git\n",
                "",
            )
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


class AmbiguousPushRunner(ScenarioRunner):
    """Update the remote, then simulate a lost local push response."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.fail_after_push = True

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
        argv = [str(value) for value in args]
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[:2] == ["git", "push"] and self.fail_after_push:
            self.fail_after_push = False
            msg = "connection dropped after remote update"
            raise CommandError(msg)
        return result


class ClosedAfterPushRunner(ScenarioRunner):
    """Close the PR immediately after the remote accepts the commit."""

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
        argv = [str(value) for value in args]
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[:2] == ["git", "push"] and result.returncode == 0:
            self.state["state"] = "CLOSED"
        return result


class TransientGitHubAfterPushRunner(ScenarioRunner):
    """Fail the first GitHub snapshot after a successful push."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.push_completed = False
        self.fail_next_snapshot = True

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
        argv = [str(value) for value in args]
        if (
            argv[:3] == ["gh", "pr", "view"]
            and self.push_completed
            and self.fail_next_snapshot
        ):
            self.fail_next_snapshot = False
            msg = "temporary GitHub API failure"
            raise CommandError(msg)
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[:2] == ["git", "push"] and result.returncode == 0:
            self.push_completed = True
        return result


class BranchOnlyLeaseLossRunner(ScenarioRunner):
    """Advance only the competing PR branch before the guarded push."""

    def __init__(
        self,
        repo: Path,
        remote: Path,
        state: JsonObject,
        competitor: str,
    ) -> None:
        super().__init__(repo, remote, state)
        self.competitor = competitor

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
        argv = [str(value) for value in args]
        if argv[:2] == ["git", "push"] and self.competitor:
            _run_process(
                [
                    GIT,
                    "-c",
                    "push.followTags=false",
                    "-c",
                    "push.recurseSubmodules=no",
                    "push",
                    str(self.remote),
                    f"{self.competitor}:refs/heads/feature",
                ],
                cwd=self.repo,
            )
            self.state["headRefOid"] = self.competitor
            self.competitor = ""
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


class TransientRemoteConfirmationRunner(ScenarioRunner):
    """Lose the push response and the first recovery read."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.fail_after_push = True
        self.fail_first_recovery = True
        self.remote_updated = False

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
        argv = [str(value) for value in args]
        if (
            argv[:4] == ["git", "ls-remote", "--refs", "origin"]
            and self.remote_updated
            and self.fail_first_recovery
        ):
            self.fail_first_recovery = False
            msg = "temporary remote confirmation failure"
            raise CommandError(msg)
        result = super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )
        if argv[:2] == ["git", "push"] and self.fail_after_push:
            self.fail_after_push = False
            self.remote_updated = True
            msg = "connection dropped after remote update"
            raise CommandError(msg)
        return result


class DelayedRemoteAcceptanceRunner(ScenarioRunner):
    """Expose the expected head once before the remote accepts the push."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.pending_commit: str | None = None
        self.recovery_reads = 0

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
        argv = [str(value) for value in args]
        if argv[:2] == ["git", "push"]:
            self.pending_commit = argv[-1].split(":", 1)[0]
            msg = "connection dropped before remote confirmation"
            raise CommandError(msg)
        if (
            argv[:4] == ["git", "ls-remote", "--refs", "origin"]
            and self.pending_commit is not None
        ):
            self.recovery_reads += 1
            if self.recovery_reads > 1:
                commit_sha = self.pending_commit
                _git(
                    self.repo,
                    "push",
                    str(self.remote),
                    f"{commit_sha}:refs/heads/feature",
                )
                self.state["headRefOid"] = commit_sha
                self.pending_commit = None
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


class RecoveryDeadlineRunner(ScenarioRunner):
    """Advance the remote only after wrapper recovery reaches its deadline."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.pending_commit: str | None = None
        self.recovery_reads = 0

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
        argv = [str(value) for value in args]
        if argv[:2] == ["git", "push"]:
            self.pending_commit = argv[-1].split(":", 1)[0]
            msg = "connection dropped before remote confirmation"
            raise CommandError(msg)
        if (
            argv[:4] == ["git", "ls-remote", "--refs", "origin"]
            and self.pending_commit is not None
        ):
            self.recovery_reads += 1
            if self.recovery_reads == 2:
                commit_sha = self.pending_commit
                _git(
                    self.repo,
                    "push",
                    str(self.remote),
                    f"{commit_sha}:refs/heads/feature",
                )
                self.state["headRefOid"] = commit_sha
                self.state["state"] = "CLOSED"
                self.pending_commit = None
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


def test_multiple_push_urls_fail_before_staging(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = MultiplePushUrlRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "repository"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_ambiguous_push_failure_accepts_updated_remote(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = AmbiguousPushRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    remote_head = _git(repo, "ls-remote", str(remote), "refs/heads/feature").split()[0]
    assert remote_head == result.commit_sha
    assert result.resulting_head_sha == result.commit_sha
    assert (Path(result.artifacts_dir) / "push.json").is_file()


def test_known_credential_in_path_fails_before_commit(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    credential = "known-path-credential"
    (repo / f"{credential}.txt").write_text("safe\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)
    runner.secrets.add(credential)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "credentials"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_closed_pr_after_push_accepts_exact_resulting_head(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ClosedAfterPushRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert state["state"] == "CLOSED"
    assert result.resulting_head_sha == result.commit_sha


def test_transient_github_failure_after_push_is_retried(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = TransientGitHubAfterPushRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.fail_next_snapshot is False
    assert result.resulting_head_sha == result.commit_sha


def test_lease_loss_does_not_write_tags_or_submodule_remotes(tmp_path: Path) -> None:
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)
    submodule_remote = tmp_path / "submodule.git"
    submodule_source = tmp_path / "submodule-source"
    _run_process([GIT, "init", "--bare", str(submodule_remote)], cwd=tmp_path)
    _run_process(
        [GIT, "clone", str(submodule_remote), str(submodule_source)], cwd=tmp_path
    )
    _git(submodule_source, "config", "user.name", "PR Review Loop Test")
    _git(submodule_source, "config", "user.email", "pr-review-loop@example.invalid")
    (submodule_source / "submodule.txt").write_text("published\n", encoding="utf-8")
    _git(submodule_source, "add", "submodule.txt")
    _git(submodule_source, "commit", "-m", "published submodule")
    _git(submodule_source, "branch", "-M", "main")
    _git(submodule_source, "push", "-u", "origin", "main")
    _git(submodule_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    published_submodule_head = _git(submodule_source, "rev-parse", "HEAD")

    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule_remote),
        "vendor/submodule",
    )
    submodule = repo / "vendor/submodule"
    _git(submodule, "config", "user.name", "PR Review Loop Test")
    _git(submodule, "config", "user.email", "pr-review-loop@example.invalid")
    _git(repo, "add", ".gitmodules", "vendor/submodule")
    _git(repo, "commit", "-m", "add submodule")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", f"{head}:refs/heads/feature")
    state["headRefOid"] = head

    _git(repo, "tag", "-a", "candidate", "-m", "candidate", head)
    _git(repo, "config", "push.followTags", "true")
    _git(repo, "config", "push.recurseSubmodules", "on-demand")
    (submodule / "submodule.txt").write_text("unpublished\n", encoding="utf-8")
    _git(submodule, "commit", "-am", "unpublished submodule")
    unpublished_submodule_head = _git(submodule, "rev-parse", "HEAD")
    assert unpublished_submodule_head != published_submodule_head

    competitor = _git(
        repo,
        "commit-tree",
        f"{head}^{{tree}}",
        "-p",
        head,
        "-m",
        "competitor",
    )
    runner = BranchOnlyLeaseLossRunner(repo, remote, state, competitor)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.category == "lease_lost"
    assert _git(repo, "ls-remote", str(remote), "refs/tags/candidate") == ""
    remote_submodule_head = _git(
        submodule,
        "ls-remote",
        str(submodule_remote),
        "refs/heads/main",
    ).split()[0]
    assert remote_submodule_head == published_submodule_head


def test_previous_artifacts_are_excluded_from_submit(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    previous = repo / ".pr-review-loop" / "runs" / "previous"
    previous.mkdir(parents=True)
    (previous / "result.json").write_text("{}\n", encoding="utf-8")
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=Path(".pr-review-loop"),
        runner=ScenarioRunner(repo, remote, state),
    )

    committed_paths = _git(
        repo, "ls-tree", "-r", "--name-only", result.commit_sha
    ).splitlines()
    assert "file.txt" in committed_paths
    assert not any(path.startswith(".pr-review-loop/") for path in committed_paths)


def test_gitignored_artifact_directory_does_not_block_staging(tmp_path: Path) -> None:
    """Staging succeeds when Git already ignores the artifact directory.

    Newer Git refuses `git add` when a pathspec argument names an already
    ignored path, even with exclude magic. Once `.pr-review-loop/` is
    covered by `.gitignore`, the exclude pathspec must not be passed to the
    staging command at all -- Git already skips ignored paths under `.`
    without it.
    """
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / ".gitignore").write_text(".pr-review-loop/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore artifacts")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "feature")
    state["headRefOid"] = head
    previous = repo / ".pr-review-loop" / "runs" / "previous"
    previous.mkdir(parents=True)
    (previous / "result.json").write_text("{}\n", encoding="utf-8")
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=Path(".pr-review-loop"),
        runner=ScenarioRunner(repo, remote, state),
    )

    committed_paths = _git(
        repo, "ls-tree", "-r", "--name-only", result.commit_sha
    ).splitlines()
    assert "file.txt" in committed_paths
    assert not any(path.startswith(".pr-review-loop/") for path in committed_paths)


def test_tracked_artifact_directory_is_rejected(tmp_path: Path) -> None:
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)
    tracked = repo / ".pr-review-loop" / "tracked.txt"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("repository content\n", encoding="utf-8")
    _git(repo, "add", ".pr-review-loop/tracked.txt")
    _git(repo, "commit", "-m", "track artifact path")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "feature")
    state["headRefOid"] = head
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=Path(".pr-review-loop"),
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "artifacts"


def test_only_previous_artifacts_remain_an_empty_patch(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    previous = repo / ".pr-review-loop" / "runs" / "previous"
    previous.mkdir(parents=True)
    (previous / "result.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=Path(".pr-review-loop"),
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "empty_patch"


def test_transient_remote_confirmation_after_push_is_retried(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = TransientRemoteConfirmationRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.fail_first_recovery is False
    assert result.resulting_head_sha == result.commit_sha


def test_expected_remote_head_after_push_error_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = DelayedRemoteAcceptanceRunner(repo, remote, state)
    monkeypatch.setattr(submission_module, "POLL_INTERVAL_SECONDS", 0)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.recovery_reads == 2
    assert runner.pending_commit is None
    assert result.resulting_head_sha == result.commit_sha


def test_resolved_merge_state_is_rejected_before_commit(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "add side change")
    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "--no-commit", "side")

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "conflict"


def test_escaped_credential_in_path_fails_before_commit(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    credential = "known\\credential"
    (repo / f"{credential}.txt").write_text("safe\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)
    runner.secrets.add(credential)

    with pytest.raises(LooprError) as captured:
        execute_guarded(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "credentials"


def test_fallback_accepts_commit_after_recovery_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = RecoveryDeadlineRunner(repo, remote, state)
    monkeypatch.setattr(submission_module, "POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(submission_module, "POLL_INTERVAL_SECONDS", 0)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        artifacts_dir=tmp_path / "artifacts",
        runner=runner,
    )

    assert runner.recovery_reads == 2
    assert runner.pending_commit is None
    assert state["state"] == "CLOSED"
    assert result.resulting_head_sha == result.commit_sha


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("baseRefName", "renamed-main"), ("headRefName", "renamed-feature")],
)
def test_ref_rebinding_fails_before_staging(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
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
        execute_guarded(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"


def test_forged_tracking_ref_cannot_publish_an_unpublished_gitlink(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)
    submodule_remote = tmp_path / "submodule.git"
    submodule_source = tmp_path / "submodule-source"
    _run_process([GIT, "init", "--bare", str(submodule_remote)], cwd=tmp_path)
    _run_process(
        [GIT, "clone", str(submodule_remote), str(submodule_source)], cwd=tmp_path
    )
    _git(submodule_source, "config", "user.name", "PR Review Loop Test")
    _git(submodule_source, "config", "user.email", "pr-review-loop@example.invalid")
    (submodule_source / "submodule.txt").write_text("published\n", encoding="utf-8")
    _git(submodule_source, "add", "submodule.txt")
    _git(submodule_source, "commit", "-m", "published submodule")
    _git(submodule_source, "branch", "-M", "main")
    _git(submodule_source, "push", "-u", "origin", "main")
    _git(submodule_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    published_submodule_head = _git(submodule_source, "rev-parse", "HEAD")

    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule_remote),
        "vendor/submodule",
    )
    submodule = repo / "vendor/submodule"
    _git(submodule, "config", "user.name", "PR Review Loop Test")
    _git(submodule, "config", "user.email", "pr-review-loop@example.invalid")
    _git(repo, "add", ".gitmodules", "vendor/submodule")
    _git(repo, "commit", "-m", "add submodule")
    expected_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", f"{expected_head}:refs/heads/feature")
    state["headRefOid"] = expected_head

    (submodule / "submodule.txt").write_text("unpublished\n", encoding="utf-8")
    _git(submodule, "commit", "-am", "unpublished submodule")
    unpublished_submodule_head = _git(submodule, "rev-parse", "HEAD")
    assert unpublished_submodule_head != published_submodule_head
    _git(
        submodule, "update-ref", "refs/remotes/origin/main", unpublished_submodule_head
    )

    with pytest.raises(LooprError) as captured:
        execute_guarded(
            pr_value="1",
            expected_head=expected_head,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "submodule"
    remote_submodule_head = _git(
        submodule,
        "ls-remote",
        str(submodule_remote),
        "refs/heads/main",
    ).split()[0]
    assert remote_submodule_head == published_submodule_head
