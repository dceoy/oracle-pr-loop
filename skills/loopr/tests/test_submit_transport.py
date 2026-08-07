"""Regression tests for hardened submit transport behavior."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from test_submit_command import (
    GIT,
    ScenarioRunner,
    _fixture_repo,
    _git,
    _run_process,
)

from scripts.models import EXIT_PRECONDITION, JsonObject, LooprError
from scripts.process import CommandError, CommandResult
from scripts.submit import execute_submit


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
        if argv == [
            "git",
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        ]:
            return CommandResult(
                tuple(argv),
                0,
                (
                    b"https://github.com/acme/demo.git\n"
                    b"https://github.com/acme/mirror.git\n"
                ),
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
        """Initialize one post-update failure."""
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
        """Raise only after the real local push has updated the remote."""
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
            raise CommandError("connection dropped after remote update")
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
        """Expose a post-write state transition before GitHub confirmation."""
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
        """Initialize one transient post-write GitHub failure."""
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
        """Raise once after the remote write, then expose the pushed head."""
        argv = [str(value) for value in args]
        if (
            argv[:3] == ["gh", "pr", "view"]
            and self.push_completed
            and self.fail_next_snapshot
        ):
            self.fail_next_snapshot = False
            raise CommandError("temporary GitHub API failure")
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
        """Initialize one branch-only concurrent update."""
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
        """Simulate the competitor without inheriting local push configuration."""
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


def test_multiple_push_urls_fail_before_staging(tmp_path: Path) -> None:
    """A second push destination is rejected before local mutation."""
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
    """A post-update command error proceeds through GitHub confirmation."""
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

    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == result.commit_sha
    assert result.resulting_head_sha == result.commit_sha
    assert (Path(result.artifacts_dir) / "push.json").is_file()


def test_known_credential_in_path_fails_before_commit(tmp_path: Path) -> None:
    """Patch metadata cannot carry a known credential in a pathname."""
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
    """A post-write close does not turn the exact successful push into failure."""
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
    assert (Path(result.artifacts_dir) / "push.json").is_file()


def test_transient_github_failure_after_push_is_retried(tmp_path: Path) -> None:
    """A temporary post-write GitHub failure does not negate the exact push."""
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
    assert (Path(result.artifacts_dir) / "push.json").is_file()


def test_lease_loss_does_not_write_tags_or_submodule_remotes(
    tmp_path: Path,
) -> None:
    """A rejected PR push cannot mutate any configuration-added destination."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)

    submodule_remote = tmp_path / "submodule.git"
    submodule_source = tmp_path / "submodule-source"
    _run_process([GIT, "init", "--bare", str(submodule_remote)], cwd=tmp_path)
    _run_process(
        [GIT, "clone", str(submodule_remote), str(submodule_source)],
        cwd=tmp_path,
    )
    _git(submodule_source, "config", "user.name", "Loopr Test")
    _git(submodule_source, "config", "user.email", "loopr@example.invalid")
    (submodule_source / "submodule.txt").write_text(
        "published\n",
        encoding="utf-8",
    )
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
    _git(submodule, "config", "user.name", "Loopr Test")
    _git(submodule, "config", "user.email", "loopr@example.invalid")
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
