"""Tests for private audit artifact persistence."""

from __future__ import annotations

import datetime as dt
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import artifacts as artifacts_module
from scripts.artifacts import ArtifactWriter, claim_run_directory
from scripts.models import LooprError
from scripts.process import CommandRunner


def test_claim_run_directory_retries_on_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A colliding candidate run directory is retried with a fresh suffix."""

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(artifacts_module.dt, "datetime", _FixedDateTime)
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(
        artifacts_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(tokens)),
    )
    stamp = "20260101T000000Z"
    prefix = "run"
    colliding = tmp_path / "artifacts" / "runs" / f"{prefix}-{stamp}-aaaaaaaa"
    colliding.mkdir(parents=True)

    result = claim_run_directory(tmp_path, Path("artifacts"), prefix)

    assert result.name == f"{prefix}-{stamp}-bbbbbbbb"
    assert result.is_dir()


def test_claim_run_directory_rejects_symlinked_artifacts_component(
    tmp_path: Path,
) -> None:
    """A repository-controlled symlink cannot redirect audit artifacts."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LooprError) as captured:
        claim_run_directory(tmp_path, Path("artifacts"), "run")

    assert captured.value.category == "artifacts"
    assert not list(outside.iterdir())


def test_claim_run_directory_rejects_relative_traversal(tmp_path: Path) -> None:
    """A relative artifact root cannot escape the checkout."""
    with pytest.raises(LooprError) as captured:
        claim_run_directory(tmp_path, Path("../escape"), "run")

    assert captured.value.category == "artifacts"


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
