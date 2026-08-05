"""Contract and race tests for the vendor-neutral review command."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- tests exercise it directly
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, TypeVar, cast

import pytest

from scripts import loopr as cli, process as process_module, review as review_module
from scripts.artifacts import ArtifactWriter
from scripts.github import GitHubClient, normalize_repo, resolve_target, validate_path
from scripts.models import (
    EXIT_ORACLE,
    EXIT_PRECONDITION,
    EXIT_RACE,
    JsonObject,
    LooprError,
    OracleReview,
    PullRequest,
    ReviewResult,
)
from scripts.oracle import (
    MAX_CHANGED_FILES,
    MAX_ORACLE_OUTPUT,
    MAX_REVIEW_BODY_BYTES,
    OracleClient,
    parse_review,
)
from scripts.process import CommandError, CommandResult, CommandRunner
from scripts.review import execute_review

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
ClientT = TypeVar("ClientT", bound=GitHubClient)


def sample_pr(*, base_sha: str = SHA_A, head_sha: str = SHA_B) -> PullRequest:
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
        base_sha=base_sha,
        head_ref="feature",
        head_sha=head_sha,
        head_repository="owner/repository",
        changed_paths=("file.py",),
        raw={},
    )


def approve_review(pull_request: PullRequest) -> OracleReview:
    """Return a valid APPROVE result for a frozen snapshot."""
    return OracleReview(
        repository=pull_request.repository,
        pr_number=pull_request.number,
        base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
        verdict="APPROVE",
        review_body="Approved.",
        blocking_findings=(),
        implementation_prompt=None,
        non_blocking_notes=(),
        raw={},
    )


def snapshot_payload(*, files: list[str], changed_files: int) -> JsonObject:
    """Return a GitHub CLI PR payload."""
    return {
        "url": "https://github.com/owner/repository/pull/21",
        "number": 21,
        "title": "Title",
        "body": "Body",
        "author": {"login": "author"},
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "baseRefOid": SHA_A,
        "headRefName": "feature",
        "headRefOid": SHA_B,
        "headRepository": {"nameWithOwner": "owner/repository"},
        "headRepositoryOwner": {"login": "owner"},
        "files": [{"path": path} for path in files],
        "changedFiles": changed_files,
    }


def configured_client(client: ClientT) -> ClientT:
    """Set the resolved identity fields required by snapshot operations."""
    client.repository = "owner/repository"
    client.number = 21
    client.url = "https://github.com/owner/repository/pull/21"
    client.reviewer_login = "reviewer"
    return client


def test_target_and_path_validation() -> None:
    """Canonical targets and safe paths are accepted while traversal is rejected."""
    assert normalize_repo("https://github.com/owner/repository.git") == (
        "owner/repository"
    )
    assert normalize_repo("git@github.com:owner/repository.git") == ("owner/repository")
    assert resolve_target("21", "owner/repository")[1] == 21
    assert (
        resolve_target(
            "https://github.com/owner/repository/pull/21",
            None,
        )[1]
        == 21
    )
    assert validate_path("src/file.py") == "src/file.py"
    for value in ("../file", "/file", ".git/config", "a\\b"):
        with pytest.raises(LooprError):
            validate_path(value)


def test_snapshot_rejects_truncated_changed_file_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Advertised and materialized changed-file counts must match exactly."""
    client = configured_client(GitHubClient(CommandRunner(), tmp_path, "token"))
    payload = snapshot_payload(files=["file.py"], changed_files=2)

    def fake_text(
        args: list[str],
        *,
        reviewer: bool = False,
        input_text: str | None = None,
        max_output: int = 24 * 1024 * 1024,
    ) -> str:
        del args, reviewer, input_text, max_output
        return json.dumps(payload)

    monkeypatch.setattr(client, "_text", fake_text)
    with pytest.raises(LooprError) as captured:
        client.snapshot()
    assert captured.value.category == "inventory"


class RecordingGitHubClient(GitHubClient):
    """Capture a review API payload without running GitHub CLI."""

    last_input: str | None = None

    def _text(
        self,
        args: list[str],
        *,
        reviewer: bool = False,
        input_text: str | None = None,
        max_output: int = 24 * 1024 * 1024,
    ) -> str:
        del args, reviewer, max_output
        self.last_input = input_text
        return json.dumps({"id": 123, "commit_id": SHA_B})


