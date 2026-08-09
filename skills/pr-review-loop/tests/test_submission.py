"""Contract and race tests for deterministic PR submission."""

from __future__ import annotations

import json
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING

import pytest
from scripts import cli
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
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "baseRefOid": base,
        "headRefName": "feature",
        "headRefOid": head,
        "headRepository": {"nameWithOwner": "acme/demo", "name": "demo"},
        "headRepositoryOwner": {"login": "acme"},
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
    assert not (repo / "artifacts").exists()


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
