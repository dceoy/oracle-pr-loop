"""Tests for Oracle's pinned JSON5 Unicode identifier tables."""

from __future__ import annotations

from scripts.json5_unicode import is_identifier_continue, is_identifier_start


def test_identifier_start_matches_pinned_table_boundaries() -> None:
    """ASCII, legacy non-BMP, and newer Unicode characters follow Oracle."""
    assert is_identifier_start("A")
    assert is_identifier_start("é")
    assert is_identifier_start(chr(0x10000))
    assert not is_identifier_start(chr(0x10D00))


def test_identifier_continue_supports_ecmascript_extras() -> None:
    """Digits and joiners are accepted only as continuation characters."""
    assert is_identifier_continue("0")
    assert is_identifier_continue("\u200c")
    assert is_identifier_continue("\u0300")
    assert not is_identifier_start("0")
