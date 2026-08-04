from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

import pytest

import review_loop as loopr

SHA_A = "a" * 40
SHA_B = "b" * 40


def approval(sha: str = SHA_A) -> dict[str, object]:
    return {
        "schema_version": 1,
        "head_sha": sha,
        "verdict": "APPROVE",
        "review_body": "No blocking findings.",
        "implementation_prompt": "",
        "blocking_findings": [],
        "non_blocking_notes": [],
    }


def request_changes(sha: str = SHA_A) -> dict[str, object]:
    return {
        "schema_version": 1,
        "head_sha": sha,
        "verdict": "REQUEST_CHANGES",
        "review_body": "A correctness defect blocks merge.",
        "implementation_prompt": "Fix the boundary check and add a regression test.",
        "blocking_findings": [
            {
                "id": "B1",
                "title": "Boundary defect",
                "description": "The final allowed value is rejected.",
                "required_change": "Correct the comparison and cover the boundary.",
            }
        ],
        "non_blocking_notes": ["A rename could be considered later."],
    }


def make_pr(sha: str = SHA_A, *, author: str = "author") -> loopr.PullRequest:
    raw = {
        "url": "https://github.com/acme/project/pull/7",
        "number": 7,
        "title": "Change",
        "body": "Body",
        "author": {"login": author},
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "baseRefOid": "c" * 40,
        "headRefName": "feature",
        "headRefOid": sha,
        "headRepository": {"name": "project"},
        "headRepositoryOwner": {"login": "acme"},
        "reviewDecision": "REVIEW_REQUIRED",
        "changedFiles": 1,
        "files": [{"path": "app.py", "status": "modified"}],
        "statusCheckRollup": [],
        "reviews": [],
    }
    return loopr.PullRequest.from_json("acme/project", raw)


def args_for(repo: pathlib.Path, *, maximum: int = 5) -> argparse.Namespace:
    return argparse.Namespace(
        pr="7",
        repo_dir=str(repo),
        max_iterations=maximum,
        oracle_thinking_time="heavy",
        artifacts_dir=".pr-review-loop",
        dry_run=False,
    )


class OracleContractTests(unittest.TestCase):
    def test_valid_approval_fixture(self) -> None:
        parsed = loopr.parse_oracle_review(json.dumps(approval()), SHA_A)
        assert parsed.verdict == "APPROVE"
        assert not parsed.blocking_findings

    def test_valid_request_changes_fixture(self) -> None:
        parsed = loopr.parse_oracle_review(json.dumps(request_changes()), SHA_A)
        assert parsed.verdict == "REQUEST_CHANGES"
        assert parsed.implementation_prompt

    def test_one_outer_json_fence_is_tolerated(self) -> None:
        raw = "```json\n" + json.dumps(approval()) + "\n```"
        assert loopr.parse_oracle_review(raw, SHA_A).verdict == "APPROVE"

    def test_malformed_and_trailing_output_fail(self) -> None:
        fixtures = ["not json", json.dumps(approval()) + " trailing", "{}\n{}"]
        for fixture in fixtures:
            with (
                self.subTest(fixture=fixture),
                pytest.raises(loopr.LoopError) as caught,
            ):
                loopr.parse_oracle_review(fixture, SHA_A)
            assert caught.value.code == loopr.EXIT_ORACLE

    def test_stale_sha_fails(self) -> None:
        with pytest.raises(loopr.LoopError) as caught:
            loopr.parse_oracle_review(json.dumps(approval(SHA_B)), SHA_A)
        assert caught.value.code == loopr.EXIT_ORACLE

    def test_verdict_invariants_are_enforced(self) -> None:
        bad = approval()
        bad["implementation_prompt"] = "make changes"
        with pytest.raises(loopr.LoopError):
            loopr.parse_oracle_review(json.dumps(bad), SHA_A)
        bad = request_changes()
        bad["blocking_findings"] = []
        with pytest.raises(loopr.LoopError):
            loopr.parse_oracle_review(json.dumps(bad), SHA_A)