def test_post_review_is_anchored_to_frozen_head(tmp_path: Path) -> None:
    """The GitHub review payload always includes the exact reviewed head SHA."""
    client = configured_client(
        RecordingGitHubClient(CommandRunner(), tmp_path, "token")
    )
    review_id, _data = client.post_review(sample_pr(), "APPROVE", "Approved.")
    assert review_id == 123
    assert client.last_input is not None
    assert json.loads(client.last_input)["commit_id"] == SHA_B


def _run_git(git: str, args: list[str], *, cwd: Path) -> str:
    """Run one trusted git command and return its captured stdout."""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed, trusted argv
        [git, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_patch_disables_external_diff_and_textconv(tmp_path: Path) -> None:
    """`patch()` ignores GIT_EXTERNAL_DIFF and textconv drivers from the environment."""
    git = shutil.which("git")
    assert git is not None
    marker = tmp_path / "external-diff-invoked"
    spy_script = tmp_path / "spy-diff.sh"
    spy_script.write_text(f"#!/bin/sh\ntouch {marker!s}\nexit 0\n")
    spy_script.chmod(0o755)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(git, ["init", "-q"], cwd=repo_dir)
    _run_git(git, ["config", "user.email", "test@example.com"], cwd=repo_dir)
    _run_git(git, ["config", "user.name", "Test"], cwd=repo_dir)

    tracked_file = repo_dir / "file.py"
    tracked_file.write_text("original\n")
    _run_git(git, ["add", "file.py"], cwd=repo_dir)
    _run_git(git, ["commit", "-q", "-m", "base"], cwd=repo_dir)
    base_sha = _run_git(git, ["rev-parse", "HEAD"], cwd=repo_dir).strip()

    tracked_file.write_text("changed\n")
    _run_git(git, ["commit", "-q", "-am", "head"], cwd=repo_dir)
    head_sha = _run_git(git, ["rev-parse", "HEAD"], cwd=repo_dir).strip()

    runner = CommandRunner({
        **os.environ,
        "GIT_EXTERNAL_DIFF": str(spy_script),
    })
    github = GitHubClient(runner, repo_dir, "token")
    pull_request = sample_pr(base_sha=base_sha, head_sha=head_sha)

    patch_bytes = github.patch(pull_request, max_output=1024 * 1024)

    assert not marker.exists()
    assert b"-original" in patch_bytes
    assert b"+changed" in patch_bytes


def test_strict_oracle_contract() -> None:
    """Oracle verdicts are accepted only with exact identity and consistency."""
    pull_request = sample_pr()
    approve: JsonObject = {
        "schema_version": 1,
        "repository": pull_request.repository,
        "pr_number": pull_request.number,
        "base_sha": pull_request.base_sha,
        "head_sha": pull_request.head_sha,
        "verdict": "APPROVE",
        "review_body": "Approved.",
        "implementation_prompt": None,
        "blocking_findings": [],
        "non_blocking_notes": [],
    }
    assert parse_review(json.dumps(approve), pull_request).verdict == "APPROVE"
    request = dict(approve)
    request.update(
        verdict="REQUEST_CHANGES",
        implementation_prompt="Fix the blocker.",
        blocking_findings=[
            {
                "id": "B1",
                "title": "Blocker",
                "description": "Description",
                "required_change": "Required change",
            }
        ],
    )
    parsed = parse_review(json.dumps(request), pull_request)
    assert parsed.implementation_prompt == "Fix the blocker."
    invalid = dict(request)
    invalid["extra"] = True
    with pytest.raises(LooprError):
        parse_review(json.dumps(invalid), pull_request)


def _review_payload(pull_request: PullRequest, *, review_body: str) -> JsonObject:
    """Return a minimal valid APPROVE payload with the given review_body."""
    return {
        "schema_version": 1,
        "repository": pull_request.repository,
        "pr_number": pull_request.number,
        "base_sha": pull_request.base_sha,
        "head_sha": pull_request.head_sha,
        "verdict": "APPROVE",
        "review_body": review_body,
        "implementation_prompt": None,
        "blocking_findings": [],
        "non_blocking_notes": [],
    }


def test_oracle_review_rejects_oversized_review_body() -> None:
    """The bound is enforced on encoded UTF-8 bytes, not character count."""
    pull_request = sample_pr()
    # "é" is 2 bytes in UTF-8: a char-count check would wrongly accept this.
    at_bound = "é" * (MAX_REVIEW_BODY_BYTES // 2)
    assert len(at_bound.encode("utf-8")) == MAX_REVIEW_BODY_BYTES
    accepted = parse_review(
        json.dumps(_review_payload(pull_request, review_body=at_bound)),
        pull_request,
    )
    assert accepted.review_body == at_bound

    over_bound = at_bound + "é"
    with pytest.raises(LooprError) as captured:
        parse_review(
            json.dumps(_review_payload(pull_request, review_body=over_bound)),
            pull_request,
        )
    assert captured.value.category == "oracle_schema"


def test_bundle_rejects_changed_file_limit(tmp_path: Path) -> None:
    """Bundle construction fails before Git reads when file count exceeds its limit."""
    changed_paths = tuple(f"file-{index}.py" for index in range(MAX_CHANGED_FILES + 1))
    pull_request = sample_pr()
    oversized = replace(pull_request, changed_paths=changed_paths)
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    with pytest.raises(LooprError) as captured:
        oracle.build_bundle(oversized)
    assert captured.value.category == "bundle"


def test_redactor_matches_legacy_credential_aliases() -> None:
    """PASSWD, ACCESS_KEY, and PRIVATE_KEY variables register as secrets."""
    runner = CommandRunner({
        "SSH_PRIVATE_KEY": "private-key-secret",
        "DB_PASSWD": "passwd-secret-value",
        "AWS_ACCESS_KEY_ID": "access-key-secret",
    })
    for secret in (
        "private-key-secret",
        "passwd-secret-value",
        "access-key-secret",
    ):
        assert runner.contains_secret(secret)
        assert runner.redact(f"leaked: {secret}") == "leaked: [REDACTED]"


def test_artifact_json_redacts_secret_before_escaping(tmp_path: Path) -> None:
    """A credential containing a quote or backslash is redacted before JSON escaping."""
    secret = 'abc"def\\ghi'
    runner = CommandRunner({"SSH_PRIVATE_KEY": secret})
    writer = ArtifactWriter(tmp_path / "artifacts", runner)

    path = writer.json("snapshot.json", {"token": secret, "nested": [secret]})

    written = path.read_text(encoding="utf-8")
    escaped_secret = json.dumps(secret)[1:-1]
    assert escaped_secret not in written
    assert written.count("[REDACTED]") == 2


def test_artifact_json_redacts_secret_in_dict_key(tmp_path: Path) -> None:
    """A credential used as a JSON object key is also redacted."""
    secret = 'abc"def\\ghi'
    runner = CommandRunner({"SSH_PRIVATE_KEY": secret})
    writer = ArtifactWriter(tmp_path / "artifacts", runner)

    path = writer.json("snapshot.json", {secret: "value"})

    written = path.read_text(encoding="utf-8")
    escaped_secret = json.dumps(secret)[1:-1]
    assert escaped_secret not in written
    assert "[REDACTED]" in written


def test_bundle_rejects_patch_with_legacy_alias_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A patch leaking a PASSWD/ACCESS_KEY/PRIVATE_KEY value is refused as evidence."""
    pull_request = sample_pr()
    runner = CommandRunner({"SSH_PRIVATE_KEY": "private-key-secret"})
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")

    def fake_patch(_pull_request: PullRequest, *, max_output: int) -> bytes:
        """Simulate a patch that leaks a legacy-alias credential value."""
        del max_output
        return b"diff --git leaked private-key-secret"

    monkeypatch.setattr(github, "patch", fake_patch)
    with pytest.raises(LooprError) as captured:
        oracle.build_bundle(pull_request)
    assert captured.value.category == "bundle"


def test_runner_terminates_on_output_overflow(tmp_path: Path) -> None:
    """Output overflow is detected from private spools before reading into memory."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"]
    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            command,
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
        )


