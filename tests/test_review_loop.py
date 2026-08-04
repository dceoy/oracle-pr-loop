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
        self.assertEqual("APPROVE", parsed.verdict)
        self.assertFalse(parsed.blocking_findings)

    def test_valid_request_changes_fixture(self) -> None:
        parsed = loopr.parse_oracle_review(json.dumps(request_changes()), SHA_A)
        self.assertEqual("REQUEST_CHANGES", parsed.verdict)
        self.assertTrue(parsed.implementation_prompt)

    def test_one_outer_json_fence_is_tolerated(self) -> None:
        raw = "```json\n" + json.dumps(approval()) + "\n```"
        self.assertEqual("APPROVE", loopr.parse_oracle_review(raw, SHA_A).verdict)

    def test_malformed_and_trailing_output_fail(self) -> None:
        fixtures = ["not json", json.dumps(approval()) + " trailing", "{}\n{}"]
        for fixture in fixtures:
            with (
                self.subTest(fixture=fixture),
                self.assertRaises(loopr.LoopError) as caught,
            ):
                loopr.parse_oracle_review(fixture, SHA_A)
            self.assertEqual(loopr.EXIT_ORACLE, caught.exception.code)

    def test_stale_sha_fails(self) -> None:
        with self.assertRaises(loopr.LoopError) as caught:
            loopr.parse_oracle_review(json.dumps(approval(SHA_B)), SHA_A)
        self.assertEqual(loopr.EXIT_ORACLE, caught.exception.code)

    def test_verdict_invariants_are_enforced(self) -> None:
        bad = approval()
        bad["implementation_prompt"] = "make changes"
        with self.assertRaises(loopr.LoopError):
            loopr.parse_oracle_review(json.dumps(bad), SHA_A)
        bad = request_changes()
        bad["blocking_findings"] = []
        with self.assertRaises(loopr.LoopError):
            loopr.parse_oracle_review(json.dumps(bad), SHA_A)


