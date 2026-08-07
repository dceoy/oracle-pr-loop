"""Vendor-neutral pull-request review skill implementation."""

from __future__ import annotations

import importlib
from types import ModuleType


def __getattr__(name: str) -> ModuleType:
    """Resolve the former test-only CLI import without an eager import cycle."""
    if name == "loopr":
        return importlib.import_module(".cli", __name__)
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