class InputAndIsolationTests(unittest.TestCase):
    def test_pr_resolution_is_canonical_and_unambiguous(self) -> None:
        assert loopr.resolve_pr_target("7", "acme/project") == (
            "acme/project",
            7,
            "https://github.com/acme/project/pull/7",
        )
        assert loopr.resolve_pr_target(
            "https://github.com/acme/project/pull/8", "elsewhere/repo"
        ) == ("acme/project", 8, "https://github.com/acme/project/pull/8")
        for invalid in (
            "0",
            "https://evil.example/acme/project/pull/7",
            "https://github.com/a/b/pull/7?x=1",
        ):
            with self.subTest(invalid=invalid), pytest.raises(loopr.LoopError):
                loopr.resolve_pr_target(invalid, "acme/project")

    def test_pr_url_comparison_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
            instance = loopr.ReviewLoop(args_for(root), runner)
            instance.repo = "acme/project"
            instance.number = 7
            instance.pr_url = "https://github.com/acme/project/pull/7"
            differently_cased = loopr.PullRequest.from_json(
                "acme/project",
                make_pr().raw | {"url": "https://github.com/Acme/Project/pull/7"},
            )
            instance._validate_snapshot(differently_cased)
            mismatched = loopr.PullRequest.from_json(
                "acme/project",
                make_pr().raw | {"url": "https://github.com/other/project/pull/7"},
            )
            with pytest.raises(loopr.LoopError):
                instance._validate_snapshot(mismatched)

    def test_remote_normalization_rejects_non_github_hosts(self) -> None:
        assert (
            loopr.normalize_github_repo("git@github.com:acme/project.git")
            == "acme/project"
        )
        assert (
            loopr.normalize_github_repo("https://github.com/acme/project.git")
            == "acme/project"
        )
        with pytest.raises(loopr.LoopError):
            loopr.normalize_github_repo("https://github.example/acme/project.git")

    def test_non_linux_platform_fails_before_bootstrap_or_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = loopr.ReviewLoop(
                args_for(pathlib.Path(temporary)),
                loopr.CommandRunner({"PATH": os.environ["PATH"]}),
            )
            with (
                mock.patch.object(loopr.sys, "platform", "darwin"),
                mock.patch.object(instance, "_bootstrap") as bootstrap,
                pytest.raises(loopr.LoopError) as caught,
            ):
                instance.execute()
            assert caught.value.code == loopr.EXIT_PRECONDITION
            bootstrap.assert_not_called()

    def test_model_environment_drops_credentials_and_ssh_agent(self) -> None:
        source = {
            "PATH": "/bin",
            "HOME": "/home/test",
            "GH_REVIEW_TOKEN": "review-secret",
            "GH_TOKEN": "gh-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_ACCESS_KEY_ID": "aws-secret",
            "NPM_TOKEN": "npm-secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "SAFE_SETTING": "not-allowlisted",
        }
        env = loopr.CommandRunner(source).model_env()
        for key in source.keys() - {"PATH", "HOME"}:
            assert key not in env

    def test_reviewer_token_is_scoped_and_redacted(self) -> None:
        runner = loopr.CommandRunner({
            "PATH": "/bin",
            "GH_REVIEW_TOKEN": "review-secret",
        })
        assert "GH_REVIEW_TOKEN" not in runner.base_env()
        review_env = runner.reviewer_env("review-secret")
        assert review_env["GH_TOKEN"] == "review-secret"
        assert runner.redact("failure review-secret") == "failure [REDACTED]"

    def test_gh_env_never_allowlists_an_enterprise_host_or_token(self) -> None:
        runner = loopr.CommandRunner({
            "PATH": "/bin",
            "GH_HOST": "github.example.com",
            "GH_ENTERPRISE_TOKEN": "enterprise-secret",
        })
        assert "GH_HOST" not in runner.gh_env()
        assert "GH_ENTERPRISE_TOKEN" not in runner.gh_env()
        assert "GH_HOST" not in runner.reviewer_env("review-secret")
        assert "GH_ENTERPRISE_TOKEN" not in runner.reviewer_env("review-secret")

    def test_same_pr_lock_rejects_second_process_and_releases(self) -> None:
        first = loopr.PrLock("acme/project", 7)
        second = loopr.PrLock("acme/project", 7)
        with first:
            with pytest.raises(loopr.LoopError) as caught:
                second.__enter__()
            assert caught.value.code == loopr.EXIT_PRECONDITION
        with second:
            assert second.path.exists()
        assert not second.path.exists()

    def test_arbiter_contention_fails_closed_within_bounded_timeout(self) -> None:
        if os.name != "posix":
            self.skipTest("arbiter contention is exercised via fcntl.flock on POSIX")
        import fcntl

        holder = loopr.PrLock("acme/project", 4343)
        holder.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        arbiter_path = holder.directory / f"{holder.digest}.arbiter"
        holder_fd = os.open(arbiter_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.write(holder_fd, b"\0")
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            with (
                mock.patch.object(loopr, "LOCK_ARBITER_TIMEOUT", 0.3),
                mock.patch.object(loopr, "LOCK_ARBITER_INTERVAL", 0.05),
            ):
                contender = loopr.PrLock("acme/project", 4343)
                start = time.monotonic()
                with pytest.raises(loopr.LoopError) as caught:
                    contender.__enter__()
                elapsed = time.monotonic() - start
            assert caught.value.code == loopr.EXIT_PRECONDITION
            assert elapsed < 2.0
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_concurrent_stale_lock_recovery_yields_exactly_one_holder(self) -> None:
        contenders = 6
        stale_pid = 2**30
        seed = loopr.PrLock("acme/project", 4242)
        seed.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        seed.path.write_text(f"{stale_pid}\n", encoding="ascii")
        locks = [loopr.PrLock("acme/project", 4242) for _ in range(contenders)]
        barrier = threading.Barrier(contenders)
        results: list[object] = [None] * contenders

        def attempt(index: int) -> None:
            barrier.wait()
            try:
                locks[index].__enter__()
                results[index] = "ok"
            except loopr.LoopError as exc:
                results[index] = exc

        threads = [
            threading.Thread(target=attempt, args=(index,))
            for index in range(contenders)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [index for index, r in enumerate(results) if r == "ok"]
        assert len(successes) == 1
        for index, result in enumerate(results):
            if index not in successes:
                assert isinstance(result, loopr.LoopError)
        winner = locks[successes[0]]
        recorded_lines = winner.path.read_text(encoding="ascii").splitlines()
        assert str(os.getpid()) == recorded_lines[0].strip()
        winner.__exit__(None, None, None)
        assert not winner.path.exists()

    def test_stale_lock_with_pid_reused_by_a_different_process_is_reclaimed(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("/proc/<pid>/stat identity checks are Linux-only")
        # A crashed loop's PID can be reused by an unrelated live process
        # before the next run starts. Recovery must not treat that live PID
        # as the still-running holder just because os.kill(pid, 0) succeeds;
        # the recorded start time no longer matching is the tell.
        lock = loopr.PrLock("acme/project", 5150)
        lock.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        real_start_time = loopr.PrLock._own_start_time()
        assert real_start_time is not None
        mismatched_start_time = str(int(real_start_time) + 1)
        lock.path.write_text(
            f"{os.getpid()}\n{mismatched_start_time}\n", encoding="ascii"
        )
        with lock:
            assert lock.path.exists()
        assert not lock.path.exists()

    def test_pid_alive_treats_an_unreaped_zombie_as_not_the_lock_holder(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("/proc/<pid>/stat identity checks are Linux-only")
        # An unreaped zombie keeps os.kill(pid, 0) succeeding after the loop
        # it once was has finished all its work; its process state must be
        # enough to recognize it as no longer a valid lock holder.
        child = os.fork()
        if child == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 5
            fields = None
            while time.monotonic() < deadline:
                fields = loopr.PrLock._proc_stat_fields(child)
                if fields is not None and fields[0] == "Z":
                    break
                time.sleep(0.02)
            assert fields is not None and fields[0] == "Z", "child never zombified"
            assert not loopr.PrLock._pid_alive(child, fields[19])
        finally:
            os.waitpid(child, 0)

    def test_lock_release_closes_the_descriptor_before_unlinking(self) -> None:
        # Assert the fd is already closed (via EBADF) by the time unlink()
        # runs, without patching the global os.close.
        lock = loopr.PrLock("acme/project", 9)
        lock.__enter__()
        fd = lock.fd
        assert fd is not None
        original_unlink = pathlib.Path.unlink
        unlink_called = False

        def recording_unlink(target: pathlib.Path, *args: Any, **kwargs: Any) -> None:
            nonlocal unlink_called
            unlink_called = True
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
            original_unlink(target, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "unlink", recording_unlink):
            lock.__exit__(None, None, None)
        assert unlink_called
        assert not lock.path.exists()

    def test_command_wrapper_can_truncate_bounded_stdout_without_raising(self) -> None:
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run(
                ["python3", "-c", "print('x' * 10000)"],
                cwd=pathlib.Path(temporary),
                env=runner.base_env(),
                max_output_bytes=128,
                check=False,
                allow_stdout_truncation=True,
            )
            assert len(result.stdout) == 128
            with pytest.raises(loopr.CommandError) as bounded:
                runner.run(
                    [
                        "python3",
                        "-c",
                        "import sys; sys.stderr.write('e' * 10000)",
                    ],
                    cwd=pathlib.Path(temporary),
                    env=runner.base_env(),
                    max_output_bytes=128,
                    check=False,
                    allow_stdout_truncation=True,
                )
            assert "exceeded" in str(bounded.value)

    def test_command_wrapper_bounds_and_redacts_diagnostics(self) -> None:
        runner = loopr.CommandRunner({
            "PATH": os.environ["PATH"],
            "TEST_TOKEN": "hidden-value",
        })
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run(
                ["python3", "-c", "print('ok')"],
                cwd=pathlib.Path(temporary),
                env=runner.base_env(),
            )
            assert result.stdout == "ok\n"
            with pytest.raises(loopr.CommandError) as caught:
                runner.run(
                    [
                        "python3",
                        "-c",
                        "import sys; print('hidden-value', file=sys.stderr); sys.exit(1)",
                    ],
                    cwd=pathlib.Path(temporary),
                    env=runner.base_env(),
                )
            assert "hidden-value" not in str(caught.value)
            with pytest.raises(loopr.CommandError) as bounded:
                runner.run(
                    ["python3", "-c", "print('x' * 10000)"],
                    cwd=pathlib.Path(temporary),
                    env=runner.base_env(),
                    max_output_bytes=128,
                )
            assert "exceeded 128 bytes" in str(bounded.value)

    def test_command_wrapper_kills_a_detached_grandchild_after_success(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group reaping is exercised via os.killpg on POSIX")
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            marker = root / "grandchild-survived"
            grandchild_script = root / "grandchild.py"
            grandchild_script.write_text(
                "import sys, time\ntime.sleep(1)\nopen(sys.argv[1], 'w').close()\n",
                encoding="utf-8",
            )
            leader_script = root / "leader.py"
            leader_script.write_text(
                "import subprocess, sys\n"
                "subprocess.Popen(\n"
                "    [sys.executable, sys.argv[1], sys.argv[2]],\n"
                "    stdout=subprocess.DEVNULL,\n"
                "    stderr=subprocess.DEVNULL,\n"
                "    stdin=subprocess.DEVNULL,\n"
                ")\n",
                encoding="utf-8",
            )
            result = runner.run(
                ["python3", str(leader_script), str(grandchild_script), str(marker)],
                cwd=root,
                env=runner.base_env(),
            )
            assert result.returncode == 0
            assert not marker.exists()
            time.sleep(1.5)
            assert not marker.exists()

    def test_command_wrapper_kills_a_grandchild_that_leaves_the_process_group(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("process-tree containment is exercised via /proc on POSIX")
        if not pathlib.Path("/proc").is_dir():
            self.skipTest("descendant verification requires /proc (Linux only)")
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            marker = root / "grandchild-survived"
            grandchild_script = root / "grandchild.py"
            grandchild_script.write_text(
                "import sys, time\ntime.sleep(1)\nopen(sys.argv[1], 'w').close()\n",
                encoding="utf-8",
            )
            leader_script = root / "leader.py"
            leader_script.write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen(\n"
                "    [sys.executable, sys.argv[1], sys.argv[2]],\n"
                "    stdout=subprocess.DEVNULL,\n"
                "    stderr=subprocess.DEVNULL,\n"
                "    stdin=subprocess.DEVNULL,\n"
                "    start_new_session=True,\n"
                ")\n"
                "time.sleep(0.3)\n",
                encoding="utf-8",
            )
            result = runner.run(
                ["python3", str(leader_script), str(grandchild_script), str(marker)],
                cwd=root,
                env=runner.base_env(),
            )
            assert result.returncode == 0
            assert not marker.exists()
            time.sleep(1.5)
            assert not marker.exists()

    def test_command_wrapper_kills_a_fast_double_forked_detached_grandchild(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("kernel subreaper containment is Linux-only")
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            marker = root / "grandchild-survived"
            leader_script = root / "leader.py"
            leader_script.write_text(
                "import os, sys, time\n"
                "first = os.fork()\n"
                "if first:\n"
                "    os._exit(0)\n"
                "os.setsid()\n"
                "second = os.fork()\n"
                "if second:\n"
                "    os._exit(0)\n"
                "time.sleep(1)\n"
                "open(sys.argv[1], 'w').close()\n",
                encoding="utf-8",
            )
            result = runner.run(
                [sys.executable, str(leader_script), str(marker)],
                cwd=root,
                env=runner.base_env(),
                timeout=10,
            )
            assert result.returncode == 0
            assert not marker.exists()
            time.sleep(1.5)
            assert not marker.exists()

    def test_linux_supervisor_fails_before_exec_when_subreaper_setup_fails(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("kernel subreaper containment is Linux-only")
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            marker = root / "payload-ran"
            payload = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')"
            with (
                mock.patch.object(
                    loopr,
                    "_linux_enable_subreaper",
                    side_effect=OSError("forced subreaper failure"),
                ),
                pytest.raises(loopr.CommandError),
            ):
                runner.run(
                    [sys.executable, "-c", payload, str(marker)],
                    cwd=root,
                    env=runner.base_env(),
                )
            assert not marker.exists()

    def test_linux_supervisor_fails_before_exec_when_pidfds_are_unavailable(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("stable pidfd containment is Linux-only")
        runner = loopr.CommandRunner({"PATH": os.environ["PATH"]})
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            marker = root / "payload-ran"
            payload = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')"
            with (
                mock.patch.object(
                    loopr,
                    "_linux_pidfd_open",
                    side_effect=OSError("forced pidfd failure"),
                ),
                pytest.raises(loopr.CommandError),
            ):
                runner.run(
                    [sys.executable, "-c", payload, str(marker)],
                    cwd=root,
                    env=runner.base_env(),
                )
            assert not marker.exists()

    def test_linux_kill_uses_a_stable_pidfd_handle(self) -> None:
        with (
            mock.patch.object(
                loopr.os, "pidfd_open", create=True, return_value=41
            ) as pidfd_open,
            mock.patch.object(
                loopr.signal, "pidfd_send_signal", create=True
            ) as send_signal,
            mock.patch.object(loopr.os, "close") as close,
        ):
            assert loopr._linux_kill_pid(1234)
        pidfd_open.assert_called_once_with(1234, 0)
        send_signal.assert_called_once_with(41, loopr.signal.SIGKILL, None, 0)
        close.assert_called_once_with(41)

    def test_linux_status_payload_is_hard_bounded(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            loopr._linux_send_status(
                write_fd,
                {"type": "error", "message": "x" * (loopr.LINUX_STATUS_MAX_BYTES * 4)},
            )
            os.close(write_fd)
            write_fd = -1
            raw = os.read(read_fd, loopr.LINUX_STATUS_MAX_BYTES + 1)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)
        assert len(raw) <= loopr.LINUX_STATUS_MAX_BYTES
        parsed = json.loads(raw)
        assert parsed["type"] == "error"
        assert (
            len(parsed["message"].encode("utf-8")) <= loopr.LINUX_STATUS_ERROR_BYTES + 3
        )

    def test_run_codex_receives_the_sanitized_environment(self) -> None:
        source = {
            "PATH": os.environ["PATH"],
            "HOME": tempfile.gettempdir(),
            "GH_REVIEW_TOKEN": "review-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "NPM_TOKEN": "registry-secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
        }
        runner = loopr.CommandRunner(source)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            worktree = root / "worktree"
            artifacts = root / "artifacts"
            worktree.mkdir()
            artifacts.mkdir()
            instance = loopr.ReviewLoop(args_for(root), runner)
            instance.repo_dir = root
            instance.artifacts_dir = artifacts
            instance.writer = loopr.ArtifactWriter(artifacts, runner)
            captured: dict[str, str] = {}

            def fake_command(
                command: list[str], **kwargs: object
            ) -> loopr.CommandResult:
                environment = kwargs["env"]
                assert isinstance(environment, dict)
                captured.update({
                    str(key): str(value) for key, value in environment.items()
                })
                final = pathlib.Path(
                    command[command.index("--output-last-message") + 1]
                )
                final.write_text("Done.\n", encoding="utf-8")
                return loopr.CommandResult(tuple(command), 0, '{"type":"done"}\n', "")

            review = loopr.parse_oracle_review(json.dumps(request_changes()), SHA_A)
            with (
                mock.patch.object(instance, "_outside_state", return_value={}),
                mock.patch.object(instance, "command", side_effect=fake_command),
            ):
                instance.run_codex(review, worktree, artifacts)
            for key in (
                "GH_REVIEW_TOKEN",
                "GITHUB_TOKEN",
                "AWS_SECRET_ACCESS_KEY",
                "NPM_TOKEN",
                "SSH_AUTH_SOCK",
            ):
                assert key not in captured


class ScriptedLoop(loopr.ReviewLoop):
    def __init__(
        self,
        root: pathlib.Path,
        reviews: list[dict[str, object]],
        *,
        maximum: int = 5,
        no_op: bool = False,
        race: bool = False,
        author: str = "author",
    ) -> None:
        runner = loopr.CommandRunner({
            "PATH": os.environ["PATH"],
            "GH_REVIEW_TOKEN": "review-token",
        })
        super().__init__(args_for(root, maximum=maximum), runner)
        self.scripted_reviews = reviews
        self.review_index = 0
        self.current_sha = str(reviews[0]["head_sha"])
        self.no_op = no_op
        self.race = race
        self.author = author
        self.calls: list[str] = []

    def _bootstrap(self) -> None:
        self.repo_dir = pathlib.Path(self.args.repo_dir)
        self.repo = "acme/project"
        self.number = 7
        self.pr_url = "https://github.com/acme/project/pull/7"
        self.artifacts_dir = self.repo_dir / ".pr-review-loop"
        self.writer = loopr.ArtifactWriter(self.artifacts_dir, self.runner)

    def precheck(self) -> loopr.PullRequest:
        self.calls.append("precheck")
        if self.author == "reviewer":
            raise loopr.LoopError(loopr.EXIT_PRECONDITION, "self-review is forbidden")
        self.versions = {
            "python": "test",
            "node": "v24",
            "git": "test",
            "gh": "test",
            "oracle": "test",
            "codex": "test",
            "chrome": "test",
        }
        return make_pr(self.current_sha, author=self.author)

    def snapshot(self, *, reviewer: bool = False) -> loopr.PullRequest:
        return make_pr(self.current_sha, author=self.author)

    def prepare_worktree(self, pr: loopr.PullRequest) -> pathlib.Path:
        self.calls.append("prepare")
        path = self.artifacts_dir / "worktrees" / "pr-7"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def collect_bundle(
        self, pr: loopr.PullRequest, worktree: pathlib.Path, iteration_dir: pathlib.Path
    ) -> loopr.ReviewBundle:
        self.calls.append("collect")
        assert self.writer
        paths = []
        for name, content in {
            "pr.json": json.dumps(pr.raw),
            "context.md": pr.head_sha,
            "diff.patch": "diff",
            "changed-files.txt": "modified\tapp.py\n",
            "attachments.json": "[]\n",
        }.items():
            path = iteration_dir / name
            self.writer.text(path, content)
            paths.append(path)
        return loopr.ReviewBundle(iteration_dir, tuple(paths))

    def oracle_review(
        self, pr: loopr.PullRequest, bundle: loopr.ReviewBundle
    ) -> loopr.OracleReview:
        self.calls.append("oracle")
        assert self.writer
        value = self.scripted_reviews[self.review_index]
        self.review_index += 1
        raw = json.dumps(value)
        self.writer.text(bundle.iteration_dir / "oracle-raw.md", raw)
        parsed = loopr.parse_oracle_review(raw, pr.head_sha)
        self.writer.json(bundle.iteration_dir / "oracle.json", parsed.raw)
        return parsed

    def post_review(
        self,
        pr: loopr.PullRequest,
        review: loopr.OracleReview,
        iteration: int,
        iteration_dir: pathlib.Path,
    ) -> int:
        self.calls.append(f"post:{review.verdict}")
        assert self.writer
        self.writer.text(
            iteration_dir / "review.md",
            review.review_body + f"\n{pr.head_sha}\nIteration: {iteration}\n",
        )
        return 1

    def verify_approval(
        self, expected_head_sha: str, expected_base_sha: str, review_id: int
    ) -> None:
        self.calls.append("verify")

    def run_codex(
        self,
        review: loopr.OracleReview,
        worktree: pathlib.Path,
        iteration_dir: pathlib.Path,
    ) -> tuple[dict[str, str], set[str]]:
        self.calls.append("codex")
        assert self.writer
        self.writer.text(
            iteration_dir / "codex-prompt.md",
            loopr.CODEX_GUARDRAILS.format(
                implementation_prompt=review.implementation_prompt
            ),
        )
        self.writer.text(iteration_dir / "codex-events.jsonl", '{"type":"done"}\n')
        self.writer.text(
            iteration_dir / "codex-final.md", "Changed app.py; tests pass.\n"
        )
        return {}, set()

    def validate_commit_push(
        self,
        pr: loopr.PullRequest,
        worktree: pathlib.Path,
        iteration: int,
        iteration_dir: pathlib.Path,
        outside_before: dict[str, str],
        nested_before: set[str],
    ) -> str:
        self.calls.append("push")
        if self.no_op:
            raise loopr.LoopError(
                loopr.EXIT_STALLED, "Codex produced no implementation changes"
            )
        if self.race:
            raise loopr.LoopError(loopr.EXIT_RACE, "remote head changed")
        assert self.writer
        pushed = SHA_B if pr.head_sha == SHA_A else "d" * 40
        self.writer.text(iteration_dir / "resulting.patch", "binary patch\n")
        self.writer.text(iteration_dir / "pushed-commit.txt", pushed + "\n")
        return pushed

    def wait_for_github_head(self, expected_sha: str) -> None:
        self.current_sha = expected_sha


class AcceptanceStateMachineTests(unittest.TestCase):
    def run_script(
        self,
        reviews: list[dict[str, object]],
        *,
        maximum: int = 5,
        no_op: bool = False,
        race: bool = False,
        author: str = "author",
    ) -> tuple[ScriptedLoop, int, pathlib.Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        scripted = ScriptedLoop(
            root,
            reviews,
            maximum=maximum,
            no_op=no_op,
            race=race,
            author=author,
        )
        try:
            with mock.patch.object(loopr.sys, "platform", "linux"):
                code = scripted.execute()
        except loopr.LoopError as exc:
            code = exc.code
        return scripted, code, root

    def test_request_changes_codex_edit_push_then_approval(self) -> None:
        scripted, code, _ = self.run_script([request_changes(SHA_A), approval(SHA_B)])
        assert code == loopr.EXIT_OK
        assert scripted.calls.count("codex") == 1
        assert scripted.calls.count("push") == 1
        assert "post:REQUEST_CHANGES" in scripted.calls
        assert "post:APPROVE" in scripted.calls

    def test_approval_exits_zero_without_codex(self) -> None:
        scripted, code, _ = self.run_script([approval()])
        assert code == loopr.EXIT_OK
        assert "codex" not in scripted.calls
        assert scripted.calls.count("post:APPROVE") == 1

    def test_dry_run_validates_without_artifacts_models_or_writes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        scripted = ScriptedLoop(root, [approval()])
        scripted.args.dry_run = True
        with mock.patch.object(loopr.sys, "platform", "linux"):
            assert scripted.execute() == loopr.EXIT_OK
        assert scripted.calls == ["precheck"]
        assert not (root / ".pr-review-loop").exists()

    def test_no_op_codex_is_stalled_without_push(self) -> None:
        scripted, code, _ = self.run_script([request_changes()], no_op=True)
        assert code == loopr.EXIT_STALLED
        assert scripted.calls.count("push") == 1

    def test_concurrent_remote_update_aborts(self) -> None:
        _scripted, code, _ = self.run_script([request_changes()], race=True)
        assert code == loopr.EXIT_RACE

    def test_malformed_oracle_output_causes_no_write_or_edit(self) -> None:
        malformed = approval()
        del malformed["schema_version"]
        scripted, code, _ = self.run_script([malformed])
        assert code == loopr.EXIT_ORACLE
        assert not any(call.startswith("post:") for call in scripted.calls)
        assert "codex" not in scripted.calls
        assert "push" not in scripted.calls

    def test_maximum_iteration_is_exact_and_does_not_leave_unreviewed_patch(
        self,
    ) -> None:
        scripted, code, _ = self.run_script([request_changes()], maximum=1)
        assert code == loopr.EXIT_STALLED
        assert scripted.calls.count("oracle") == 1
        assert "codex" not in scripted.calls
        assert "push" not in scripted.calls

    def test_complete_approval_artifacts_exist(self) -> None:
        scripted, code, _ = self.run_script([approval()])
        assert code == loopr.EXIT_OK
        run = scripted.run_dir
        assert run is not None
        iteration = run / "iteration-01"
        expected = {
            "pr.json",
            "context.md",
            "diff.patch",
            "changed-files.txt",
            "attachments.json",
            "oracle-raw.md",
            "oracle.json",
            "review.md",
            "codex-prompt.md",
            "codex-events.jsonl",
            "codex-final.md",
            "resulting.patch",
            "pushed-commit.txt",
            "versions.json",
        }
        assert expected.issubset({path.name for path in iteration.iterdir()})
        assert (run / "state.json").is_file()
        assert (run / "final.json").is_file()
        for path in run.rglob("*"):
            if path.is_file():
                assert "review-token" not in path.read_text(encoding="utf-8")

    def test_self_review_rejected_before_posting(self) -> None:
        scripted, code, root = self.run_script([approval()], author="reviewer")
        assert code == loopr.EXIT_PRECONDITION
        assert not any(call.startswith("post:") for call in scripted.calls)
        assert not (root / ".pr-review-loop").exists()


class PatchSafetyTests(unittest.TestCase):
    def git(self, cwd: pathlib.Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
        ).stdout.strip()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.primary = root / "primary"
        self.git(root, "init", "--bare", str(self.remote))
        self.git(root, "clone", str(self.remote), str(self.primary))
        self.git(self.primary, "config", "user.name", "Loop Test")
        self.git(self.primary, "config", "user.email", "loop@example.test")
        (self.primary / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git(self.primary, "add", "app.py")
        self.git(self.primary, "commit", "-m", "initial")
        self.git(self.primary, "branch", "-M", "main")
        self.git(self.primary, "push", "-u", "origin", "main")
        self.git(self.primary, "checkout", "-b", "feature")
        (self.primary / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.git(self.primary, "commit", "-am", "feature")
        self.git(self.primary, "push", "-u", "origin", "feature")
        self.sha = self.git(self.primary, "rev-parse", "HEAD")
        base = self.git(self.primary, "rev-parse", "main")
        raw = make_pr(self.sha).raw | {"baseRefOid": base, "headRefOid": self.sha}
        self.pr = loopr.PullRequest.from_json("acme/project", raw)
        runner = loopr.CommandRunner({
            "PATH": os.environ["PATH"],
            "GH_REVIEW_TOKEN": "review-token",
        })
        self.loop = loopr.ReviewLoop(args_for(self.primary), runner)
        self.loop.repo_dir = self.primary
        self.loop.repo = "acme/project"
        self.loop.number = 7
        self.loop.pr_url = "https://github.com/acme/project/pull/7"
        self.loop.origin_url = str(self.remote)
        self.loop.push_url = str(self.remote)
        self.loop.artifacts_dir = self.primary / ".pr-review-loop"
        self.loop.artifacts_dir.mkdir()
        self.loop.writer = loopr.ArtifactWriter(self.loop.artifacts_dir, runner)
        self.loop._author_identity = ("Loop Test", "loop@example.test")
        self.loop._initialize_control_repository()
        self.worktree = self.loop.prepare_worktree(self.pr)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_precheck_author_identity_probe_fails_closed_without_configured_identity(
        self,
    ) -> None:
        unset = pathlib.Path(self.temporary.name) / "no-identity"
        unset.mkdir()
        self.git(unset, "init", "-q")
        homeless = pathlib.Path(self.temporary.name) / "empty-home"
        homeless.mkdir()
        runner = loopr.CommandRunner({
            "PATH": os.environ["PATH"],
            "HOME": str(homeless),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.useConfigOnly",
            "GIT_CONFIG_VALUE_0": "true",
        })
        instance = loopr.ReviewLoop(args_for(unset), runner)
        instance.repo_dir = unset
        with pytest.raises(loopr.CommandError):
            instance.command(["git", "var", "GIT_AUTHOR_IDENT"], cwd=unset)
        self.git(unset, "config", "user.name", "Loop Test")
        self.git(unset, "config", "user.email", "loop@example.test")
        instance.command(["git", "var", "GIT_AUTHOR_IDENT"], cwd=unset)

    def test_pushability_precheck_passes_for_a_reachable_writable_head(self) -> None:
        self.loop._check_pushable(self.pr)

    def test_pushability_precheck_detects_a_diverged_remote_head(self) -> None:
        other = pathlib.Path(self.temporary.name) / "other"
        self.git(
            pathlib.Path(self.temporary.name), "clone", str(self.remote), str(other)
        )
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.test")
        self.git(other, "checkout", "feature")
        (other / "other.txt").write_text("race\n", encoding="utf-8")
        self.git(other, "add", "other.txt")
        self.git(other, "commit", "-m", "race")
        self.git(other, "push", "origin", "feature")
        with pytest.raises(loopr.LoopError) as caught:
            self.loop._check_pushable(self.pr)
        assert caught.value.code == loopr.EXIT_PRECONDITION

    def test_pushability_precheck_cannot_predict_a_branch_policy_rejection(
        self,
    ) -> None:
        # Documents a known limitation: --dry-run never reaches the
        # server's hook/branch-protection phase, even for a push that
        # would land a genuinely new commit, so this precheck cannot
        # substitute for handling a policy rejection at the real push.
        hooks = self.remote / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            '  if [ "$old" != "$new" ]; then\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        self.loop._check_pushable(self.pr)
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "policy-rejection"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_patch_validation_commits_with_hooks_disabled_and_pushes_exact_ref(
        self,
    ) -> None:
        git_dir = pathlib.Path(self.git(self.primary, "rev-parse", "--git-common-dir"))
        if not git_dir.is_absolute():
            git_dir = self.primary / git_dir
        hooks = git_dir / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "iteration"
        iteration.mkdir()
        pushed = self.loop.validate_commit_push(
            self.pr,
            self.worktree,
            1,
            iteration,
            self.loop._outside_state(self.worktree),
            self.loop._nested_git_entries(self.worktree),
        )
        remote_sha = self.git(
            self.primary, "ls-remote", "origin", "refs/heads/feature"
        ).split()[0]
        assert pushed == remote_sha
        assert (iteration / "resulting.patch").read_text().strip()

    def test_push_does_not_run_a_relative_receive_pack_in_the_worktree(self) -> None:
        # A receive-pack configured before the worktree controls are captured is
        # trusted configuration, but a relative program name must not resolve
        # against Codex-controlled disposable-worktree content during the push.
        self.git(self.primary, "config", "remote.origin.receivepack", "./receive-pack")
        worktree = self.loop.prepare_worktree(self.pr)
        marker = pathlib.Path(self.temporary.name) / "receive-pack-fired"
        helper = worktree / "receive-pack"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        helper.chmod(0o755)
        (worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "relative-receive-pack"
        iteration.mkdir()
        pushed = self.loop.validate_commit_push(
            self.pr,
            worktree,
            1,
            iteration,
            self.loop._outside_state(worktree),
            self.loop._nested_git_entries(worktree),
        )
        assert pushed
        assert not marker.exists()

    def test_authenticated_push_does_not_use_primary_checkout_helpers(self) -> None:
        marker = pathlib.Path(self.temporary.name) / "primary-receive-pack-fired"
        helper = self.primary / "receive-pack"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        helper.chmod(0o755)
        self.git(
            self.primary,
            "config",
            "remote.origin.receivepack",
            "./receive-pack",
        )
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "primary-relative-receive-pack"
        iteration.mkdir()
        pushed = self.loop.validate_commit_push(
            self.pr,
            self.worktree,
            1,
            iteration,
            self.loop._outside_state(self.worktree),
            self.loop._nested_git_entries(self.worktree),
        )
        assert pushed
        assert not marker.exists()
        common = pathlib.Path(self.git(self.primary, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.primary / common
        assert pathlib.Path(self.loop._control_repo or "").resolve() != common.resolve()

    def test_git_wrapper_ignores_a_tracked_core_fsmonitor_hook(self) -> None:
        # core.hooksPath does not cover core.fsmonitor: it is a distinct
        # hook command that git/status invokes on its own, and it can be
        # pointed at a path the disposable worktree controls.
        marker = pathlib.Path(self.temporary.name) / "fsmonitor-fired"
        hook = self.worktree / "fsmonitor-hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\necho /\n", encoding="utf-8")
        hook.chmod(0o755)
        self.git(self.worktree, "config", "core.fsmonitor", str(hook))
        self.loop.command(["git", "status"], cwd=self.worktree)
        assert not marker.exists()

    def test_git_wrapper_ignores_a_tracked_gitattributes_smudge_filter(self) -> None:
        # A filter.<name>.smudge driver can be configured on the repository
        # (or global/system config) for legitimate local use; a PR-controlled
        # .gitattributes cannot define a new driver, but it can activate this
        # one on a checkout path via a filter= attribute.
        marker = pathlib.Path(self.temporary.name) / "smudge-fired"
        self.git(
            self.primary, "config", "filter.marker.smudge", f"touch '{marker}'; cat"
        )
        (self.worktree / ".gitattributes").write_text(
            "app.py filter=marker\n", encoding="utf-8"
        )
        self.git(self.worktree, "add", ".gitattributes")
        self.git(self.worktree, "commit", "-m", "attributes")
        (self.worktree / "app.py").unlink()
        # The unwrapped setup calls above can themselves trip the smudge
        # filter (git's racy-git re-verification re-cleans/smudges any
        # attributed path whose mtime is not distinguishable from the index
        # timestamp); reset the marker so the assertion checks only the
        # wrapped call under test.
        marker.unlink(missing_ok=True)
        self.loop.command(["git", "checkout", "--", "app.py"], cwd=self.worktree)
        assert not marker.exists()
        assert (self.worktree / "app.py").read_text() == "VALUE = 2\n"

    def test_git_wrapper_ignores_a_tracked_gitattributes_clean_filter(self) -> None:
        # Same escape as the smudge case, but on the staging side: a
        # filter.<name>.clean driver runs when a filter=-attributed path is
        # added to the index.
        marker = pathlib.Path(self.temporary.name) / "clean-fired"
        self.git(
            self.primary, "config", "filter.marker.clean", f"touch '{marker}'; cat"
        )
        (self.worktree / ".gitattributes").write_text(
            "app.py filter=marker\n", encoding="utf-8"
        )
        self.git(self.worktree, "add", ".gitattributes")
        self.git(self.worktree, "commit", "-m", "attributes")
        (self.worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        # The unwrapped setup calls above can themselves trip the clean filter
        # (git's racy-git re-verification re-cleans any attributed path whose
        # mtime is not distinguishable from the index timestamp); reset the
        # marker so the assertion below checks only the wrapped call under test.
        marker.unlink(missing_ok=True)
        self.loop.command(["git", "add", "app.py"], cwd=self.worktree)
        assert not marker.exists()

    def test_git_wrapper_neutralizes_filters_without_resolving_worktree_cat(
        self,
    ) -> None:
        marker = pathlib.Path(self.temporary.name) / "worktree-cat-fired"
        local_cat = self.worktree / "cat"
        local_cat.write_text(
            f"#!/bin/sh\ntouch '{marker}'\nexit 77\n", encoding="utf-8"
        )
        local_cat.chmod(0o755)
        self.git(self.primary, "config", "filter.marker.clean", "cat")
        (self.worktree / ".gitattributes").write_text(
            "app.py filter=marker\n", encoding="utf-8"
        )
        self.git(self.worktree, "add", ".gitattributes")
        self.git(self.worktree, "commit", "-m", "attributes")
        (self.worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.loop.base_env["PATH"] = f".{os.pathsep}{os.environ['PATH']}"

        self.loop.command(["git", "add", "app.py"], cwd=self.worktree)

        assert not marker.exists()
        indexed = subprocess.run(
            ["git", "show", ":app.py"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        ).stdout
        assert indexed == b"VALUE = 9\n"

    def test_command_wrapper_resolves_trusted_git_despite_relative_path_entry(
        self,
    ) -> None:
        # Reproduces the P1 finding directly: PATH=.:$PATH plus a
        # same-named executable tracked in (or written to) the
        # PR-controlled worktree must never be selected in place of the
        # real "git", which is looked up by bare name after `cwd` has
        # already changed to that worktree.
        marker = pathlib.Path(self.temporary.name) / "worktree-git-fired"
        fake_git = self.worktree / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        fake_git.chmod(0o755)
        self.loop.runner._trusted_executables.pop("git", None)
        poisoned_path = f".{os.pathsep}{os.environ['PATH']}"
        self.loop.runner.source_env["PATH"] = poisoned_path
        self.loop.base_env["PATH"] = poisoned_path

        result = self.loop.command(["git", "status"], cwd=self.worktree)

        assert result.returncode == 0
        assert not marker.exists()

    def test_command_wrapper_shadow_reproduces_without_trusted_resolution(
        self,
    ) -> None:
        # Sanity check for the test above: with resolution reverted to the
        # old bare-name behavior, the identical PATH=.:$PATH plus
        # worktree-local "git" setup does let the worktree file run,
        # proving the assertions above actually exercise the fix rather
        # than an unrelated side effect.
        marker = pathlib.Path(self.temporary.name) / "worktree-git-fired-unguarded"
        fake_git = self.worktree / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        fake_git.chmod(0o755)
        # This is the env actually handed to the child at exec time (see
        # command()'s `child_env = dict(env or self.base_env)`); the
        # resolution-time PATH read by trusted_executable() is irrelevant
        # here since resolution itself is mocked out below.
        self.loop.base_env["PATH"] = f".{os.pathsep}{os.environ['PATH']}"

        with mock.patch.object(
            self.loop.runner, "trusted_executable", side_effect=lambda name: name
        ):
            result = self.loop.command(
                ["git", "status"], cwd=self.worktree, check=False
            )

        assert result.returncode == 1
        assert marker.exists()

    def test_command_wrapper_resolves_trusted_codex_despite_relative_path_entry(
        self,
    ) -> None:
        # Same escape as above, but for "codex": run_codex() invokes it by
        # bare name with `cwd` set to the worktree it is meant to be
        # sandboxed into.
        marker = pathlib.Path(self.temporary.name) / "worktree-codex-fired"
        fake_worktree_codex = self.worktree / "codex"
        fake_worktree_codex.write_text(
            f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8"
        )
        fake_worktree_codex.chmod(0o755)
        trusted_dir = pathlib.Path(self.temporary.name) / "trusted-bin"
        trusted_dir.mkdir()
        trusted_codex = trusted_dir / "codex"
        trusted_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        trusted_codex.chmod(0o755)
        self.loop.runner._trusted_executables.pop("codex", None)
        self.loop.runner.source_env["PATH"] = (
            f".{os.pathsep}{trusted_dir}{os.pathsep}{os.environ['PATH']}"
        )

        result = self.loop.command(["codex", "--version"], cwd=self.worktree)

        assert result.returncode == 0
        assert not marker.exists()

    def test_trusted_executable_ignores_relative_and_empty_path_entries(self) -> None:
        trusted_dir = pathlib.Path(self.temporary.name) / "trusted-bin-generic"
        trusted_dir.mkdir()
        trusted_tool = trusted_dir / "loopr-example-tool"
        trusted_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        trusted_tool.chmod(0o755)
        worktree_tool = self.worktree / "loopr-example-tool"
        worktree_tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        worktree_tool.chmod(0o755)
        runner = loopr.CommandRunner({
            "PATH": f".{os.pathsep}{os.pathsep}{trusted_dir}"
        })

        resolved = runner.trusted_executable("loopr-example-tool")

        assert str(trusted_tool) == resolved
        result = runner.run([resolved], cwd=self.worktree, env={"PATH": ""})
        assert result.returncode == 0

    def test_trusted_executable_fails_closed_without_an_absolute_path_entry(
        self,
    ) -> None:
        runner = loopr.CommandRunner({"PATH": f".{os.pathsep}"})
        with pytest.raises(loopr.CommandError):
            runner.trusted_executable("git")

    def test_content_filter_overrides_sort_and_deduplicate_dotted_drivers(self) -> None:
        listing = (
            "filter.z.driver.clean clean\n"
            "filter.a.smudge smudge\n"
            "filter.z.driver.process process\n"
            "filter.a.clean clean\n"
            "filter.z.driver.smudge smudge\n"
            "filter.foo=bar.clean clean\n"
            "filter.foo=bar.clean duplicate\n"
            "filter.foo=bar.process process\n"
            "filter.foo=bar.smudge smudge\n"
        )
        result = loopr.CommandResult(("git", "config"), 0, listing, "")
        with mock.patch.object(self.loop.runner, "run", return_value=result):
            overrides = self.loop._content_filter_overrides(self.worktree)
        assert overrides == [
            "-c",
            "filter.a.clean=",
            "-c",
            "filter.a.smudge=",
            "-c",
            "filter.a.process=",
            "-c",
            "filter.a.required=false",
            "--config-env",
            "filter.foo=bar.clean=LOOPR_GIT_CONFIG_EMPTY",
            "--config-env",
            "filter.foo=bar.smudge=LOOPR_GIT_CONFIG_EMPTY",
            "--config-env",
            "filter.foo=bar.process=LOOPR_GIT_CONFIG_EMPTY",
            "--config-env",
            "filter.foo=bar.required=LOOPR_GIT_CONFIG_FALSE",
            "-c",
            "filter.z.driver.clean=",
            "-c",
            "filter.z.driver.smudge=",
            "-c",
            "filter.z.driver.process=",
            "-c",
            "filter.z.driver.required=false",
        ]

    def test_git_wrapper_ignores_a_clean_filter_with_equals_in_driver_name(
        self,
    ) -> None:
        marker = pathlib.Path(self.temporary.name) / "equals-clean-fired"
        original = b"VALUE = 2\n"
        self.git(
            self.primary,
            "config",
            "filter.foo=bar.clean",
            f"touch '{marker}'; cat",
        )
        self.git(self.primary, "config", "filter.foo=bar.required", "true")
        (self.worktree / ".gitattributes").write_text(
            "app.py filter=foo=bar\n", encoding="utf-8"
        )
        self.git(self.worktree, "add", ".gitattributes")
        self.git(self.worktree, "commit", "-m", "attributes")
        (self.worktree / "app.py").write_bytes(original)
        self.git(self.worktree, "add", "app.py")
        marker.unlink(missing_ok=True)
        result = self.loop.command(["git", "add", "app.py"], cwd=self.worktree)

        assert result.returncode == 0
        assert not marker.exists()
        indexed = subprocess.run(
            ["git", "show", ":app.py"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        ).stdout
        assert original == indexed

    def test_git_wrapper_ignores_a_configured_filter_process_driver(self) -> None:
        # filter.<name>.process takes priority over clean/smudge when defined;
        # neutralizing it must not merely fail closed but must let the
        # checkout proceed (content is passed through unfiltered).
        marker = pathlib.Path(self.temporary.name) / "process-fired"
        (self.worktree / ".gitattributes").write_text(
            "app.py filter=marker\n", encoding="utf-8"
        )
        self.git(self.worktree, "add", ".gitattributes")
        self.git(self.worktree, "commit", "-m", "attributes")
        self.git(
            self.primary,
            "config",
            "filter.marker.process",
            f"touch '{marker}'; exit 1",
        )
        (self.worktree / "app.py").unlink()
        marker.unlink(missing_ok=True)
        self.loop.command(["git", "checkout", "--", "app.py"], cwd=self.worktree)
        assert not marker.exists()
        assert (self.worktree / "app.py").read_text() == "VALUE = 2\n"

    def test_git_diff_ignores_a_configured_external_diff_command(self) -> None:
        # diff.external runs for every path with a working-tree change unless
        # --no-ext-diff is passed; git tries to execute the value verbatim, so
        # (unlike filter.*.process) it cannot be neutralized by emptying it.
        marker = pathlib.Path(self.temporary.name) / "ext-diff-fired"
        self.git(self.primary, "config", "diff.external", f"sh -c \"touch '{marker}'\"")
        (self.worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.loop.command(["git", "diff"], cwd=self.worktree)
        assert not marker.exists()

    def _install_post_index_change_hook(self) -> pathlib.Path:
        git_dir = pathlib.Path(self.git(self.primary, "rev-parse", "--git-common-dir"))
        if not git_dir.is_absolute():
            git_dir = self.primary / git_dir
        hooks = git_dir / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = pathlib.Path(self.temporary.name) / "post-index-change-fired"
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        return marker

    def test_git_add_ignores_an_executable_post_index_change_hook(self) -> None:
        marker = self._install_post_index_change_hook()
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "iteration"
        iteration.mkdir()
        self.loop.validate_commit_push(
            self.pr,
            self.worktree,
            1,
            iteration,
            self.loop._outside_state(self.worktree),
            self.loop._nested_git_entries(self.worktree),
        )
        assert not marker.exists()

    def test_worktree_reset_on_reuse_ignores_an_executable_post_index_change_hook(
        self,
    ) -> None:
        marker = self._install_post_index_change_hook()
        reused = self.loop.prepare_worktree(self.pr)
        assert self.worktree == reused
        assert not marker.exists()

    def test_worktree_reuse_is_exact_and_dirty_state_fails_closed(self) -> None:
        primary_before = self.git(
            self.primary, "status", "--porcelain", "--untracked-files=all"
        )
        reused = self.loop.prepare_worktree(self.pr)
        assert self.worktree == reused
        assert self.sha == self.git(reused, "rev-parse", "HEAD")
        assert not self.git(reused, "status", "--porcelain", "--untracked-files=all")
        assert primary_before == self.git(
            self.primary, "status", "--porcelain", "--untracked-files=all"
        )
        (reused / "dirty.txt").write_text("do not carry me\n", encoding="utf-8")
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.prepare_worktree(self.pr)
        assert caught.value.code == loopr.EXIT_PRECONDITION

    def test_bundle_contains_complete_text_and_explicit_binary_entries(self) -> None:
        self.git(self.primary, "checkout", "feature")
        (self.primary / "AGENTS.md").write_text(
            "Follow focused tests.\n", encoding="utf-8"
        )
        (self.primary / "binary.dat").write_bytes(b"\x00\x01\x02")
        self.git(self.primary, "add", "AGENTS.md", "binary.dat")
        self.git(self.primary, "commit", "-m", "add context fixtures")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 3
        raw["files"] = [
            {"path": "AGENTS.md", "status": "added"},
            {"path": "app.py", "status": "modified"},
            {"path": "binary.dat", "status": "added"},
        ]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "bundle"
        iteration.mkdir()
        with (
            mock.patch.object(
                self.loop, "_gh", return_value="diff --git a/app.py b/app.py\n"
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
        ):
            bundle = self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["binary.dat"]["kind"] == "binary"
        agents_attachment = iteration / by_path["AGENTS.md"]["attachment"]
        assert (
            agents_attachment.read_text(encoding="utf-8") == "Follow focused tests.\n"
        )
        assert iteration / "diff.patch" in bundle.attachments
        assert "binary 3 bytes" in (iteration / "changed-files.txt").read_text()

    def test_bundle_uses_captured_shas_after_a_force_push_back(self) -> None:
        # The remote moves from captured A to B and back to A while context is
        # collected. A mutable PR diff could be substituted with B; the local
        # immutable A range must be used instead.
        base = self.pr.base_sha
        other = pathlib.Path(self.temporary.name) / "other"
        self.git(
            pathlib.Path(self.temporary.name), "clone", str(self.remote), str(other)
        )
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.test")
        self.git(other, "checkout", "-B", "feature", base)
        (other / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        self.git(other, "commit", "-am", "replacement B")
        replacement = self.git(other, "rev-parse", "HEAD")
        self.git(other, "push", "--force", "origin", "HEAD:refs/heads/feature")
        self.git(
            self.primary,
            "push",
            "--force",
            "origin",
            f"{self.pr.head_sha}:refs/heads/feature",
        )

        iteration = self.loop.artifacts_dir / "force-push-back-bundle"
        iteration.mkdir()
        replacement_patch = self.git(
            other, "diff", "--binary", "--full-index", f"{base}...{replacement}"
        )
        with (
            mock.patch.object(
                self.loop, "_gh", return_value=replacement_patch
            ) as gh_call,
            mock.patch.object(self.loop, "snapshot", return_value=self.pr),
        ):
            self.loop.collect_bundle(self.pr, self.worktree, iteration)
        patch = (iteration / "diff.patch").read_text(encoding="utf-8")
        gh_call.assert_not_called()
        assert "+VALUE = 2" in patch
        assert "+VALUE = 99" not in patch

    def test_bundle_classifies_an_oversized_blob_with_late_invalid_utf8_as_binary_without_full_buffering(
        self,
    ) -> None:
        self.git(self.primary, "checkout", "feature")
        large_binary = (b"A" * loopr.MAX_ATTACHED_TEXT_BYTES) + b"\xff"
        (self.primary / ".gitattributes").write_text(
            "large.bin binary\n", encoding="utf-8"
        )
        (self.primary / "large.bin").write_bytes(large_binary)
        self.git(self.primary, "add", ".gitattributes", "large.bin")
        self.git(self.primary, "commit", "-m", "add oversized binary fixture")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 2
        raw["files"] = [
            {"path": ".gitattributes", "status": "added"},
            {"path": "large.bin", "status": "added"},
        ]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "oversized-binary-bundle"
        iteration.mkdir()

        original_stream = self.loop._stream_git_blob
        chunk_sizes: list[int] = []

        def observe_stream(
            worktree_arg: pathlib.Path,
            path: str,
            on_chunk: object,
        ) -> None:
            assert callable(on_chunk)

            def observe(chunk: bytes) -> None:
                chunk_sizes.append(len(chunk))
                on_chunk(chunk)

            original_stream(worktree_arg, path, observe)

        with (
            mock.patch.object(
                self.loop, "_stream_git_blob", side_effect=observe_stream
            ),
            mock.patch.object(
                self.loop,
                "_gh",
                return_value="diff --git a/large.bin b/large.bin\n",
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        assert len(chunk_sizes) > 1
        assert max(chunk_sizes) <= loopr.COMMAND_STREAM_CHUNK_BYTES
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["large.bin"]["kind"] == "binary"
        assert by_path["large.bin"]["attachment"] is None
        assert (
            f"binary {len(large_binary)} bytes"
            in (iteration / "changed-files.txt").read_text()
        )

    def test_bundle_rejects_oversized_valid_utf8_after_streaming_classification(
        self,
    ) -> None:
        self.git(self.primary, "checkout", "feature")
        large_text = b"A" * (loopr.MAX_ATTACHED_TEXT_BYTES + 1)
        (self.primary / ".gitattributes").write_text(
            "large.txt binary\n", encoding="utf-8"
        )
        (self.primary / "large.txt").write_bytes(large_text)
        self.git(self.primary, "add", ".gitattributes", "large.txt")
        self.git(self.primary, "commit", "-m", "add oversized text fixture")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 2
        raw["files"] = [
            {"path": ".gitattributes", "status": "added"},
            {"path": "large.txt", "status": "added"},
        ]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "oversized-text-bundle"
        iteration.mkdir()
        original_stream = self.loop._stream_git_blob
        chunk_sizes: list[int] = []

        def observe_stream(
            worktree_arg: pathlib.Path,
            path: str,
            on_chunk: object,
        ) -> None:
            assert callable(on_chunk)

            def observe(chunk: bytes) -> None:
                chunk_sizes.append(len(chunk))
                on_chunk(chunk)

            original_stream(worktree_arg, path, observe)

        with (
            mock.patch.object(
                self.loop, "_stream_git_blob", side_effect=observe_stream
            ),
            mock.patch.object(
                self.loop, "_gh", return_value="diff --git a/large.txt b/large.txt\n"
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        assert caught.value.code == loopr.EXIT_PRECONDITION
        assert len(chunk_sizes) > 1
        assert max(chunk_sizes) <= loopr.COMMAND_STREAM_CHUNK_BYTES

    def test_bundle_does_not_treat_a_split_utf8_codepoint_as_binary(self) -> None:
        self.git(self.primary, "checkout", "feature")
        content = b"A" * (loopr.COMMAND_STREAM_CHUNK_BYTES - 1) + "é".encode() + b"\n"
        (self.primary / "split.txt").write_bytes(content)
        self.git(self.primary, "add", "split.txt")
        self.git(self.primary, "commit", "-m", "add split utf8 fixture")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "split.txt", "status": "added"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "split-utf8-bundle"
        iteration.mkdir()
        with (
            mock.patch.object(
                self.loop, "_gh", return_value="diff --git a/split.txt b/split.txt\n"
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        entry = next(item for item in manifest if item["path"] == "split.txt")
        assert entry["kind"] == "text"
        attachment = iteration / entry["attachment"]
        assert content.decode("utf-8") == attachment.read_text()

    def test_bundle_marks_a_deleted_file_from_change_type_without_reading_it(
        self,
    ) -> None:
        self.git(self.primary, "checkout", "feature")
        self.git(self.primary, "rm", "app.py")
        self.git(self.primary, "commit", "-m", "remove app file")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "app.py", "changeType": "DELETED"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "deleted-bundle"
        iteration.mkdir()
        with (
            mock.patch.object(
                self.loop,
                "_gh",
                return_value="diff --git a/app.py b/app.py\n",
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        assert "-VALUE = 1" in (iteration / "diff.patch").read_text(encoding="utf-8")
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["app.py"]["kind"] == "deleted"
        assert by_path["app.py"]["attachment"] is None
        assert "no current content" in (iteration / "changed-files.txt").read_text()

    def test_bundle_marks_a_deleted_file_without_a_gh_change_type_field(
        self,
    ) -> None:
        # `gh pr view --json files` only returns changeType/status on GitHub
        # CLI >= 2.88.0; older installations return just path/additions/
        # deletions. Deletion classification must not depend on either field
        # existing and must instead come from the local base/head diff.
        self.git(self.primary, "checkout", "feature")
        self.git(self.primary, "rm", "app.py")
        self.git(self.primary, "commit", "-m", "remove app file")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "app.py", "additions": 0, "deletions": 1}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "deleted-bundle-no-change-type"
        iteration.mkdir()
        with mock.patch.object(self.loop, "snapshot", return_value=pr):
            self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["app.py"]["kind"] == "deleted"
        assert by_path["app.py"]["attachment"] is None
        assert "-VALUE = 1" in (iteration / "diff.patch").read_text(encoding="utf-8")

    def test_bundle_preserves_the_source_path_of_a_pure_rename(self) -> None:
        # `gh pr view --json files` reports only the current (post-rename)
        # path. Without preserving the source side of the rename, the old
        # path's removal is invisible everywhere in the bundle and Oracle is
        # shown what looks like a brand-new file instead of a move. The base
        # is pinned to the commit immediately before the rename so the only
        # difference in range is the rename itself, which keeps Git's
        # similarity score high enough to emit a single `R` record and
        # exercise that code path deterministically.
        self.git(self.primary, "checkout", "feature")
        pre_rename_sha = self.git(self.primary, "rev-parse", "HEAD")
        self.git(self.primary, "mv", "app.py", "renamed.py")
        self.git(self.primary, "commit", "-m", "rename app file")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["baseRefOid"] = pre_rename_sha
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "renamed.py", "changeType": "RENAMED"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "pure-rename-bundle"
        iteration.mkdir()
        with mock.patch.object(self.loop, "snapshot", return_value=pr):
            self.loop.collect_bundle(pr, worktree, iteration)
        patch = (iteration / "diff.patch").read_text(encoding="utf-8")
        assert "rename from app.py" in patch
        assert "rename to renamed.py" in patch
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["app.py"]["kind"] == "deleted"
        assert by_path["app.py"]["renamedTo"] == "renamed.py"
        assert (
            iteration / by_path["renamed.py"]["attachment"]
        ).read_text() == "VALUE = 2\n"
        changed_text = (iteration / "changed-files.txt").read_text()
        assert "app.py\t[no current content, renamed to renamed.py]" in changed_text

    def test_bundle_preserves_rename_source_below_similarity_threshold(self) -> None:
        # When the rewrite is heavy enough that Git's similarity heuristic
        # does not pair the rename into a single `R` record, it instead
        # emits independent `D` (old path) and `A` (new path) records. The
        # `D` side refers to a path GitHub's reported `paths` never
        # mentions, so it must still be surfaced through the same fallback
        # collect_bundle uses for confirmed rename sources, or the old
        # content's disappearance goes unreported.
        self.git(self.primary, "checkout", "feature")
        pre_rename_sha = self.git(self.primary, "rev-parse", "HEAD")
        self.git(self.primary, "mv", "app.py", "renamed.py")
        (self.primary / "renamed.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.git(self.primary, "commit", "-am", "rename and rewrite app file")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["baseRefOid"] = pre_rename_sha
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "renamed.py", "changeType": "RENAMED"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "renamed-edited-bundle"
        iteration.mkdir()
        with mock.patch.object(self.loop, "snapshot", return_value=pr):
            self.loop.collect_bundle(pr, worktree, iteration)
        patch = (iteration / "diff.patch").read_text(encoding="utf-8")
        assert "rename from" not in patch
        assert "-VALUE = 2" in patch
        assert "+VALUE = 9" in patch
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["app.py"]["kind"] == "deleted"
        assert "renamedTo" not in by_path["app.py"]
        assert (
            iteration / by_path["renamed.py"]["attachment"]
        ).read_text() == "VALUE = 9\n"
        changed_text = (iteration / "changed-files.txt").read_text()
        assert "app.py\t[no current content]" in changed_text

    def test_bundle_rejects_a_rename_source_with_an_embedded_control_character(
        self,
    ) -> None:
        # A rename source path comes from Git's own `-z` diff output, not
        # from GitHub's already-validated `paths`, so it must be run through
        # the same validate_changed_path() fail-closed check before it is
        # ever written into changed-files.txt or the attachment manifest.
        # Otherwise a tracked path with an embedded tab or newline could
        # inject a fabricated line into review content Oracle reads as-is.
        # The base is pinned to right after the file is first renamed to the
        # control-character name, so that name is the direct rename source
        # in the base/head diff range rather than an invisible intermediate
        # step of repository history.
        self.git(self.primary, "checkout", "feature")
        evil_name = "evil\tname.txt"
        subprocess.run(
            ["git", "mv", "app.py", evil_name],
            cwd=self.primary,
            check=True,
            env=os.environ | {"GIT_LITERAL_PATHSPECS": "1"},
        )
        self.git(self.primary, "commit", "-m", "rename to a control-character path")
        pre_second_rename_sha = self.git(self.primary, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "mv", evil_name, "renamed.py"],
            cwd=self.primary,
            check=True,
            env=os.environ | {"GIT_LITERAL_PATHSPECS": "1"},
        )
        self.git(self.primary, "commit", "-m", "rename away from it again")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["baseRefOid"] = pre_second_rename_sha
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": "renamed.py", "changeType": "RENAMED"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "malicious-rename-source-bundle"
        iteration.mkdir()
        with (
            mock.patch.object(self.loop, "snapshot", return_value=pr),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        assert caught.value.code == loopr.EXIT_PRECONDITION

    def test_bundle_treats_pathspec_magic_filenames_as_literal(self) -> None:
        # A leading `:(...)` is parsed as a pathspec magic signature, and
        # `*`/`?`/`[...]` are glob wildcards, unless literal pathspec
        # handling is enabled. Without it, Git commands that receive these
        # exact GitHub-reported filenames either fail outright or resolve a
        # different set of objects than the single literal path intended.
        self.git(self.primary, "checkout", "feature")
        magic_names = [":(icase)notes.txt", "weird[1].txt", "what?.txt"]
        for name in magic_names:
            (self.primary / name).write_text(f"content for {name}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", *magic_names],
            cwd=self.primary,
            check=True,
            env=os.environ | {"GIT_LITERAL_PATHSPECS": "1"},
        )
        self.git(self.primary, "commit", "-m", "add magic-named files")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1 + len(magic_names)
        raw["files"] = [{"path": "app.py", "status": "modified"}] + [
            {"path": name, "status": "added"} for name in magic_names
        ]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "magic-pathspec-bundle"
        iteration.mkdir()
        with mock.patch.object(self.loop, "snapshot", return_value=pr):
            self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        for name in magic_names:
            assert by_path[name]["kind"] == "text"
            assert (
                f"content for {name}\n"
                == (iteration / by_path[name]["attachment"]).read_text()
            )

    def test_bundle_does_not_leak_unrelated_changes_via_a_wildcard_filename(
        self,
    ) -> None:
        # A pathspec restricted to a GitHub-reported filename containing `*`
        # must select exactly that file. Without literal pathspec handling,
        # the glob also matches `app.py`, which was changed in the same
        # commit but was never reported by GitHub, and its diff would leak
        # into the bundle scoped to a single unrelated file.
        self.git(self.primary, "checkout", "feature")
        wildcard_name = "app*"
        (self.primary / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.primary / wildcard_name).write_text(
            "wildcard content\n", encoding="utf-8"
        )
        self.git(self.primary, "add", "app.py")
        subprocess.run(
            ["git", "add", wildcard_name],
            cwd=self.primary,
            check=True,
            env=os.environ | {"GIT_LITERAL_PATHSPECS": "1"},
        )
        self.git(self.primary, "commit", "-m", "touch app.py and add wildcard file")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 1
        raw["files"] = [{"path": wildcard_name, "status": "added"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "wildcard-bundle"
        iteration.mkdir()
        with mock.patch.object(self.loop, "snapshot", return_value=pr):
            self.loop.collect_bundle(pr, worktree, iteration)
        patch = (iteration / "diff.patch").read_text(encoding="utf-8")
        assert "wildcard content" in patch
        assert "app.py" not in patch
        assert "VALUE = 3" not in patch

    def test_bundle_fails_closed_before_oracle_on_known_credential_in_patch(
        self,
    ) -> None:
        secret = "collision-secret"
        self.loop.runner._secrets.add(secret)
        self.git(self.primary, "checkout", "feature")
        (self.primary / "app.py").write_text(f"VALUE = {secret!r}\n", encoding="utf-8")
        self.git(self.primary, "commit", "-am", "add collision fixture")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["files"] = [{"path": "app.py", "status": "modified"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "credential-patch"
        iteration.mkdir()
        with (
            mock.patch.object(self.loop, "snapshot", return_value=pr),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        assert caught.value.code == loopr.EXIT_PRECONDITION
        assert secret not in str(caught.value)
        assert not (iteration / "diff.patch").exists()

    def test_bundle_detects_a_credential_split_across_blob_chunks(self) -> None:
        secret = "chunk-boundary-secret"
        self.loop.runner._secrets.add(secret)
        self.git(self.primary, "checkout", "feature")
        (self.primary / "app.py").write_text(secret + "\n", encoding="utf-8")
        self.git(self.primary, "commit", "-am", "add chunk collision fixture")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["files"] = [{"path": "app.py", "status": "modified"}]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "credential-chunk"
        iteration.mkdir()
        original_control = self.loop._control_command

        def fake_control(args: Any, **kwargs: Any) -> loopr.CommandResult:
            assert isinstance(args, (list, tuple))
            if "--full-index" in args:
                return loopr.CommandResult(
                    tuple(str(item) for item in args), 0, b"diff\n", ""
                )
            return original_control(args, **kwargs)

        def split_blob(_worktree: pathlib.Path, path: str, on_chunk: object) -> None:
            assert path == "app.py"
            assert callable(on_chunk)
            on_chunk(secret[:7].encode())
            on_chunk((secret[7:] + "\n").encode())

        with (
            mock.patch.object(self.loop, "_control_command", side_effect=fake_control),
            mock.patch.object(self.loop, "_stream_git_blob", side_effect=split_blob),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        assert caught.value.code == loopr.EXIT_PRECONDITION
        assert secret not in str(caught.value)

    def test_staged_patch_collision_fails_before_commit(self) -> None:
        secret = "staged-collision-secret"
        self.loop.runner._secrets.add(secret)
        (self.worktree / "app.py").write_text(secret + "\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "credential-staged"
        iteration.mkdir()
        before = self.git(self.worktree, "rev-parse", "HEAD")
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_CODEX
        assert secret not in str(caught.value)
        assert before == self.git(self.worktree, "rev-parse", "HEAD")
        assert not (iteration / "resulting.patch").exists()

    def test_oracle_output_collision_is_withheld_without_redaction(self) -> None:
        secret = "oracle-collision-secret"
        self.loop.runner._secrets.add(secret)
        iteration = self.loop.artifacts_dir / "credential-oracle"
        iteration.mkdir()
        raw_review = approval(self.pr.head_sha) | {
            "review_body": f"The value {secret} must not be retained."
        }

        def fake_oracle(command: list[str], **kwargs: object) -> loopr.CommandResult:
            output = pathlib.Path(command[command.index("--write-output") + 1])
            output.write_text(json.dumps(raw_review), encoding="utf-8")
            return loopr.CommandResult(tuple(command), 0, "", "")

        with (
            mock.patch.object(self.loop, "command", side_effect=fake_oracle),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        assert caught.value.code == loopr.EXIT_ORACLE
        assert secret not in str(caught.value)
        assert secret not in (iteration / "oracle-raw.md").read_text()

    def test_failed_oracle_output_collision_is_withheld_without_redaction(self) -> None:
        secret = "oracle-failure-collision-secret"
        self.loop.runner._secrets.add(secret)
        iteration = self.loop.artifacts_dir / "credential-oracle-failure"
        iteration.mkdir()

        def failing_oracle(command: list[str], **kwargs: object) -> loopr.CommandResult:
            output = pathlib.Path(command[command.index("--write-output") + 1])
            output.write_text(f"partial {secret}\n", encoding="utf-8")
            msg = "oracle failed"
            raise loopr.CommandError(msg)

        with (
            mock.patch.object(self.loop, "command", side_effect=failing_oracle),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        assert caught.value.code == loopr.EXIT_ORACLE
        assert secret not in str(caught.value)
        assert secret not in (iteration / "oracle-raw.md").read_text()

    def test_oracle_stdout_collision_is_withheld_without_redaction(self) -> None:
        secret = "oracle-stdout-collision-secret"
        self.loop.runner._secrets.add(secret)
        iteration = self.loop.artifacts_dir / "credential-oracle-stdout"
        iteration.mkdir()

        def noisy_oracle(command: list[str], **kwargs: object) -> loopr.CommandResult:
            return loopr.CommandResult(tuple(command), 0, f"log {secret}\n", "")

        with (
            mock.patch.object(self.loop, "command", side_effect=noisy_oracle),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        assert caught.value.code == loopr.EXIT_ORACLE
        assert secret not in str(caught.value)
        assert secret not in (iteration / "oracle-raw.md").read_text()

    def test_bundle_marks_a_submodule_gitlink_without_attaching_content(self) -> None:
        root = pathlib.Path(self.temporary.name)
        submodule_remote = root / "submodule.git"
        self.git(root, "init", "--bare", str(submodule_remote))
        submodule_seed = root / "submodule-seed"
        self.git(root, "clone", str(submodule_remote), str(submodule_seed))
        self.git(submodule_seed, "config", "user.name", "Loop Test")
        self.git(submodule_seed, "config", "user.email", "loop@example.test")
        (submodule_seed / "lib.txt").write_text("lib\n", encoding="utf-8")
        self.git(submodule_seed, "add", "lib.txt")
        self.git(submodule_seed, "commit", "-m", "seed")
        self.git(submodule_seed, "push", "-u", "origin", "HEAD:refs/heads/main")
        self.git(submodule_remote, "symbolic-ref", "HEAD", "refs/heads/main")

        self.git(self.primary, "checkout", "feature")
        self.git(
            self.primary,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule_remote),
            "vendor/lib",
        )
        self.git(self.primary, "commit", "-m", "add vendor submodule")
        self.git(self.primary, "push", "origin", "feature")
        sha = self.git(self.primary, "rev-parse", "HEAD")
        raw = dict(self.pr.raw)
        raw["headRefOid"] = sha
        raw["changedFiles"] = 2
        raw["files"] = [
            {"path": ".gitmodules", "changeType": "ADDED"},
            {"path": "vendor/lib", "changeType": "ADDED"},
        ]
        pr = loopr.PullRequest.from_json("acme/project", raw)
        worktree = self.loop.prepare_worktree(pr)
        iteration = self.loop.artifacts_dir / "gitlink-bundle"
        iteration.mkdir()
        with (
            mock.patch.object(
                self.loop, "_gh", return_value="diff --git a/vendor/lib\n"
            ),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        assert by_path["vendor/lib"]["kind"] == "gitlink"
        assert by_path["vendor/lib"]["attachment"] is None

    def test_bundle_rejects_more_than_one_hundred_files_before_diff(self) -> None:
        raw = dict(self.pr.raw)
        raw["changedFiles"] = 101
        raw["files"] = []
        oversized = loopr.PullRequest.from_json("acme/project", raw)
        iteration = self.loop.artifacts_dir / "oversized"
        iteration.mkdir()
        with (
            mock.patch.object(self.loop, "_gh") as gh_call,
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(oversized, self.worktree, iteration)
        assert caught.value.code == loopr.EXIT_PRECONDITION
        gh_call.assert_not_called()

    def test_real_remote_race_is_detected_before_staging_or_commit(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        other = pathlib.Path(self.temporary.name) / "other"
        self.git(
            pathlib.Path(self.temporary.name), "clone", str(self.remote), str(other)
        )
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.test")
        self.git(other, "checkout", "feature")
        (other / "other.txt").write_text("race\n", encoding="utf-8")
        self.git(other, "add", "other.txt")
        self.git(other, "commit", "-m", "race")
        self.git(other, "push", "origin", "feature")
        iteration = self.loop.artifacts_dir / "iteration"
        iteration.mkdir()
        before = self.git(self.worktree, "rev-parse", "HEAD")
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_RACE
        assert before == self.git(self.worktree, "rev-parse", "HEAD")
        assert not self.git(self.worktree, "diff", "--name-only", "--cached")

    def test_whitespace_errors_fail(self) -> None:
        (self.worktree / "bad.txt").write_text("trailing space \n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "iteration-a"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_new_nested_repository_fails(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        nested = self.worktree / "vendor" / ".git"
        nested.mkdir(parents=True)
        iteration = self.loop.artifacts_dir / "iteration-b"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                set(),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_new_submodule_url_fails(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.worktree / ".gitmodules").write_text(
            '[submodule "vendor"]\n\tpath = vendor\n\turl = https://example.test/repo.git\n',
            encoding="utf-8",
        )
        iteration = self.loop.artifacts_dir / "iteration-c"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_codex_cannot_change_head_or_pre_stage_files(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.git(self.worktree, "add", "app.py")
        iteration = self.loop.artifacts_dir / "iteration-d"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_codex_cannot_change_the_worktree_git_pointer(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.worktree / ".git").write_text(
            "gitdir: /tmp/untrusted\n", encoding="utf-8"
        )
        iteration = self.loop.artifacts_dir / "iteration-e"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                {},
                set(),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_codex_cannot_change_repository_git_configuration(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.git(
            self.worktree,
            "config",
            "remote.origin.pushurl",
            "https://example.test/exfiltrate.git",
        )
        iteration = self.loop.artifacts_dir / "iteration-f"
        iteration.mkdir()
        with pytest.raises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                {},
                set(),
            )
        assert caught.value.code == loopr.EXIT_CODEX

    def test_post_review_anchors_the_submission_to_the_reviewed_commit(self) -> None:
        review = loopr.parse_oracle_review(
            json.dumps(approval(self.pr.head_sha)), self.pr.head_sha
        )
        iteration = self.loop.artifacts_dir / "post-review"
        iteration.mkdir()
        response = json.dumps({"commit_id": self.pr.head_sha, "id": 1})
        with (
            mock.patch.object(self.loop, "snapshot", return_value=self.pr),
            mock.patch.object(self.loop, "_gh", return_value=response) as gh_call,
        ):
            self.loop.post_review(self.pr, review, 1, iteration)
        args, kwargs = gh_call.call_args
        assert args[0] == [
            "api",
            "--hostname",
            "github.com",
            "repos/acme/project/pulls/7/reviews",
            "--method",
            "POST",
            "--input",
            "-",
        ]
        assert kwargs["reviewer"]
        payload = json.loads(kwargs["input_text"])
        assert self.pr.head_sha == payload["commit_id"]
        assert payload["event"] == "APPROVE"

    def test_post_review_rejects_a_response_anchored_to_the_wrong_commit(self) -> None:
        review = loopr.parse_oracle_review(
            json.dumps(approval(self.pr.head_sha)), self.pr.head_sha
        )
        iteration = self.loop.artifacts_dir / "post-review-race"
        iteration.mkdir()
        responses = iter([
            json.dumps({"commit_id": "f" * 40, "id": 2}),
            json.dumps({"id": 2, "state": "DISMISSED"}),
        ])
        with (
            mock.patch.object(self.loop, "snapshot", return_value=self.pr),
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ),
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.post_review(self.pr, review, 1, iteration)
        assert caught.value.code == loopr.EXIT_RACE

    def test_post_review_dismisses_when_only_the_base_moves(self) -> None:
        review = loopr.parse_oracle_review(
            json.dumps(approval(self.pr.head_sha)), self.pr.head_sha
        )
        iteration = self.loop.artifacts_dir / "post-review-base-race"
        iteration.mkdir()
        changed = loopr.PullRequest.from_json(
            self.pr.repo,
            self.pr.raw | {"baseRefOid": "d" * 40},
        )
        responses = iter([
            json.dumps({"id": 12, "commit_id": self.pr.head_sha}),
            json.dumps({"id": 12, "state": "DISMISSED"}),
        ])
        with (
            mock.patch.object(self.loop, "snapshot", side_effect=[self.pr, changed]),
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ) as gh_call,
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.post_review(self.pr, review, 1, iteration)
        assert caught.value.code == loopr.EXIT_RACE
        assert gh_call.call_count == 2
        assert any("/reviews" in value for value in gh_call.call_args_list[0].args[0])
        assert any(
            "/dismissals" in value for value in gh_call.call_args_list[1].args[0]
        )

    def test_verify_approval_dismisses_when_base_moves_after_post(self) -> None:
        responses = iter([
            json.dumps({
                "headRefOid": self.pr.head_sha,
                "baseRefOid": "d" * 40,
                "reviewDecision": "APPROVED",
                "state": "OPEN",
                "isDraft": False,
            }),
            json.dumps({"id": 13, "state": "DISMISSED"}),
        ])
        with (
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ) as gh_call,
            pytest.raises(loopr.LoopError) as caught,
        ):
            self.loop.verify_approval(self.pr.head_sha, self.pr.base_sha, 13)
        assert caught.value.code == loopr.EXIT_RACE
        assert any(
            "/dismissals" in value for value in gh_call.call_args_list[1].args[0]
        )


if __name__ == "__main__":
    unittest.main()