class InputAndIsolationTests(unittest.TestCase):
    def test_pr_resolution_is_canonical_and_unambiguous(self) -> None:
        self.assertEqual(
            ("acme/project", 7, "https://github.com/acme/project/pull/7"),
            loopr.resolve_pr_target("7", "acme/project"),
        )
        self.assertEqual(
            ("acme/project", 8, "https://github.com/acme/project/pull/8"),
            loopr.resolve_pr_target(
                "https://github.com/acme/project/pull/8", "elsewhere/repo"
            ),
        )
        for invalid in (
            "0",
            "https://evil.example/acme/project/pull/7",
            "https://github.com/a/b/pull/7?x=1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(loopr.LoopError):
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
            with self.assertRaises(loopr.LoopError):
                instance._validate_snapshot(mismatched)

    def test_remote_normalization_rejects_non_github_hosts(self) -> None:
        self.assertEqual(
            "acme/project",
            loopr.normalize_github_repo("git@github.com:acme/project.git"),
        )
        self.assertEqual(
            "acme/project",
            loopr.normalize_github_repo("https://github.com/acme/project.git"),
        )
        with self.assertRaises(loopr.LoopError):
            loopr.normalize_github_repo("https://github.example/acme/project.git")

    def test_unsupported_posix_fails_before_bootstrap_or_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = loopr.ReviewLoop(
                args_for(pathlib.Path(temporary)),
                loopr.CommandRunner({"PATH": os.environ["PATH"]}),
            )
            with (
                mock.patch.object(loopr.sys, "platform", "darwin"),
                mock.patch.object(loopr.os, "name", "posix"),
                mock.patch.object(instance, "_bootstrap") as bootstrap,
                self.assertRaises(loopr.LoopError) as caught,
            ):
                instance.execute()
            self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
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
            self.assertNotIn(key, env)

    def test_reviewer_token_is_scoped_and_redacted(self) -> None:
        runner = loopr.CommandRunner(
            {"PATH": "/bin", "GH_REVIEW_TOKEN": "review-secret"}
        )
        self.assertNotIn("GH_REVIEW_TOKEN", runner.base_env())
        review_env = runner.reviewer_env("review-secret")
        self.assertEqual("review-secret", review_env["GH_TOKEN"])
        self.assertEqual("failure [REDACTED]", runner.redact("failure review-secret"))

    def test_gh_env_never_allowlists_an_enterprise_host_or_token(self) -> None:
        runner = loopr.CommandRunner(
            {
                "PATH": "/bin",
                "GH_HOST": "github.example.com",
                "GH_ENTERPRISE_TOKEN": "enterprise-secret",
            }
        )
        self.assertNotIn("GH_HOST", runner.gh_env())
        self.assertNotIn("GH_ENTERPRISE_TOKEN", runner.gh_env())
        self.assertNotIn("GH_HOST", runner.reviewer_env("review-secret"))
        self.assertNotIn("GH_ENTERPRISE_TOKEN", runner.reviewer_env("review-secret"))

    def test_same_pr_lock_rejects_second_process_and_releases(self) -> None:
        first = loopr.PrLock("acme/project", 7)
        second = loopr.PrLock("acme/project", 7)
        with first:
            with self.assertRaises(loopr.LoopError) as caught:
                second.__enter__()
            self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
        with second:
            self.assertTrue(second.path.exists())
        self.assertFalse(second.path.exists())

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
                with self.assertRaises(loopr.LoopError) as caught:
                    contender.__enter__()
                elapsed = time.monotonic() - start
            self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
            self.assertLess(elapsed, 2.0)
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
        self.assertEqual(1, len(successes))
        for index, result in enumerate(results):
            if index not in successes:
                self.assertIsInstance(result, loopr.LoopError)
        winner = locks[successes[0]]
        self.assertEqual(
            str(os.getpid()), winner.path.read_text(encoding="ascii").strip()
        )
        winner.__exit__(None, None, None)
        self.assertFalse(winner.path.exists())

    def test_lock_release_closes_the_descriptor_before_unlinking(self) -> None:
        # On Windows an open os.open() handle does not grant delete-sharing,
        # so unlink() must not be attempted while the descriptor is still
        # open. Assert the fd is already closed (via EBADF) by the time
        # unlink() runs, without patching the global os.close.
        lock = loopr.PrLock("acme/project", 9)
        lock.__enter__()
        fd = lock.fd
        assert fd is not None
        original_unlink = pathlib.Path.unlink
        unlink_called = False

        def recording_unlink(target: pathlib.Path, *args: Any, **kwargs: Any) -> None:
            nonlocal unlink_called
            unlink_called = True
            with self.assertRaises(OSError):
                os.fstat(fd)
            original_unlink(target, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "unlink", recording_unlink):
            lock.__exit__(None, None, None)
        self.assertTrue(unlink_called)
        self.assertFalse(lock.path.exists())

    def test_windows_job_object_contains_a_detached_grandchild(self) -> None:
        if os.name != "nt":
            self.skipTest(
                "Windows Job Object containment is exercised via real "
                "kernel32 calls and cannot be validated off Windows"
            )
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
            self.assertEqual(0, result.returncode)
            self.assertFalse(marker.exists())
            time.sleep(1.5)
            self.assertFalse(marker.exists())

    def test_pid_liveness_on_windows_never_probes_with_os_kill(self) -> None:
        with (
            mock.patch.object(loopr.os, "name", "nt"),
            mock.patch.object(
                loopr.PrLock, "_pid_alive_windows", return_value=True
            ) as windows_probe,
            mock.patch.object(loopr.os, "kill") as kill_call,
        ):
            self.assertTrue(loopr.PrLock._pid_alive(4321))
        windows_probe.assert_called_once_with(4321)
        kill_call.assert_not_called()

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
            self.assertEqual(128, len(result.stdout))
            with self.assertRaises(loopr.CommandError) as bounded:
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
            self.assertIn("exceeded", str(bounded.exception))

    def test_command_wrapper_bounds_and_redacts_diagnostics(self) -> None:
        runner = loopr.CommandRunner(
            {"PATH": os.environ["PATH"], "TEST_TOKEN": "hidden-value"}
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run(
                ["python3", "-c", "print('ok')"],
                cwd=pathlib.Path(temporary),
                env=runner.base_env(),
            )
            self.assertEqual("ok\n", result.stdout)
            with self.assertRaises(loopr.CommandError) as caught:
                runner.run(
                    [
                        "python3",
                        "-c",
                        "import sys; print('hidden-value', file=sys.stderr); sys.exit(1)",
                    ],
                    cwd=pathlib.Path(temporary),
                    env=runner.base_env(),
                )
            self.assertNotIn("hidden-value", str(caught.exception))
            with self.assertRaises(loopr.CommandError) as bounded:
                runner.run(
                    ["python3", "-c", "print('x' * 10000)"],
                    cwd=pathlib.Path(temporary),
                    env=runner.base_env(),
                    max_output_bytes=128,
                )
            self.assertIn("exceeded 128 bytes", str(bounded.exception))

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
            self.assertEqual(0, result.returncode)
            self.assertFalse(marker.exists())
            time.sleep(1.5)
            self.assertFalse(marker.exists())

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
            self.assertEqual(0, result.returncode)
            self.assertFalse(marker.exists())
            time.sleep(1.5)
            self.assertFalse(marker.exists())

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
            self.assertEqual(0, result.returncode)
            self.assertFalse(marker.exists())
            time.sleep(1.5)
            self.assertFalse(marker.exists())

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
                self.assertRaises(loopr.CommandError),
            ):
                runner.run(
                    [sys.executable, "-c", payload, str(marker)],
                    cwd=root,
                    env=runner.base_env(),
                )
            self.assertFalse(marker.exists())

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
                self.assertRaises(loopr.CommandError),
            ):
                runner.run(
                    [sys.executable, "-c", payload, str(marker)],
                    cwd=root,
                    env=runner.base_env(),
                )
            self.assertFalse(marker.exists())

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
            self.assertTrue(loopr._linux_kill_pid(1234))
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
        self.assertLessEqual(len(raw), loopr.LINUX_STATUS_MAX_BYTES)
        parsed = json.loads(raw)
        self.assertEqual("error", parsed["type"])
        self.assertLessEqual(
            len(parsed["message"].encode("utf-8")), loopr.LINUX_STATUS_ERROR_BYTES + 3
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
                captured.update(
                    {str(key): str(value) for key, value in environment.items()}
                )
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
                self.assertNotIn(key, captured)


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
    ):
        runner = loopr.CommandRunner(
            {"PATH": os.environ["PATH"], "GH_REVIEW_TOKEN": "review-token"}
        )
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
        self.assertEqual(loopr.EXIT_OK, code)
        self.assertEqual(1, scripted.calls.count("codex"))
        self.assertEqual(1, scripted.calls.count("push"))
        self.assertIn("post:REQUEST_CHANGES", scripted.calls)
        self.assertIn("post:APPROVE", scripted.calls)

    def test_approval_exits_zero_without_codex(self) -> None:
        scripted, code, _ = self.run_script([approval()])
        self.assertEqual(loopr.EXIT_OK, code)
        self.assertNotIn("codex", scripted.calls)
        self.assertEqual(1, scripted.calls.count("post:APPROVE"))

    def test_dry_run_validates_without_artifacts_models_or_writes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        scripted = ScriptedLoop(root, [approval()])
        scripted.args.dry_run = True
        with mock.patch.object(loopr.sys, "platform", "linux"):
            self.assertEqual(loopr.EXIT_OK, scripted.execute())
        self.assertEqual(["precheck"], scripted.calls)
        self.assertFalse((root / ".pr-review-loop").exists())

    def test_no_op_codex_is_stalled_without_push(self) -> None:
        scripted, code, _ = self.run_script([request_changes()], no_op=True)
        self.assertEqual(loopr.EXIT_STALLED, code)
        self.assertEqual(1, scripted.calls.count("push"))

    def test_concurrent_remote_update_aborts(self) -> None:
        _scripted, code, _ = self.run_script([request_changes()], race=True)
        self.assertEqual(loopr.EXIT_RACE, code)

    def test_malformed_oracle_output_causes_no_write_or_edit(self) -> None:
        malformed = approval()
        del malformed["schema_version"]
        scripted, code, _ = self.run_script([malformed])
        self.assertEqual(loopr.EXIT_ORACLE, code)
        self.assertFalse(any(call.startswith("post:") for call in scripted.calls))
        self.assertNotIn("codex", scripted.calls)
        self.assertNotIn("push", scripted.calls)

    def test_maximum_iteration_is_exact_and_does_not_leave_unreviewed_patch(
        self,
    ) -> None:
        scripted, code, _ = self.run_script([request_changes()], maximum=1)
        self.assertEqual(loopr.EXIT_STALLED, code)
        self.assertEqual(1, scripted.calls.count("oracle"))
        self.assertNotIn("codex", scripted.calls)
        self.assertNotIn("push", scripted.calls)

    def test_complete_approval_artifacts_exist(self) -> None:
        scripted, code, _ = self.run_script([approval()])
        self.assertEqual(loopr.EXIT_OK, code)
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
        self.assertTrue(expected.issubset({path.name for path in iteration.iterdir()}))
        self.assertTrue((run / "state.json").is_file())
        self.assertTrue((run / "final.json").is_file())
        for path in run.rglob("*"):
            if path.is_file():
                self.assertNotIn("review-token", path.read_text(encoding="utf-8"))

    def test_self_review_rejected_before_posting(self) -> None:
        scripted, code, root = self.run_script([approval()], author="reviewer")
        self.assertEqual(loopr.EXIT_PRECONDITION, code)
        self.assertFalse(any(call.startswith("post:") for call in scripted.calls))
        self.assertFalse((root / ".pr-review-loop").exists())


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
        runner = loopr.CommandRunner(
            {"PATH": os.environ["PATH"], "GH_REVIEW_TOKEN": "review-token"}
        )
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
        runner = loopr.CommandRunner(
            {
                "PATH": os.environ["PATH"],
                "HOME": str(homeless),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "user.useConfigOnly",
                "GIT_CONFIG_VALUE_0": "true",
            }
        )
        instance = loopr.ReviewLoop(args_for(unset), runner)
        instance.repo_dir = unset
        with self.assertRaises(loopr.CommandError):
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
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop._check_pushable(self.pr)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)

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
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

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
        self.assertEqual(pushed, remote_sha)
        self.assertTrue((iteration / "resulting.patch").read_text().strip())

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
        self.assertTrue(pushed)
        self.assertFalse(marker.exists())

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
        self.assertTrue(pushed)
        self.assertFalse(marker.exists())
        common = pathlib.Path(self.git(self.primary, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.primary / common
        self.assertNotEqual(
            pathlib.Path(self.loop._control_repo or "").resolve(),
            common.resolve(),
        )

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
        self.assertFalse(marker.exists())

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
        self.assertFalse(marker.exists())
        self.assertEqual("VALUE = 2\n", (self.worktree / "app.py").read_text())

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
        self.assertFalse(marker.exists())

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

        self.assertFalse(marker.exists())
        indexed = subprocess.run(
            ["git", "show", ":app.py"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(b"VALUE = 9\n", indexed)

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
        self.assertEqual(
            [
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
            ],
            overrides,
        )

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

        self.assertEqual(0, result.returncode)
        self.assertFalse(marker.exists())
        indexed = subprocess.run(
            ["git", "show", ":app.py"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(original, indexed)

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
        self.assertFalse(marker.exists())
        self.assertEqual("VALUE = 2\n", (self.worktree / "app.py").read_text())

    def test_git_diff_ignores_a_configured_external_diff_command(self) -> None:
        # diff.external runs for every path with a working-tree change unless
        # --no-ext-diff is passed; git tries to execute the value verbatim, so
        # (unlike filter.*.process) it cannot be neutralized by emptying it.
        marker = pathlib.Path(self.temporary.name) / "ext-diff-fired"
        self.git(self.primary, "config", "diff.external", f"sh -c \"touch '{marker}'\"")
        (self.worktree / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.loop.command(["git", "diff"], cwd=self.worktree)
        self.assertFalse(marker.exists())

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
        self.assertFalse(marker.exists())

    def test_worktree_reset_on_reuse_ignores_an_executable_post_index_change_hook(
        self,
    ) -> None:
        marker = self._install_post_index_change_hook()
        reused = self.loop.prepare_worktree(self.pr)
        self.assertEqual(self.worktree, reused)
        self.assertFalse(marker.exists())

    def test_worktree_reuse_is_exact_and_dirty_state_fails_closed(self) -> None:
        primary_before = self.git(
            self.primary, "status", "--porcelain", "--untracked-files=all"
        )
        reused = self.loop.prepare_worktree(self.pr)
        self.assertEqual(self.worktree, reused)
        self.assertEqual(self.sha, self.git(reused, "rev-parse", "HEAD"))
        self.assertFalse(
            self.git(reused, "status", "--porcelain", "--untracked-files=all")
        )
        self.assertEqual(
            primary_before,
            self.git(self.primary, "status", "--porcelain", "--untracked-files=all"),
        )
        (reused / "dirty.txt").write_text("do not carry me\n", encoding="utf-8")
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.prepare_worktree(self.pr)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)

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
        self.assertEqual("binary", by_path["binary.dat"]["kind"])
        agents_attachment = iteration / by_path["AGENTS.md"]["attachment"]
        self.assertEqual(
            "Follow focused tests.\n", agents_attachment.read_text(encoding="utf-8")
        )
        self.assertIn(iteration / "diff.patch", bundle.attachments)
        self.assertIn("binary 3 bytes", (iteration / "changed-files.txt").read_text())

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
        self.assertIn("+VALUE = 2", patch)
        self.assertNotIn("+VALUE = 99", patch)

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
        self.assertGreater(len(chunk_sizes), 1)
        self.assertLessEqual(max(chunk_sizes), loopr.COMMAND_STREAM_CHUNK_BYTES)
        manifest = json.loads(
            (iteration / "attachments.json").read_text(encoding="utf-8")
        )
        by_path = {entry["path"]: entry for entry in manifest}
        self.assertEqual("binary", by_path["large.bin"]["kind"])
        self.assertIsNone(by_path["large.bin"]["attachment"])
        self.assertIn(
            f"binary {len(large_binary)} bytes",
            (iteration / "changed-files.txt").read_text(),
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
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
        self.assertGreater(len(chunk_sizes), 1)
        self.assertLessEqual(max(chunk_sizes), loopr.COMMAND_STREAM_CHUNK_BYTES)

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
        self.assertEqual("text", entry["kind"])
        attachment = iteration / entry["attachment"]
        self.assertEqual(content.decode("utf-8"), attachment.read_text())

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
        self.assertIn(
            "-VALUE = 1",
            (iteration / "diff.patch").read_text(encoding="utf-8"),
        )
        by_path = {entry["path"]: entry for entry in manifest}
        self.assertEqual("deleted", by_path["app.py"]["kind"])
        self.assertIsNone(by_path["app.py"]["attachment"])
        self.assertIn(
            "no current content", (iteration / "changed-files.txt").read_text()
        )

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
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertFalse((iteration / "diff.patch").exists())

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
            if len(args) > 2 and args[2] == "diff":
                return loopr.CommandResult(
                    tuple(str(item) for item in args), 0, b"diff\n", ""
                )
            return original_control(args, **kwargs)

        def split_blob(_worktree: pathlib.Path, path: str, on_chunk: object) -> None:
            self.assertEqual("app.py", path)
            assert callable(on_chunk)
            on_chunk(secret[:7].encode())
            on_chunk((secret[7:] + "\n").encode())

        with (
            mock.patch.object(self.loop, "_control_command", side_effect=fake_control),
            mock.patch.object(self.loop, "_stream_git_blob", side_effect=split_blob),
            mock.patch.object(self.loop, "snapshot", return_value=pr),
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(pr, worktree, iteration)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))

    def test_staged_patch_collision_fails_before_commit(self) -> None:
        secret = "staged-collision-secret"
        self.loop.runner._secrets.add(secret)
        (self.worktree / "app.py").write_text(secret + "\n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "credential-staged"
        iteration.mkdir()
        before = self.git(self.worktree, "rev-parse", "HEAD")
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(before, self.git(self.worktree, "rev-parse", "HEAD"))
        self.assertFalse((iteration / "resulting.patch").exists())

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
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        self.assertEqual(loopr.EXIT_ORACLE, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, (iteration / "oracle-raw.md").read_text())

    def test_failed_oracle_output_collision_is_withheld_without_redaction(self) -> None:
        secret = "oracle-failure-collision-secret"
        self.loop.runner._secrets.add(secret)
        iteration = self.loop.artifacts_dir / "credential-oracle-failure"
        iteration.mkdir()

        def failing_oracle(command: list[str], **kwargs: object) -> loopr.CommandResult:
            output = pathlib.Path(command[command.index("--write-output") + 1])
            output.write_text(f"partial {secret}\n", encoding="utf-8")
            raise loopr.CommandError("oracle failed")

        with (
            mock.patch.object(self.loop, "command", side_effect=failing_oracle),
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        self.assertEqual(loopr.EXIT_ORACLE, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, (iteration / "oracle-raw.md").read_text())

    def test_oracle_stdout_collision_is_withheld_without_redaction(self) -> None:
        secret = "oracle-stdout-collision-secret"
        self.loop.runner._secrets.add(secret)
        iteration = self.loop.artifacts_dir / "credential-oracle-stdout"
        iteration.mkdir()

        def noisy_oracle(command: list[str], **kwargs: object) -> loopr.CommandResult:
            return loopr.CommandResult(tuple(command), 0, f"log {secret}\n", "")

        with (
            mock.patch.object(self.loop, "command", side_effect=noisy_oracle),
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.oracle_review(
                self.pr,
                loopr.ReviewBundle(iteration, ()),
            )
        self.assertEqual(loopr.EXIT_ORACLE, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, (iteration / "oracle-raw.md").read_text())

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
        self.assertEqual("gitlink", by_path["vendor/lib"]["kind"])
        self.assertIsNone(by_path["vendor/lib"]["attachment"])

    def test_bundle_rejects_more_than_one_hundred_files_before_diff(self) -> None:
        raw = dict(self.pr.raw)
        raw["changedFiles"] = 101
        raw["files"] = []
        oversized = loopr.PullRequest.from_json("acme/project", raw)
        iteration = self.loop.artifacts_dir / "oversized"
        iteration.mkdir()
        with (
            mock.patch.object(self.loop, "_gh") as gh_call,
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.collect_bundle(oversized, self.worktree, iteration)
        self.assertEqual(loopr.EXIT_PRECONDITION, caught.exception.code)
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
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_RACE, caught.exception.code)
        self.assertEqual(before, self.git(self.worktree, "rev-parse", "HEAD"))
        self.assertFalse(self.git(self.worktree, "diff", "--name-only", "--cached"))

    def test_whitespace_errors_fail(self) -> None:
        (self.worktree / "bad.txt").write_text("trailing space \n", encoding="utf-8")
        iteration = self.loop.artifacts_dir / "iteration-a"
        iteration.mkdir()
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

    def test_new_nested_repository_fails(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        nested = self.worktree / "vendor" / ".git"
        nested.mkdir(parents=True)
        iteration = self.loop.artifacts_dir / "iteration-b"
        iteration.mkdir()
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                set(),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

    def test_new_submodule_url_fails(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.worktree / ".gitmodules").write_text(
            '[submodule "vendor"]\n\tpath = vendor\n\turl = https://example.test/repo.git\n',
            encoding="utf-8",
        )
        iteration = self.loop.artifacts_dir / "iteration-c"
        iteration.mkdir()
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

    def test_codex_cannot_change_head_or_pre_stage_files(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.git(self.worktree, "add", "app.py")
        iteration = self.loop.artifacts_dir / "iteration-d"
        iteration.mkdir()
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                self.loop._outside_state(self.worktree),
                self.loop._nested_git_entries(self.worktree),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

    def test_codex_cannot_change_the_worktree_git_pointer(self) -> None:
        (self.worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.worktree / ".git").write_text(
            "gitdir: /tmp/untrusted\n", encoding="utf-8"
        )
        iteration = self.loop.artifacts_dir / "iteration-e"
        iteration.mkdir()
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                {},
                set(),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

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
        with self.assertRaises(loopr.LoopError) as caught:
            self.loop.validate_commit_push(
                self.pr,
                self.worktree,
                1,
                iteration,
                {},
                set(),
            )
        self.assertEqual(loopr.EXIT_CODEX, caught.exception.code)

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
        self.assertEqual(
            [
                "api",
                "--hostname",
                "github.com",
                "repos/acme/project/pulls/7/reviews",
                "--method",
                "POST",
                "--input",
                "-",
            ],
            args[0],
        )
        self.assertTrue(kwargs["reviewer"])
        payload = json.loads(kwargs["input_text"])
        self.assertEqual(self.pr.head_sha, payload["commit_id"])
        self.assertEqual("APPROVE", payload["event"])

    def test_post_review_rejects_a_response_anchored_to_the_wrong_commit(self) -> None:
        review = loopr.parse_oracle_review(
            json.dumps(approval(self.pr.head_sha)), self.pr.head_sha
        )
        iteration = self.loop.artifacts_dir / "post-review-race"
        iteration.mkdir()
        responses = iter(
            [
                json.dumps({"commit_id": "f" * 40, "id": 2}),
                json.dumps({"id": 2, "state": "DISMISSED"}),
            ]
        )
        with (
            mock.patch.object(self.loop, "snapshot", return_value=self.pr),
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ),
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.post_review(self.pr, review, 1, iteration)
        self.assertEqual(loopr.EXIT_RACE, caught.exception.code)

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
        responses = iter(
            [
                json.dumps({"id": 12, "commit_id": self.pr.head_sha}),
                json.dumps({"id": 12, "state": "DISMISSED"}),
            ]
        )
        with (
            mock.patch.object(self.loop, "snapshot", side_effect=[self.pr, changed]),
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ) as gh_call,
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.post_review(self.pr, review, 1, iteration)
        self.assertEqual(loopr.EXIT_RACE, caught.exception.code)
        self.assertEqual(2, gh_call.call_count)
        self.assertTrue(
            any("/reviews" in value for value in gh_call.call_args_list[0].args[0])
        )
        self.assertTrue(
            any("/dismissals" in value for value in gh_call.call_args_list[1].args[0])
        )

    def test_verify_approval_dismisses_when_base_moves_after_post(self) -> None:
        responses = iter(
            [
                json.dumps(
                    {
                        "headRefOid": self.pr.head_sha,
                        "baseRefOid": "d" * 40,
                        "reviewDecision": "APPROVED",
                        "state": "OPEN",
                        "isDraft": False,
                    }
                ),
                json.dumps({"id": 13, "state": "DISMISSED"}),
            ]
        )
        with (
            mock.patch.object(
                self.loop, "_gh", side_effect=lambda *args, **kwargs: next(responses)
            ) as gh_call,
            self.assertRaises(loopr.LoopError) as caught,
        ):
            self.loop.verify_approval(self.pr.head_sha, self.pr.base_sha, 13)
        self.assertEqual(loopr.EXIT_RACE, caught.exception.code)
        self.assertTrue(
            any("/dismissals" in value for value in gh_call.call_args_list[1].args[0])
        )


if __name__ == "__main__":
    unittest.main()
