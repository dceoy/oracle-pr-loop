"""Contract, race, and safety tests for deterministic PR submission."""

from __future__ import annotations

import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import typing
from typing import TYPE_CHECKING

import pytest
from scripts import cli
from scripts import submit as submit_module
from scripts.models import (
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    LooprError,
    SubmitResult,
)
from scripts.process import CommandError, CommandResult, CommandRunner
from scripts.submit import execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def _git_executable() -> str:
    """Return the Git executable required by integration tests."""
    executable = shutil.which("git")
    if executable is None:
        msg = "git is required for submit integration tests"
        raise RuntimeError(msg)
    return executable


GIT = _git_executable()


class ScenarioRunner(CommandRunner):
    """Run real local Git commands while simulating GitHub PR reads."""

    def __init__(
        self,
        repo: Path,
        remote: Path,
        state: JsonObject,
        *,
        lease_loss_sha: str | None = None,
        base_advance_sha: str | None = None,
    ) -> None:
        """Initialize one isolated submission scenario."""
        super().__init__()
        self.repo = repo
        self.remote = remote
        self.state = state
        self.lease_loss_sha = lease_loss_sha
        self.base_advance_sha = base_advance_sha

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
        """Intercept GitHub and remote identity calls around real Git commands."""
        del timeout, watch_path
        argv = [str(value) for value in args]
        if argv[0] == "gh":
            payload = json.dumps(self.state).encode()
            return CommandResult(tuple(argv), 0, payload, "")
        if argv[:3] == ["git", "remote", "get-url"]:
            remote = b"https://github.com/acme/demo.git\n"
            return CommandResult(tuple(argv), 0, remote, "")
        if argv[:2] == ["git", "push"] and self.lease_loss_sha is not None:
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
            msg = "command output exceeded bound"
            raise CommandError(msg)
        if check and completed.returncode != 0:
            detail = stderr.strip() or stdout.decode("utf-8", "replace").strip()
            raise CommandError(detail or f"command failed: {' '.join(argv)}")
        if argv[:2] == ["git", "push"] and completed.returncode == 0:
            self.state["headRefOid"] = argv[-1].split(":", 1)[0]
            if self.base_advance_sha is not None:
                self.state["baseRefOid"] = self.base_advance_sha
                self.base_advance_sha = None
        return CommandResult(tuple(argv), completed.returncode, stdout, stderr)


def _run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one trusted test command with captured byte streams."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        list(args),
        cwd=cwd,
        env=None if env is None else dict(env),
        input=None if input_text is None else input_text.encode(),
        check=check,
        capture_output=True,
    )


def _git(repo: Path, *args: str) -> str:
    """Run Git in one disposable repository and return UTF-8 stdout."""
    result = _run_process([GIT, *args], cwd=repo)
    return result.stdout.decode("utf-8", "strict").strip()


def _fixture_repo(
    tmp_path: Path,
) -> tuple[Path, Path, JsonObject, str, str]:
    """Create a bare remote, a feature branch, and matching PR state."""
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
        "title": "Feature",
        "body": "Feature body",
        "author": {"login": "author"},
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "baseRefOid": base,
        "headRefName": "feature",
        "headRefOid": head,
        "headRepository": {"nameWithOwner": "acme/demo", "name": "demo"},
        "headRepositoryOwner": {"login": "acme"},
        "files": [{"path": "file.txt"}],
        "changedFiles": 1,
    }
    return repo, remote, state, base, head


def test_success_commits_and_pushes_without_runtime_files(tmp_path: Path) -> None:
    """A valid patch becomes one commit and the exact remote PR head."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
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
    assert (
        _git(repo, "show", "-s", "--format=%s", result.commit_sha)
        == "apply reviewed changes"
    )
    assert not (repo / "artifacts").exists()


def test_submit_succeeds_when_github_caps_changed_file_inventory(
    tmp_path: Path,
) -> None:
    """Submit ignores review-only inventory capped at GitHub's 100-file limit."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    state["files"] = [{"path": f"file-{index}.txt"} for index in range(100)]
    state["changedFiles"] = 101
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    assert result.previous_head_sha == head
    assert result.resulting_head_sha == result.commit_sha
    assert _git(repo, "rev-parse", "HEAD^") == head