def test_runner_terminates_on_watched_file_overflow(tmp_path: Path) -> None:
    """A watched side-effect file growing past bound terminates the process."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    watch_path = tmp_path / "watched.bin"
    script = (
        "import pathlib, time\n"
        f"pathlib.Path({str(watch_path)!r}).write_bytes(b'x' * 65536)\n"
        "time.sleep(5)\n"
    )
    command = [sys.executable, "-c", script]
    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            command,
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
            watch_path=watch_path,
        )


def _pid_is_running(pid: int) -> bool:
    """Return whether pid names a live, non-zombie process."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return "State:\tZ" not in status


def test_runner_reaps_descendants_after_leader_exits(tmp_path: Path) -> None:
    """A same-session descendant left behind by an exited leader is reaped."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        f"marker = {str(marker)!r}\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)']\n"
        ")\n"
        "with open(marker, 'w') as handle:\n"
        "    handle.write(str(child.pid))\n"
        "sys.exit(0)\n"
    )
    command = [sys.executable, "-c", script]
    result = runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    assert result.returncode == 0
    child_pid = int(marker.read_text())
    assert not _pid_is_running(child_pid)


def test_runner_detects_overflow_from_descendant_during_termination_grace(
    tmp_path: Path,
) -> None:
    """An overflow from a SIGTERM-ignoring descendant during grace is caught."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    watch_path = tmp_path / "watched.bin"
    watch_path.write_bytes(b"")
    ready_path = tmp_path / "child.ready"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import signal, time, pathlib\n"
        f"path = pathlib.Path({str(watch_path)!r})\n"
        "def on_term(*_args):\n"
        "    for _ in range(400):\n"
        "        with path.open('ab') as handle:\n"
        "            handle.write(b'x' * 64)\n"
        "        time.sleep(0.005)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        f"pathlib.Path({str(ready_path)!r}).write_text('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    leader_script = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        f"ready = pathlib.Path({str(ready_path)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.005)\n"
        "sys.exit(0)\n"
    )
    command = [sys.executable, "-c", leader_script]

    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            command,
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
            watch_path=watch_path,
        )


