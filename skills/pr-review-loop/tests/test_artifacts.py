"""Tests for command-scoped private Oracle files."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts import artifacts as artifacts_module
from scripts.artifacts import TemporaryFileWriter, temporary_file_writer
from scripts.models import LooprError
from scripts.process import CommandRunner

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_temporary_file_writer_cleans_up_after_success() -> None:
    """The command-owned temporary directory is removed on success."""
    observed: list[Path] = []

    with temporary_file_writer(CommandRunner(), prefix="loopr-test-") as writer:
        observed.append(writer.root)
        writer.text("input.txt", "temporary")
        assert writer.root.is_dir()

    assert observed
    assert not observed[0].exists()


def test_temporary_file_writer_cleans_up_after_error() -> None:
    """The command-owned temporary directory is removed on ordinary errors."""
    observed: list[Path] = []

    def fail_inside_context() -> None:
        with temporary_file_writer(
            CommandRunner(),
            prefix="loopr-test-",
        ) as writer:
            observed.append(writer.root)
            message = "stop"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        fail_inside_context()

    assert observed
    assert not observed[0].exists()


def test_temporary_file_writer_fails_closed_on_cleanup_error(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Cleanup errors become command failures before callers can write."""

    class FailingTemporaryDirectory:
        def __init__(self, **_: object) -> None:
            self.name = str(tmp_path / "oracle-temp")
            Path(self.name).mkdir(mode=0o700)

        @staticmethod
        def cleanup() -> None:
            message = "cleanup failed"
            raise OSError(message)

    mocker.patch.object(
        artifacts_module.tempfile,
        "TemporaryDirectory",
        FailingTemporaryDirectory,
    )

    def fail_cleanup() -> None:
        with temporary_file_writer(CommandRunner(), prefix="loopr-test-"):
            pass

    with pytest.raises(LooprError) as captured:
        fail_cleanup()

    assert captured.value.category == "temporary_files"


def test_json_redacts_secrets_before_serialization(tmp_path: Path) -> None:
    """Secrets are redacted from JSON values and keys before escaping."""
    secret = 'abc"def\\ghi'
    writer = TemporaryFileWriter(
        tmp_path / "oracle",
        CommandRunner({"SSH_PRIVATE_KEY": secret}),
    )

    path = writer.json("snapshot.json", {secret: [secret]})
    written = path.read_text(encoding="utf-8")

    assert secret not in written
    assert json.dumps(secret)[1:-1] not in written
    assert written.count("[REDACTED]") == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writer_rejects_non_private_root(tmp_path: Path) -> None:
    """A temporary root writable by group or others is rejected."""
    root = tmp_path / "oracle"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(LooprError) as captured:
        TemporaryFileWriter(root, CommandRunner())

    assert captured.value.category == "temporary_files"


def test_writer_rejects_path_escape(tmp_path: Path) -> None:
    """Relative temporary names cannot escape the private root."""
    writer = TemporaryFileWriter(tmp_path / "oracle", CommandRunner())

    with pytest.raises(LooprError) as captured:
        writer.text("../outside.txt", "unsafe")

    assert captured.value.category == "temporary_files"