class HookScenarioRunner(ScenarioRunner):
    """Run real Git commands through narrow per-scenario intercept/observe hooks.

    Scenario runners below customize behavior by overriding `intercept`
    (called before a command runs; a non-None return replaces it, and
    raising simulates a failed or lost response) and `observe` (called
    after a command actually ran) instead of restating the full
    `CommandRunner.run` signature and forwarding boilerplate.
    """

    def intercept(  # ruff: ignore[no-self-use] -- overridden with self by subclasses below
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        """Return a result to replace argv's real execution, or None to run it."""
        del argv, env
        return None

    def observe(  # ruff: ignore[no-self-use] -- overridden with self by subclasses below
        self,
        argv: tuple[str, ...],
        result: CommandResult,
        env: Mapping[str, str],
    ) -> CommandResult:
        """Inspect, and optionally replace, one already-completed result.

        Returns:
            result, or a scenario-specific replacement.
        """
        del argv, env
        return result

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
        argv = tuple(str(value) for value in args)
        replacement = self.intercept(argv, env)
        if replacement is not None:
            return replacement
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
        return self.observe(argv, result, env)


class RecordingScenarioRunner(HookScenarioRunner):
    """Record every intercepted command's argv and environment."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.commands: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        self.commands.append((argv, env))
        return None


def test_success_push_is_hardened_against_recursive_submodules_and_tags(
    tmp_path: Path,
) -> None:
    """The guarded push always disables recursive submodules and follow-tags."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = RecordingScenarioRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    push_calls = [
        (argv, env) for argv, env in runner.commands if argv[:2] == ("git", "push")
    ]
    assert len(push_calls) == 1
    argv, env = push_calls[0]
    assert "--recurse-submodules=no" in argv
    assert "--no-verify" in argv
    lease_flags = [value for value in argv if value.startswith("--force-with-lease=")]
    assert lease_flags == [f"--force-with-lease=refs/heads/feature:{head}"]
    assert argv[-1] == f"{result.commit_sha}:refs/heads/feature"
    assert env.get("GIT_CONFIG_PARAMETERS", "").endswith("'push.followTags=false'")


def test_legacy_runtime_artifacts_are_never_staged(tmp_path: Path) -> None:
    """Pre-existing .pr-review-loop/ files from an older revision stay untracked."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    legacy_dir = repo / ".pr-review-loop" / "runs"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "submit-pr-1-old" / "staged.patch"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("stale audit artifact\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    assert result.resulting_head_sha == result.commit_sha
    committed_paths = _git(
        repo,
        "show",
        "--name-only",
        "--pretty=format:",
        result.commit_sha,
    ).splitlines()
    assert not any(path.startswith(".pr-review-loop") for path in committed_paths)
    assert legacy_file.exists()
    status = _git(repo, "status", "--porcelain", "--", ".pr-review-loop")
    assert status.strip().startswith("??")


def test_locally_ignored_legacy_artifacts_do_not_break_staging(
    tmp_path: Path,
) -> None:
    """An upgraded checkout with .pr-review-loop/ in .git/info/exclude still submits."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.write_text(".pr-review-loop/\n", encoding="utf-8")
    legacy_dir = repo / ".pr-review-loop" / "runs"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "leftover.txt").write_text("stale\n", encoding="utf-8")
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    assert result.resulting_head_sha == result.commit_sha
    committed_paths = _git(
        repo,
        "show",
        "--name-only",
        "--pretty=format:",
        result.commit_sha,
    ).splitlines()
    assert not any(path.startswith(".pr-review-loop") for path in committed_paths)