def test_runner_terminates_process_group_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A KeyboardInterrupt while monitoring still reaps the detached child."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    pids: list[int] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed test-controlled argv
                *args,  # type: ignore[arg-type]
                **kwargs,
            ),
        )
        pids.append(proc.pid)
        return proc

    def raise_interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        process_module,
        "subprocess",
        SimpleNamespace(
            Popen=recording_popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(
        process_module,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=raise_interrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_oracle_review_rejects_oversized_write_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An oversized `--write-output` payload is rejected via a bounded read."""
    pull_request = sample_pr()
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")

    def fake_run(command: list[str], **_kwargs: object) -> CommandResult:
        """Simulate Oracle writing an oversized verdict to --write-output."""
        raw_path = Path(command[command.index("--write-output") + 1])
        raw_path.write_bytes(b"x" * (MAX_ORACLE_OUTPUT + 1))
        return CommandResult(tuple(command), 0, b"", "")

    monkeypatch.setattr(runner, "run", fake_run)
    with pytest.raises(LooprError) as captured:
        oracle.review(pull_request, ())
    assert captured.value.category == "oracle"


def test_oracle_review_rejects_fifo_write_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A FIFO left at `--write-output` is rejected instead of blocking forever."""
    pull_request = sample_pr()
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")

    def fake_run(command: list[str], **_kwargs: object) -> CommandResult:
        """Simulate Oracle leaving a FIFO instead of a regular file."""
        raw_path = Path(command[command.index("--write-output") + 1])
        os.mkfifo(raw_path)
        return CommandResult(tuple(command), 0, b"", "")

    monkeypatch.setattr(runner, "run", fake_run)
    with pytest.raises(LooprError) as captured:
        oracle.review(pull_request, ())
    assert captured.value.category == "oracle"


def test_oracle_review_rejects_symlinked_write_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A symlink left at `--write-output` is rejected without being followed."""
    pull_request = sample_pr()
    runner = CommandRunner()
    writer = ArtifactWriter(tmp_path / "artifacts", runner)
    github = GitHubClient(runner, tmp_path, "token")
    oracle = OracleClient(runner, github, writer, "heavy")
    outside = tmp_path / "outside.json"
    outside.write_text("secret")

    def fake_run(command: list[str], **_kwargs: object) -> CommandResult:
        """Simulate Oracle leaving a symlink instead of writing a real file."""
        raw_path = Path(command[command.index("--write-output") + 1])
        raw_path.symlink_to(outside)
        return CommandResult(tuple(command), 0, b"", "")

    monkeypatch.setattr(runner, "run", fake_run)
    with pytest.raises(LooprError) as captured:
        oracle.review(pull_request, ())
    assert captured.value.category == "oracle"


def test_run_directory_retries_on_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A colliding candidate run directory is retried with a fresh suffix."""
    pull_request = sample_pr()

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(review_module.dt, "datetime", _FixedDateTime)
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(
        review_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(tokens)),
    )
    stamp = "20260101T000000Z"
    prefix = f"review-pr-{pull_request.number}-{pull_request.head_sha[:12]}"
    colliding = tmp_path / "artifacts" / "runs" / f"{prefix}-{stamp}-aaaaaaaa"
    colliding.mkdir(parents=True)

    result = review_module._run_directory(tmp_path, Path("artifacts"), pull_request)

    assert result.name == f"{prefix}-{stamp}-bbbbbbbb"
    assert result.is_dir()


def test_run_directory_rejects_symlinked_artifacts_component(tmp_path: Path) -> None:
    """A repository-controlled symlink in the artifacts path is rejected."""
    pull_request = sample_pr()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, Path("artifacts"), pull_request)

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_run_directory_rejects_symlinked_runs_component(tmp_path: Path) -> None:
    """A repository-controlled symlink for the `runs` directory is rejected."""
    pull_request = sample_pr()
    (tmp_path / "artifacts").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts" / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, Path("artifacts"), pull_request)

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_run_directory_rejects_symlinked_absolute_artifacts_dir(
    tmp_path: Path,
) -> None:
    """An absolute `--artifacts-dir` that is itself a symlink is rejected."""
    pull_request = sample_pr()
    outside = tmp_path / "outside"
    outside.mkdir()
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, artifacts_dir, pull_request)

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_run_directory_rejects_symlinked_ancestor_of_absolute_artifacts_dir(
    tmp_path: Path,
) -> None:
    """A symlink in an ancestor of an absolute `--artifacts-dir` is rejected."""
    pull_request = sample_pr()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    artifacts_dir = link / "artifacts"

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, artifacts_dir, pull_request)

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_run_directory_creates_new_absolute_artifacts_dir(tmp_path: Path) -> None:
    """A nonexistent absolute `--artifacts-dir` is created component by component."""
    pull_request = sample_pr()
    artifacts_dir = tmp_path / "does" / "not" / "exist" / "yet"

    result = review_module._run_directory(tmp_path, artifacts_dir, pull_request)

    assert result.is_dir()
    assert result.is_relative_to(artifacts_dir)


