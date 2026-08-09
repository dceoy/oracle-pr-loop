"""Cross-platform bounded subprocess execution."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- this module is the sole, argv-validated, shell=False subprocess boundary
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_OUTPUT = 24 * 1024 * 1024
MAX_INPUT = 4 * 1024 * 1024
MAX_STDERR = 1024 * 1024
POLL_INTERVAL_SECONDS = 0.01
TERMINATION_GRACE_SECONDS = 2
MIN_SECRET_LENGTH = 4


@dataclass(frozen=True)
class CommandResult:
    """A completed bounded command result."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: str


class CommandError(RuntimeError):
    """A redacted subprocess failure with optional completed-process output."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """Initialize one failure, preserving optional bounded output metadata."""
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CommandRunner:
    """Run trusted executables with bounded input, output, and lifetime."""

    def __init__(self, source_env: Mapping[str, str] | None = None) -> None:
        """Capture the source environment and known secret values."""
        self.source_env = dict(source_env or os.environ)
        self.secrets = {
            value
            for key, value in self.source_env.items()
            if value
            and len(value) >= MIN_SECRET_LENGTH
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
        """Replace every known secret value in text.

        Returns:
            text with every known secret value replaced by a placeholder.
        """
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
        """Resolve an executable only through absolute PATH entries.

        Returns:
            The resolved absolute path to the executable.

        Raises:
            CommandError: No matching executable was found.
        """
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
        """Return the captured environment for local Git operations."""
        return dict(self.source_env)

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

    def gh_env(self) -> dict[str, str]:
        """Return the GitHub CLI environment for ordinary authenticated use."""
        env = self.allowlisted_env({
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_CONFIG_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        })
        return env

    def oracle_env(self) -> dict[str, str]:
        """Return the minimal environment supplied to Oracle.

        Includes Oracle's own remote-transport variables (`ORACLE_HOME_DIR`,
        `ORACLE_REMOTE_HOST`, `ORACLE_REMOTE_TOKEN`) so a host configured to
        use a remote `oracle serve` instance works without a local Chrome
        session and without this module implementing a second transport.
        """
        return self.allowlisted_env({
            "CHROME_PATH",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS",
            "ORACLE_BROWSER_PROFILE_DIR",
            "ORACLE_CHATGPT_ACCOUNT_EMAIL",
            "ORACLE_HOME_DIR",
            "ORACLE_REMOTE_HOST",
            "ORACLE_REMOTE_TOKEN",
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
        timeout window. A timeout, output overflow, or `KeyboardInterrupt`
        terminates and reaps the direct child before control returns.

        Returns:
            The completed command's bounded stdout, stderr, and return code.

        Raises:
            CommandError: The argument vector or bounds were invalid, the
                command timed out, its output exceeded a bound, or `check`
                is true and it exited non-zero.
        """
        argv = tuple(str(value) for value in args)
        if not argv or any("\0" in value for value in argv):
            msg = "invalid subprocess argument vector"
            raise CommandError(msg)
        if timeout <= 0 or max_output <= 0:
            msg = "subprocess bounds must be positive"
            raise CommandError(msg)
        executable = self.trusted(argv[0])
        argv = (executable, *argv[1:])
        input_bytes = b"" if input_text is None else input_text.encode()
        if len(input_bytes) > MAX_INPUT:
            msg = "command input exceeded bound"
            raise CommandError(msg)

        with (
            tempfile.TemporaryFile(mode="w+b") as stdin_file,
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            stdin_file.write(input_bytes)
            stdin_file.seek(0)
            proc = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] -- argv is a validated, non-shell tuple
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=stdin_file if input_text is not None else subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
            )
            stderr_limit = min(max_output, MAX_STDERR)
            try:
                self._await_completion(
                    proc,
                    stdout_file,
                    stderr_file,
                    max_output,
                    stderr_limit,
                    argv,
                    watch_path,
                    timeout,
                )
                stdout = self._read_spool(stdout_file, max_output)
                stderr_bytes = self._read_spool(stderr_file, stderr_limit)
            except BaseException:
                self._terminate_process(proc)
                raise

        stderr = self.redact(stderr_bytes.decode("utf-8", "replace"))
        result = CommandResult(argv, proc.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            detail = (
                stderr.strip() or self.redact(stdout.decode("utf-8", "replace")).strip()
            )
            command = self.redact(" ".join(argv))
            message = f"command failed ({proc.returncode}): {command}: {detail[:2000]}"
            raise CommandError(
                message,
                returncode=proc.returncode,
                stdout=self.redact(stdout.decode("utf-8", "replace")),
                stderr=stderr,
            )
        return result

    def _await_completion(
        self,
        proc: subprocess.Popen[bytes],
        stdout_file: BinaryIO,
        stderr_file: BinaryIO,
        max_output: int,
        stderr_limit: int,
        argv: tuple[str, ...],
        watch_path: Path | None,
        timeout: float,
    ) -> None:
        """Poll a running child until it exits, its bounds break, or it times out."""
        deadline = time.monotonic() + timeout
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
                self._terminate_process(proc)
                self._raise_timeout(argv, timeout)
            time.sleep(POLL_INTERVAL_SECONDS)
        self._enforce_output_bounds(
            proc, stdout_file, stderr_file, max_output, stderr_limit, argv, watch_path
        )

    def _raise_timeout(self, argv: tuple[str, ...], timeout: float) -> NoReturn:
        """Raise for a child that missed its wall-clock deadline.

        Raises:
            CommandError: Always.
        """
        command = self.redact(" ".join(argv))
        message = f"command timed out after {timeout}s: {command}"
        raise CommandError(message)

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
        """Terminate the direct child as soon as a spool exceeds its bound.

        Raises:
            CommandError: A bounded spool or the watched path exceeded its limit.
        """
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
        self._terminate_process(proc)
        command = self.redact(" ".join(argv))
        message = f"command output exceeded bound: {command}"
        raise CommandError(message)

    @staticmethod
    def _read_spool(handle: BinaryIO, limit: int) -> bytes:
        """Read a previously bounded private spool.

        Returns:
            The spool's contents, from the start, up to limit bytes.
        """
        handle.seek(0)
        return handle.read(limit)

    @staticmethod
    def _wait_for_process_exit(proc: subprocess.Popen[bytes], timeout: float) -> bool:
        """Wait at most the cleanup budget for the direct child to be reaped.

        Returns:
            Whether the process exited within the given timeout.
        """
        if proc.poll() is not None:
            return True
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    @classmethod
    def _terminate_process(cls, proc: subprocess.Popen[bytes]) -> None:
        """Terminate and reap the direct child within the cleanup budget.

        Raises:
            CommandError: The child could not be reaped within the budget.
        """
        if proc.poll() is not None:
            return
        with suppress(ProcessLookupError):
            proc.terminate()
        if cls._wait_for_process_exit(proc, TERMINATION_GRACE_SECONDS):
            return
        with suppress(ProcessLookupError):
            proc.kill()
        if cls._wait_for_process_exit(proc, TERMINATION_GRACE_SECONDS):
            return
        msg = "could not reap subprocess"
        raise CommandError(msg)