def test_pre_staged_legacy_artifact_fails_before_commit(tmp_path: Path) -> None:
    """A legacy artifact staged before submit runs cannot be committed."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    legacy_dir = repo / ".pr-review-loop" / "runs"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "leftover.txt"
    legacy_file.write_text("stale audit artifact\n", encoding="utf-8")
    _git(repo, "add", "--", ".pr-review-loop")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "legacy_artifacts"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_tracked_legacy_artifact_fails_before_commit(tmp_path: Path) -> None:
    """A .pr-review-loop path already tracked in history blocks submission."""
    repo, remote, state, _base, _head = _fixture_repo(tmp_path)
    tracked_dir = repo / ".pr-review-loop"
    tracked_dir.mkdir()
    (tracked_dir / "tracked.txt").write_text("was committed\n", encoding="utf-8")
    _git(repo, "add", "--", ".pr-review-loop")
    _git(repo, "commit", "-m", "accidentally track legacy artifact")
    tracked_head = _git(repo, "rev-parse", "HEAD")
    state["headRefOid"] = tracked_head
    _git(repo, "push", "origin", "feature")
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=tracked_head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "legacy_artifacts"
    assert _git(repo, "rev-parse", "HEAD") == tracked_head


def test_empty_workspace_fails_without_commit(tmp_path: Path) -> None:
    """An unchanged workspace cannot create an empty commit."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "empty_patch"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_stale_remote_head_fails_before_staging(tmp_path: Path) -> None:
    """A stale expected head fails before any local patch mutation."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    state["headRefOid"] = "a" * 40
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_head"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_lease_loss_does_not_overwrite_remote(tmp_path: Path) -> None:
    """A concurrent branch update wins and the explicit lease rejects the push."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    competitor = _git(
        repo,
        "commit-tree",
        f"{head}^{{tree}}",
        "-p",
        head,
        "-m",
        "competitor",
    )
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(
        repo,
        remote,
        state,
        lease_loss_sha=competitor,
    )

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "lease_lost"
    remote_head = _git(
        repo,
        "ls-remote",
        str(remote),
        "refs/heads/feature",
    ).split()[0]
    assert remote_head == competitor