def test_run_directory_rejects_relative_artifacts_dir_traversal(
    tmp_path: Path,
) -> None:
    """A relative `--artifacts-dir` containing `..` cannot escape the checkout."""
    pull_request = sample_pr()

    with pytest.raises(LooprError) as captured:
        review_module._run_directory(tmp_path, Path("../escape"), pull_request)

    assert captured.value.category == "artifacts"


class FakeGitHubClient:
    """A deterministic PR/review transport for orchestration race tests."""

    instance: ClassVar[FakeGitHubClient | None] = None
    snapshots: ClassVar[list[PullRequest]] = []

    def __init__(
        self,
        _runner: CommandRunner,
        repo_dir: Path,
        _token: str,
    ) -> None:
        self.repo_dir = repo_dir
        self.dismissed: list[int] = []
        self.post_count = 0
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)

    def initialize(self, _pr_value: str) -> None:
        """Accept the already configured fake target."""

    def snapshot(self) -> PullRequest:
        """Return the next deterministic snapshot."""
        return self._snapshots.pop(0)

    def ensure_objects(self, _pull_request: PullRequest) -> None:
        """Treat fake SHAs as available commit objects."""

    @staticmethod
    def same_snapshot(first: PullRequest, second: PullRequest) -> bool:
        """Compare base and head SHAs."""
        return first.base_sha == second.base_sha and first.head_sha == second.head_sha

    def post_review(
        self,
        pull_request: PullRequest,
        _event: str,
        _body: str,
    ) -> tuple[int, JsonObject]:
        """Record a posted review anchored to the supplied head."""
        self.post_count += 1
        return 123, {"id": 123, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        _pull_request: PullRequest,
        _review_id: int,
    ) -> JsonObject:
        """Return a valid approved review state."""
        return {"state": "APPROVED"}

    def dismiss(self, _pull_request: PullRequest, review_id: int) -> None:
        """Record stale-review neutralization."""
        self.dismissed.append(review_id)


