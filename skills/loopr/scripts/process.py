"""Cross-platform bounded subprocess execution."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_OUTPUT = 24 * 1024 * 1024
MAX_INPUT = 4 * 1024 * 1024
MAX_STDERR = 1024 * 1024
POLL_INTERVAL_SECONDS = 0.01
TERMINATION_GRACE_SECONDS = 2


@dataclass(frozen=True)
class CommandResult:
    """A completed bounded command result."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: str


class CommandError(RuntimeError):
    """A redacted subprocess failure."""


class CommandRunner:
    """Run trusted executables with bounded input, output, and lifetime."""

    def __init__(self, source_env: Mapping[str, str] | None = None) -> None:
        """Capture the source environment and known secret values."""
        self.source_env = dict(source_env or os.environ)
        self.secrets = {
            value
            for key, value in self.source_env.items()
            if value
            and len(value) >= 4
            and any(
                marker in key.upper()
                for marker in (
                    "TOKEN",
                    "SECRET",
                    "PASSWORD",
                    "PASSWD",
                    "API_KEY",
                    "ACCESS_KEY",
                    "PRIVATE_KEY",
                    "CREDENTIAL",
                )
            )
        }

    def redact(self, text: str) -> str:
        """Replace every known secret value in text."""
        redacted = text
        for secret in sorted(self.secrets, key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def contains_secret(self, value: str | bytes) -> bool:
        """Return whether value contains a known secret."""
        if isinstance(value, bytes):
            return any(secret.encode() in value for secret in self.secrets)
        return any(secret in value for secret in self.secrets)

    def trusted(self, name: str) -> str:
        """Resolve an executable only through absolute PATH entries."""
        candidate = Path(name)
        if candidate.is_absolute():
            return str(candidate)
        paths = [
            entry
            for entry in self.source_env.get("PATH", "").split(os.pathsep)
            if Path(entry).is_absolute()
        ]
        found = shutil.which(name, path=os.pathsep.join(paths))
        if found is None:
            message = f"required executable not found: {name}"
            raise CommandError(message)
        return str(Path(found).resolve())

    def base_env(self) -> dict[str, str]:
        """Return the source environment without the reviewer credential."""
        env = dict(self.source_env)
        env.pop("GH_REVIEW_TOKEN", None)
        return env

    def allowlisted_env(self, extra: set[str] | None = None) -> dict[str, str]:
        """Return a small environment allowlist for an external tool."""
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LANGUAGE",
            "TERM",
            "NO_COLOR",
            "TZ",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        } | (extra or set())
        return {
            key: value
            for key, value in self.base_env().items()
            if key.upper() in allowed or key.upper().startswith("LC_")
        }

    def gh_env(self, reviewer_token: str | None = None) -> dict[str, str]:
        """Return the GitHub CLI environment for read or review operations."""
        env = self.allowlisted_env({
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_CONFIG_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        })
        if reviewer_token is not None:
            env.pop("GITHUB_TOKEN", None)
            env["GH_TOKEN"] = reviewer_token
            self.secrets.add(reviewer_token)
        return env

    def oracle_env(self) -> dict[str, str]:
        """Return the minimal browser environment supplied to Oracle."""
        return self.allowlisted_env({
            "CHROME_PATH",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS",
            "ORACLE_BROWSER_PROFILE_DIR",
            "ORACLE_CHATGPT_ACCOUNT_EMAIL",
        })

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = MAX_OUTPUT,
        watch_path: Path | None = None,
    ) -> CommandResult:
        """Run a command while enforcing bounds before data reaches memory.

        When `watch_path` is given, its on-disk size is polled alongside the
        stdout/stderr spools so a process that writes its real payload to a
        side file (rather than stdout) cannot exhaust disk during a long
        timeout window; the process group is terminated as soon as it grows
        past `max_output`. Because the child runs in its own session, a
        `KeyboardInterrupt` raised anywhere during monitoring is caught to
        terminate and reap the child before propagating, so an interrupt
        cannot leave it running past the command's lifetime.
        """
        argv = tuple(str(value) for value in args)
        if not argv or any("\0" in value for value in argv):
            raise CommandError("invalid subprocess argument vector")
        if timeout <= 0 or max_output <= 0:
            raise CommandError("subprocess bounds must be positive")
        executable = self.trusted(argv[0])
        argv = (executable, *argv[1:])
        input_bytes = b"" if input_text is None else input_text.encode()
        if len(input_bytes) > MAX_INPUT:
            raise CommandError("command input exceeded bound")

        with (
            tempfile.TemporaryFile(mode="w+b") as stdin_file,
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            stdin_file.write(input_bytes)
            stdin_file.seek(0)
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=stdin_file if input_text is not None else subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                shell=False,
            )
            try:
                deadline = time.monotonic() + timeout
                stderr_limit = min(max_output, MAX_STDERR)
                while proc.poll() is None:
                    self._enforce_output_bounds(
                        proc,
                        stdout_file,
                        stderr_file,
                        max_output,
                        stderr_limit,
                        argv,
                        watch_path,
                    )
                    if time.monotonic() >= deadline:
                        self._terminate_group(proc)
                        command = self.redact(" ".join(argv))
                        message = f"command timed out after {timeout}s: {command}"
                        raise CommandError(message)
                    time.sleep(POLL_INTERVAL_SECONDS)

                self._enforce_output_bounds(
                    proc,
                    stdout_file,
                    stderr_file,
                    max_output,
                    stderr_limit,
                    argv,
                    watch_path,
                )
                self._terminate_group(proc)
                self._enforce_output_bounds(
                    proc,
                    stdout_file,
                    stderr_file,
                    max_output,
                    stderr_limit,
                    argv,
                    watch_path,
                )
                stdout = self._read_spool(stdout_file, max_output)
                stderr_bytes = self._read_spool(stderr_file, stderr_limit)
            except BaseException:
                self._terminate_group(proc)
                raise

        stderr = self.redact(stderr_bytes.decode("utf-8", "replace"))
        result = CommandResult(argv, proc.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            detail = (
                stderr.strip() or self.redact(stdout.decode("utf-8", "replace")).strip()
            )
            command = self.redact(" ".join(argv))
            message = f"command failed ({proc.returncode}): {command}: {detail[:2000]}"
            raise CommandError(message)
        return result

    def _enforce_output_bounds(
        self,
        proc: subprocess.Popen[bytes],
        stdout_file: BinaryIO,
        stderr_file: BinaryIO,
        stdout_limit: int,
        stderr_limit: int,
        argv: tuple[str, ...],
        watch_path: Path | None = None,
    ) -> None:
        """Terminate the process group as soon as a spool exceeds its bound."""
        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        watch_size = 0
        if watch_path is not None:
            with suppress(OSError):
                watch_size = watch_path.stat().st_size
        if (
            stdout_size <= stdout_limit
            and stderr_size <= stderr_limit
            and watch_size <= stdout_limit
        ):
            return
        self._terminate_group(proc)
        command = self.redact(" ".join(argv))
        message = f"command output exceeded bound: {command}"
        raise CommandError(message)

    @staticmethod
    def _read_spool(handle: BinaryIO, limit: int) -> bytes:
        """Read a previously bounded private spool."""
        handle.seek(0)
        return handle.read(limit)

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
        """Terminate and reap the complete subprocess group.

        The group leader exiting does not imply the group is empty: a
        same-session descendant can outlive it. Signal the whole process
        group by pgid regardless of the leader's state, then poll the group
        itself (not just the leader) until no member remains.
        """
        pgid = proc.pid
        if proc.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                proc.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)

        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
