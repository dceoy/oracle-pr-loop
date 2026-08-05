"""Private deterministic artifact writes."""

from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from .models import EXIT_PRECONDITION, JsonValue, LooprError

if TYPE_CHECKING:
    from .process import CommandRunner


class ArtifactWriter:
    """Write redacted artifacts atomically into a private real directory."""

    def __init__(self, root: Path, runner: CommandRunner) -> None:
        """Create and validate the private artifact root."""
        self.root = root.resolve()
        self.runner = runner
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.root.lstat()
        except OSError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "artifacts",
                "failed to create or inspect the artifact directory",
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or self.root.is_symlink()
            or metadata.st_mode & 0o077
        ):
            raise LooprError(
                EXIT_PRECONDITION,
                "artifacts",
                "artifact directory must be a private real directory",
            )

    def _path(self, relative: str) -> Path:
        """Resolve an artifact path without permitting root escape."""
        path = self.root / relative
        try:
            path.parent.resolve().relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "artifacts",
                "artifact path escaped the private root",
            ) from exc
        return path

    def text(self, relative: str, value: str) -> Path:
        """Atomically write redacted UTF-8 text with mode 0600."""
        path = self._path(relative)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            safe = self.runner.redact(value)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(safe)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError as exc:
            raise LooprError(
                EXIT_PRECONDITION,
                "artifacts",
                "failed to write a private artifact",
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return path

    def json(self, relative: str, value: JsonValue) -> Path:
        """Write canonical indented JSON as a private artifact."""
        serialized = json.dumps(
            self._redact(value),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        return self.text(relative, f"{serialized}\n")

    def _redact(self, value: JsonValue) -> JsonValue:
        """Recursively redact secrets from strings before JSON escaping.

        `text()` redacts the fully serialized JSON string, but `json.dumps()`
        escapes quotes, backslashes, and control characters first, so a
        secret containing one of those characters no longer matches the
        known secret value by the time `text()` scans it. Redacting each
        string leaf before serialization catches it while it is still exact.
        """
        if isinstance(value, str):
            return self.runner.redact(value)
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value