def test_unresolved_conflict_fails_before_staging(tmp_path: Path) -> None:
    """Unmerged index entries fail before the complete patch is staged."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    _git(repo, "checkout", "-b", "conflicting", "main")
    (repo / "file.txt").write_text("conflicting\n", encoding="utf-8")
    _git(repo, "commit", "-am", "conflicting")
    _git(repo, "checkout", "feature")
    merge = _run_process(
        [GIT, "merge", "conflicting"],
        cwd=repo,
        check=False,
    )
    assert merge.returncode != 0
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "conflict"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_local_origin_mismatch_fails_before_staging(tmp_path: Path) -> None:
    """A canonical PR URL cannot redirect submission to another repository."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="https://github.com/other/demo/pull/1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "repository"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_untracked_whitespace_error_fails_before_commit(tmp_path: Path) -> None:
    """Staging exposes whitespace errors in relevant untracked files."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "new.txt").write_text("trailing  \n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "git"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_known_credential_in_staged_blob_fails_before_commit(tmp_path: Path) -> None:
    """Known environment credential values cannot enter staged blobs."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    credential = "known-test-credential"
    (repo / "file.txt").write_text(f"{credential}\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)
    runner.secrets.add(credential)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "credentials"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_known_credential_in_binary_blob_fails_before_commit(
    tmp_path: Path,
) -> None:
    """Binary diff encoding cannot hide a known credential value."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    credential = "known-binary-credential"
    (repo / "secret.bin").write_bytes(
        b"\x00prefix\x00" + credential.encode() + b"\x00suffix\x00"
    )
    runner = ScenarioRunner(repo, remote, state)
    runner.secrets.add(credential)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "credentials"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_base_advance_after_push_keeps_success(tmp_path: Path) -> None:
    """A post-write base advance does not negate the exact pushed head."""
    repo, remote, state, base, head = _fixture_repo(tmp_path)
    advanced_base = "f" * 40
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(
        repo,
        remote,
        state,
        base_advance_sha=advanced_base,
    )

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
        runner=runner,
    )

    assert state["baseRefOid"] == advanced_base
    assert result.base_sha == base
    assert result.resulting_head_sha == result.commit_sha


def test_malformed_head_ref_fails_before_staging(tmp_path: Path) -> None:
    """Unsafe remote branch names never reach Git argument construction."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    state["headRefName"] = "-unsafe"
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.category == "ref"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_submit_cli_emits_the_stable_success_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The public command emits exactly one submit JSON object."""
    expected = SubmitResult(
        repository="acme/demo",
        pr_number=1,
        base_sha="b" * 40,
        previous_head_sha="a" * 40,
        resulting_head_sha="c" * 40,
        commit_sha="c" * 40,
        pushed_branch="feature",
    )

    def fake_submit(**_kwargs: object) -> SubmitResult:
        return expected

    monkeypatch.setattr(cli, "execute_submit", fake_submit)
    exit_code = cli.main([
        "submit",
        "--pr",
        "1",
        "--expected-head",
        "a" * 40,
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == expected.as_json()


class MultiplePushUrlRunner(HookScenarioRunner):
    """Expose two configured push destinations."""

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        """Return every configured push URL for the submit preflight."""
        del env
        if argv == ("git", "remote", "get-url", "--push", "--all", "origin"):
            return CommandResult(
                argv,
                0,
                b"https://github.com/acme/demo.git\nhttps://github.com/acme/mirror.git\n",
                "",
            )
        return None


class AmbiguousPushRunner(HookScenarioRunner):
    """Update the remote, then simulate a lost local push response."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.fail_after_push = True

    @typing.override
    def observe(
        self,
        argv: tuple[str, ...],
        result: CommandResult,
        env: Mapping[str, str],
    ) -> CommandResult:
        del env
        if argv[:2] == ("git", "push") and self.fail_after_push:
            self.fail_after_push = False
            msg = "connection dropped after remote update"
            raise CommandError(msg)
        return result


class ClosedAfterPushRunner(HookScenarioRunner):
    """Close the PR immediately after the remote accepts the commit."""

    @typing.override
    def observe(
        self,
        argv: tuple[str, ...],
        result: CommandResult,
        env: Mapping[str, str],
    ) -> CommandResult:
        del env
        if argv[:2] == ("git", "push") and result.returncode == 0:
            self.state["state"] = "CLOSED"
        return result


class TransientGitHubAfterPushRunner(HookScenarioRunner):
    """Fail the first GitHub snapshot after a successful push."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.push_completed = False
        self.fail_next_snapshot = True

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if (
            argv[:3] == ("gh", "pr", "view")
            and self.push_completed
            and self.fail_next_snapshot
        ):
            self.fail_next_snapshot = False
            msg = "temporary GitHub API failure"
            raise CommandError(msg)
        return None

    @typing.override
    def observe(
        self,
        argv: tuple[str, ...],
        result: CommandResult,
        env: Mapping[str, str],
    ) -> CommandResult:
        del env
        if argv[:2] == ("git", "push") and result.returncode == 0:
            self.push_completed = True
        return result


class BranchOnlyLeaseLossRunner(HookScenarioRunner):
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

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if argv[:2] == ("git", "push") and self.competitor:
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
        return None


class TransientRemoteConfirmationRunner(HookScenarioRunner):
    """Lose the push response and the first recovery read."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.fail_after_push = True
        self.fail_first_recovery = True
        self.remote_updated = False

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if (
            argv[:4] == ("git", "ls-remote", "--refs", "origin")
            and self.remote_updated
            and self.fail_first_recovery
        ):
            self.fail_first_recovery = False
            msg = "temporary remote confirmation failure"
            raise CommandError(msg)
        return None

    @typing.override
    def observe(
        self,
        argv: tuple[str, ...],
        result: CommandResult,
        env: Mapping[str, str],
    ) -> CommandResult:
        del env
        if argv[:2] == ("git", "push") and self.fail_after_push:
            self.fail_after_push = False
            self.remote_updated = True
            msg = "connection dropped after remote update"
            raise CommandError(msg)
        return result


class DelayedRemoteAcceptanceRunner(HookScenarioRunner):
    """Expose the expected head once before the remote accepts the push."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.pending_commit: str | None = None
        self.recovery_reads = 0

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if argv[:2] == ("git", "push"):
            self.pending_commit = argv[-1].split(":", 1)[0]
            msg = "connection dropped before remote confirmation"
            raise CommandError(msg)
        if (
            argv[:4] == ("git", "ls-remote", "--refs", "origin")
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
        return None


class RecoveryDeadlineRunner(HookScenarioRunner):
    """Advance the remote only after ambiguous-push recovery reaches its deadline."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        super().__init__(repo, remote, state)
        self.pending_commit: str | None = None
        self.recovery_reads = 0

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if argv[:2] == ("git", "push"):
            self.pending_commit = argv[-1].split(":", 1)[0]
            msg = "connection dropped before remote confirmation"
            raise CommandError(msg)
        if (
            argv[:4] == ("git", "ls-remote", "--refs", "origin")
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
        return None


class RefRebindingRunner(HookScenarioRunner):
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

    @typing.override
    def intercept(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
    ) -> CommandResult | None:
        del env
        if argv[:3] == ("gh", "pr", "view"):
            self.snapshot_count += 1
            if self.snapshot_count == 2:
                self.state[self.field] = self.replacement
        return None


def test_multiple_push_urls_fail_before_staging(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = MultiplePushUrlRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
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
        runner=runner,
    )

    remote_head = _git(repo, "ls-remote", str(remote), "refs/heads/feature").split()[0]
    assert remote_head == result.commit_sha
    assert result.resulting_head_sha == result.commit_sha


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
        runner=runner,
    )

    assert runner.fail_next_snapshot is False
    assert result.resulting_head_sha == result.commit_sha


def test_lease_loss_does_not_publish_local_tags(tmp_path: Path) -> None:
    """A concurrent branch update prevents publishing an unrelated local tag."""
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    _git(repo, "tag", "-a", "candidate", "-m", "candidate", head)
    _git(repo, "config", "push.followTags", "true")
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
            runner=runner,
        )

    assert captured.value.category == "lease_lost"
    assert _git(repo, "ls-remote", str(remote), "refs/tags/candidate") == ""


