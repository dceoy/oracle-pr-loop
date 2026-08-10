"""Tests for the dependency-free JSON5 parser."""

from __future__ import annotations

import math

import pytest
from scripts import json5
from scripts.json5 import loads, may_declare_member


def test_loads_supports_documented_json5_features() -> None:
    """The parser accepts syntax used by Oracle's documented examples."""
    value = loads(
        """{
          // comments and unquoted keys
          unquoted: 'and you can quote me on that',
          singleQuotes: 'I can use "double quotes" here',
          hexadecimal: 0xdecaf,
          leadingDecimalPoint: .8675309,
          andTrailing: 8675309.,
          positiveSign: +1,
          trailingComma: 'in objects',
          andIn: ['arrays',],
        }"""
    )

    assert isinstance(value, dict)
    assert value["unquoted"] == "and you can quote me on that"
    assert value["singleQuotes"] == 'I can use "double quotes" here'
    assert value["hexadecimal"] == 0xDECAF
    assert value["leadingDecimalPoint"] == pytest.approx(0.8675309)
    assert value["andTrailing"] == pytest.approx(8675309.0)
    assert value["positiveSign"] == 1
    assert value["andIn"] == ["arrays"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("+5", 5),
        ("-0xC0FFEE", -0xC0FFEE),
        (".5", 0.5),
        ("5.", 5.0),
        ("1e-3", 0.001),
        ("Infinity", float("inf")),
        ("-Infinity", float("-inf")),
    ],
)
def test_loads_supports_json5_numbers(source: str, expected: float | int) -> None:
    """JSON5's extended numeric forms parse."""
    assert loads(source) == expected


def test_loads_supports_nan() -> None:
    """JSON5's NaN literal becomes a Python NaN."""
    value = loads("+NaN")

    assert isinstance(value, float)
    assert math.isnan(value)


def test_loads_supports_decimal_numbers_beyond_python_integer_limit() -> None:
    """Very long JSON5 decimals become numeric Infinity, not parse errors."""
    value = loads("9" * 5000)

    assert value == float("inf")


def test_loads_uses_last_duplicate_member_value() -> None:
    """Object semantics retain the last duplicate value."""
    assert loads("{value: 1, value: 2}") == {"value": 2}


def test_loads_supports_unicode_whitespace_and_identifiers() -> None:
    """JSON5 whitespace and Unicode IdentifierName characters are accepted."""
    value = loads("{\u00a0café: 1,\u2028foo\u0300bar: 2,\ufeff}")

    assert value == {"café": 1, "foo\u0300bar": 2}


def test_loads_decodes_string_escapes_and_line_continuations() -> None:
    """Quoted values decode escapes, non-escapes, and continuations."""
    source = (
        "{escaped: 'A\\x42\\u0043\\q', continued: \"line"
        + chr(92)
        + "\ncontinued\", nul: '\\0'}"
    )

    assert loads(source) == {
        "escaped": "ABCq",
        "continued": "linecontinued",
        "nul": "\0",
    }
    assert loads("'\\uD83D\\uDE00'") == "😀"


@pytest.mark.parametrize(
    "source",
    [
        "",
        "{",
        "{value: 1",
        "{value: 1,,}",
        "[,]",
        "{,}",
        "{value: +}",
        "{value: 0x}",
        "{value: 1e}",
        "{value: 01}",
        "{value: '\\8'}",
        "{value: 'unterminated}",
        "{value: 'line\nbreak'}",
    ],
)
def test_loads_rejects_invalid_json5(source: str) -> None:
    """Malformed or ambiguous documents are never partially interpreted."""
    with pytest.raises(ValueError, match="Invalid JSON5"):
        loads(source)


def test_loads_rejects_identifier_characters_oracle_does_not_accept() -> None:
    """A superscript digit cannot silently become part of an unquoted key."""
    with pytest.raises(ValueError, match="Invalid JSON5"):
        loads("{foo²: 1}")


def test_loads_rejects_identifier_from_newer_unicode_table() -> None:
    """Identifier tables remain pinned to Oracle's JSON5 2.2.3 parser."""
    with pytest.raises(ValueError, match="Invalid JSON5"):
        loads("{" + "\U00010d00" + ": 1}")


def test_loads_converts_recursion_error_to_json5_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python recursion exhaustion remains an ordinary JSON5 parse failure."""

    def raise_recursion_error(_parser: object) -> object:
        raise RecursionError

    monkeypatch.setattr(json5._Parser, "parse", raise_recursion_error)

    with pytest.raises(ValueError, match="Invalid JSON5"):
        loads("0")


def test_may_declare_member_is_string_and_comment_aware() -> None:
    """The malformed fallback finds fields but ignores decoys."""
    assert may_declare_member(
        '{browser: {"remote\\x48ost": "host"}, extra: +5}',
        ("remoteHost", "remoteToken"),
    )
    assert not may_declare_member(
        "{browser: {manualLogin: true}, extra: +5 /* remoteHost docs */}",
        ("remoteHost", "remoteToken"),
    )
    assert not may_declare_member(
        '{browser: {manualLogin: true}, note: "remoteToken"}',
        ("remoteHost", "remoteToken"),
    )


@pytest.mark.parametrize("name", ["remoteHost", "remoteToken"])
def test_may_declare_member_recovers_after_malformed_string(name: str) -> None:
    """A malformed quoted value does not hide a later target member."""
    assert may_declare_member(
        "{bad: '\\8', " + name + ": 'remote-value'}",
        ("remoteHost", "remoteToken"),
    )
