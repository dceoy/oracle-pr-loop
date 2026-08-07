"""Tests for private audit artifact persistence."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from scripts.artifacts import ArtifactWriter
from scripts.models import LooprError
from scripts.process import CommandRunner


def test_json_redacts_secrets_before_serialization(tmp_path: Path) -> None:
    """Secrets are redacted from JSON values and keys before escaping."""
    secret = 'abc"def\\ghi'
    writer = ArtifactWriter(
        tmp_path / "artifacts",
        CommandRunner({"SSH_PRIVATE_KEY": secret}),
    )

    path = writer.json("snapshot.json", {secret: [secret]})
    written = path.read_text(encoding="utf-8")

    assert secret not in written
    assert json.dumps(secret)[1:-1] not in written
    assert written.count("[REDACTED]") == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writer_rejects_non_private_root(tmp_path: Path) -> None:
    """An artifact root writable by group or others is rejected."""
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(LooprError) as captured:
        ArtifactWriter(root, CommandRunner())

    assert captured.value.category == "artifacts"


def test_writer_rejects_path_escape(tmp_path: Path) -> None:
    """Relative artifact names cannot escape the private root."""
    writer = ArtifactWriter(tmp_path / "artifacts", CommandRunner())

    with pytest.raises(LooprError) as captured:
        writer.text("../outside.txt", "unsafe")

    assert captured.value.category == "artifacts"