def test_gitlink_change_is_rejected_before_push(tmp_path: Path) -> None:
    """A candidate that changes a submodule pointer never reaches the push."""
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

    (submodule / "submodule.txt").write_text("unpublished\n", encoding="utf-8")
    _git(submodule, "commit", "-am", "unpublished submodule")
    unpublished_submodule_head = _git(submodule, "rev-parse", "HEAD")
    assert unpublished_submodule_head != published_submodule_head
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "submodule"
    remote_head = _git(repo, "ls-remote", str(remote), "refs/heads/feature").split()[0]
    assert remote_head == head
    remote_submodule_head = _git(
        submodule,
        "ls-remote",
        str(submodule_remote),
        "refs/heads/main",
    ).split()[0]
    assert remote_submodule_head == published_submodule_head


def test_transient_remote_confirmation_after_push_is_retried(tmp_path: Path) -> None:
    repo, remote, state, _base, head = _fixture_repo(tmp_path)
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = TransientRemoteConfirmationRunner(repo, remote, state)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
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
    monkeypatch.setattr(submit_module, "POLL_INTERVAL_SECONDS", 0)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
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
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
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
    monkeypatch.setattr(submit_module, "POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(submit_module, "POLL_INTERVAL_SECONDS", 0)

    result = execute_submit(
        pr_value="1",
        expected_head=head,
        repo_dir=repo,
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
        execute_submit(
            pr_value="1",
            expected_head=head,
            repo_dir=repo,
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
        execute_submit(
            pr_value="1",
            expected_head=expected_head,
            repo_dir=repo,
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