class FakeOracleClient:
    """Return a deterministic review without launching Oracle."""

    def __init__(
        self,
        _runner: CommandRunner,
        _github: FakeGitHubClient,
        _writer: ArtifactWriter,
        _thinking_time: str,
    ) -> None:
        pass

    def build_bundle(self, _pull_request: PullRequest) -> tuple[Path, ...]:
        """Return an empty deterministic fake bundle."""
        return ()

    def review(
        self,
        pull_request: PullRequest,
        _attachments: tuple[Path, ...],
    ) -> OracleReview:
        """Return a valid approval for the supplied snapshot."""
        return approve_review(pull_request)


def install_orchestration_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace external review transports with deterministic fakes."""
    monkeypatch.setattr(review_module, "GitHubClient", FakeGitHubClient)
    monkeypatch.setattr(review_module, "OracleClient", FakeOracleClient)


def test_pre_post_snapshot_race_fails_before_review_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A base/head change before posting prevents the GitHub write."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, changed]
    install_orchestration_fakes(monkeypatch)
    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )
    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_post_write_race_dismisses_stale_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A base/head change after posting dismisses the stale GitHub review."""
    initial = sample_pr()
    changed = sample_pr(head_sha=SHA_C)
    FakeGitHubClient.snapshots = [initial, initial, changed]
    install_orchestration_fakes(monkeypatch)
    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )
    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == [123]


def test_execute_review_rejects_oversized_posted_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The review body plus audit footer is bounded before the GitHub write."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial]
    install_orchestration_fakes(monkeypatch)

    class OversizedOracleClient(FakeOracleClient):
        def review(
            self,
            pull_request: PullRequest,
            _attachments: tuple[Path, ...],
        ) -> OracleReview:
            """Return an approval whose body overflows the posted-body bound."""
            return replace(approve_review(pull_request), review_body="x" * 70_000)

    monkeypatch.setattr(review_module, "OracleClient", OversizedOracleClient)
    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
        )
    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


def test_execute_review_survives_post_write_artifact_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An artifact write failure after a verified post does not fail the command."""
    initial = sample_pr()
    FakeGitHubClient.snapshots = [initial, initial, initial]
    install_orchestration_fakes(monkeypatch)

    original_json = ArtifactWriter.json

    def failing_json(self: ArtifactWriter, relative: str, value: JsonObject) -> Path:
        """Fail only the post-POST audit writes to simulate a disk-full error."""
        if relative in {"github-review.json", "result.json"}:
            raise LooprError(EXIT_PRECONDITION, "artifacts", "disk full")
        return original_json(self, relative, value)

    monkeypatch.setattr(ArtifactWriter, "json", failing_json)

    result = execute_review(
        pr_value="21",
        repo_dir=tmp_path,
        artifacts_dir=Path("artifacts"),
        thinking_time="heavy",
        runner=CommandRunner({"GH_REVIEW_TOKEN": "token"}),
    )

    assert result.github_review_id == 123
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.dismissed == []


def test_cli_normalizes_unexpected_operational_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected operational failures still produce exactly one JSON object."""

    def fail_review(**_kwargs: object) -> ReviewResult:
        raise OSError("filesystem failure")

    monkeypatch.setattr(cli, "execute_review", fail_review)
    status = cli.main(["review", "--pr", "21"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == EXIT_PRECONDITION
    assert payload["error"]["category"] == "internal"
    assert captured.out.count("\n") == 1
    assert "Traceback" not in captured.err


def test_cli_normalizes_argument_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argparse failures use the same machine-readable error channel."""
    status = cli.main(["review"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == EXIT_PRECONDITION
    assert payload["error"]["category"] == "input"
    assert captured.out.count("\n") == 1


def test_help_has_no_implementation_agent_dependency() -> None:
    """The review command help does not embed a host-agent invocation."""
    help_text = cli.parser().format_help().lower()
    assert "review" in help_text
    assert "codex" not in help_text
    assert "claude" not in help_text
    assert "cursor" not in help_text
