"""Contract, race, and safety tests for deterministic PR submission."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING, override

import pytest
from scripts.models import (
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    ReviewLoopError,
)
from scripts.process import CommandError, CommandResult, CommandRunner
from scripts.submit import (
    _contains_gitlink_change,
    _staged_object_ids,
    execute_submit,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        message = "git is required for submit integration tests"
        raise RuntimeError(message)
    return executable


GIT = _git_executable()


def _run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        list(args),
        cwd=cwd,
        env=None if env is None else dict(env),
        input=None if input_text is None else input_text.encode(),
        check=check,
        capture_output=True,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run_process([GIT, *args], cwd=repo)
    return result.stdout.decode("utf-8", "strict").strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path, JsonObject, str, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _run_process([GIT, "init", "--bare", str(remote)], cwd=tmp_path)
    _run_process([GIT, "clone", str(remote), str(repo)], cwd=tmp_path)
    _git(repo, "config", "user.name", "PR Review Loop Test")
    _git(repo, "config", "user.email", "pr-review-loop@example.invalid")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "file.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "commit", "-am", "feature")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-u", "origin", "feature")
    state: JsonObject = {
        "url": "https://github.com/acme/demo/pull/1",
        "number": 1,
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "baseRefOid": base,
        "headRefName": "feature",
        "headRefOid": head,
        "headRepository": {
            "nameWithOwner": "acme/demo",
            "name": "demo",
        },
        "headRepositoryOwner": {"login": "acme"},
    }
    return repo, remote, state, base, head


class ScenarioRunner(CommandRunner):
    """Execute local Git while replacing GitHub CLI reads with mutable state."""

    def __init__(
        self,
        repo: Path,
        remote: Path,
        state: JsonObject,
        *,
        lease_loss_sha: str | None = None,
        fail_push_after_success: bool = False,
        source_env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(source_env)
        self.repo = repo
        self.remote = remote
        self.state = state
        self.lease_loss_sha = lease_loss_sha
        self.fail_push_after_success = fail_push_after_success
        self.push_count = 0

    @override
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = 24 * 1024 * 1024,
        watch_path: Path | None = None,
    ) -> CommandResult:
        del timeout, watch_path
        argv = [str(value) for value in args]
        if argv[0] == "gh":
            payload = json.dumps(self.state).encode()
            return CommandResult(tuple(argv), 0, payload, "")
        if argv[:3] == ["git", "remote", "get-url"]:
            remote = b"https://github.com/acme/demo.git\n"
            return CommandResult(tuple(argv), 0, remote, "")
        if argv[:2] == ["git", "push"]:
            self.push_count += 1
            if self.lease_loss_sha is not None:
                _run_process(
                    [
                        GIT,
                        "push",
                        str(self.remote),
                        f"{self.lease_loss_sha}:refs/heads/feature",
                    ],
                    cwd=self.repo,
                )
                self.state["headRefOid"] = self.lease_loss_sha
                self.lease_loss_sha = None

        completed = _run_process(
            argv,
            cwd=cwd,
            env=env,
            input_text=input_text,
            check=False,
        )
        stdout = completed.stdout
        stderr = self.redact(completed.stderr.decode("utf-8", "replace"))
        if len(stdout) > max_output:
            message = "command output exceeded bound"
            raise CommandError(message)
        if argv[:2] == ["git", "push"] and completed.returncode == 0:
            candidate = argv[-1].split(":", 1)[0]
            self.state["headRefOid"] = candidate
            if self.fail_push_after_success:
                self.fail_push_after_success = False
                message = "ambiguous transport failure"
                raise CommandError(
                    message,
                    returncode=1,
                    stdout="",
                    stderr="transport closed",
                )
        if check and completed.returncode != 0:
            detail = stderr.strip() or stdout.decode("utf-8", "replace").strip()
            message = detail or f"command failed: {' '.join(argv)}"
            raise CommandError(
                message,
                returncode=completed.returncode,
                stdout=self.redact(stdout.decode("utf-8", "replace")),
                stderr=stderr,
            )
        return CommandResult(
            tuple(argv),
            completed.returncode,
            stdout,
            stderr,
        )


def _modify(repo: Path, value: str = "fixed\n") -> None:
    (repo / "file.txt").write_text(value, encoding="utf-8")


def _raw_record(
    status: str,
    *,
    old_mode: str = "100644",
    new_mode: str = "100644",
    paths: tuple[str, ...] = ("file.txt",),
) -> bytes:
    """Return one terminated raw Git diff record for parser tests."""
    header = f":{old_mode} {new_mode} {'a' * 40} {'b' * 40} {status}\0"
    return header.encode() + b"\0".join(path.encode() for path in paths) + b"\0"


def test_shared_raw_parser_accepts_scores_and_gitlink_modes() -> None:
    """Shared raw parsing accepts Git scores and exposes both modes."""
    assert _staged_object_ids(_raw_record("M100")) == ["b" * 40]
    assert _staged_object_ids(_raw_record("R100", paths=("old.txt", "new.txt"))) == [
        "b" * 40
    ]
    assert _contains_gitlink_change(_raw_record("M100", old_mode="160000")) is True


@pytest.mark.parametrize(
    ("status", "paths"),
    [
        ("R", ("old.txt", "new.txt")),
        ("C101", ("old.txt", "new.txt")),
        ("A100", ("file.txt",)),
        ("T100", ("file.txt",)),
        ("M101", ("file.txt",)),
        ("Mbad", ("file.txt",)),
    ],
)
def test_shared_raw_parser_rejects_invalid_scores(
    status: str,
    paths: tuple[str, ...],
) -> None:
    """Shared raw parsing rejects missing, out-of-range, and nonnumeric scores."""
    with pytest.raises(ReviewLoopError) as captured:
        _staged_object_ids(_raw_record(status, paths=paths))

    assert captured.value.category == "git"


def test_success_creates_single_child_and_lease_protected_remote_head(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _modify(repo)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    assert result.previous_head_sha == head
    assert result.resulting_head_sha == result.commit_sha
    assert result.pushed_branch == "feature"
    assert _git(repo, "rev-parse", "HEAD^") == head
    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == result.commit_sha
    assert _git(repo, "show", "-s", "--format=%s", result.commit_sha) == (
        "apply reviewed changes"
    )
    assert "new.txt" in _git(
        repo, "show", "--name-only", "--format=", result.commit_sha
    )


def test_submit_requires_full_expected_head(tmp_path: Path) -> None:
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head="abc123",
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "sha"


def test_submit_rejects_remote_head_that_no_longer_matches_review(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    state["headRefOid"] = "c" * 40
    _modify(repo)

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_head"


def test_submit_rejects_local_head_not_equal_to_reviewed_head(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "other.txt").write_text("local commit\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "local drift")
    _modify(repo)

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.category == "stale_workspace"


def test_submit_rejects_empty_workspace_patch(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.category == "empty_patch"


def test_submit_rejects_whitespace_errors(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _modify(repo, "fixed   \n")

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.category == "git"


def test_submit_rejects_known_credential_in_staged_blob(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    secret = "credential-secret-token"
    _modify(repo, f"fixed {secret}\n")
    source_env = dict(os.environ)
    source_env["GH_TOKEN"] = secret
    runner = ScenarioRunner(
        repo,
        remote,
        state,
        source_env=source_env,
    )

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "credentials"
    assert secret not in str(captured.value)


def test_submit_detects_lease_loss_without_overwriting_concurrent_update(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _modify(repo)
    other = tmp_path / "other"
    _run_process([GIT, "clone", str(remote), str(other)], cwd=tmp_path)
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "checkout", "feature")
    (other / "concurrent.txt").write_text("other\n", encoding="utf-8")
    _git(other, "add", "concurrent.txt")
    _git(other, "commit", "-m", "concurrent")
    concurrent_sha = _git(other, "rev-parse", "HEAD")
    _git(repo, "fetch", str(other), concurrent_sha)

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(
                repo,
                remote,
                state,
                lease_loss_sha=concurrent_sha,
            ),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "lease_lost"
    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == concurrent_sha


def test_ambiguous_push_failure_is_accepted_only_when_exact_commit_landed(
    tmp_path: Path,
) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _modify(repo)
    runner = ScenarioRunner(
        repo,
        remote,
        state,
        fail_push_after_success=True,
    )

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
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


def test_submit_rejects_gitlink_change(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    subrepo = repo / "vendor" / "sub"
    subrepo.mkdir(parents=True)
    _git(subrepo, "init", "-q")
    _git(subrepo, "config", "user.name", "Nested")
    _git(subrepo, "config", "user.email", "nested@example.invalid")
    (subrepo / "nested.txt").write_text("nested\n", encoding="utf-8")
    _git(subrepo, "add", "nested.txt")
    _git(subrepo, "commit", "-m", "nested")
    _git(repo, "add", "vendor/sub")

    with pytest.raises(ReviewLoopError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=ScenarioRunner(repo, remote, state),
        )

    assert captured.value.category == "submodule"


def test_submit_uses_hook_free_unsigned_commit(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "commit.gpgSign", "true")
    _modify(repo)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=ScenarioRunner(repo, remote, state),
    )

    assert result.commit_sha == _git(repo, "rev-parse", "HEAD")
