"""Dependency-free JSON5 parsing for Oracle's user configuration."""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, NoReturn

from .json5_unicode import is_identifier_continue as _is_identifier_continue
from .json5_unicode import is_identifier_start as _is_identifier_start

if TYPE_CHECKING:
    from collections.abc import Collection

_HEX_DIGITS = frozenset(string.hexdigits)
_DECIMAL_DIGITS = frozenset(string.digits)
_JSON5_QUOTES = frozenset("\"'")
_JSON5_SIGN_CHARS = frozenset("+-")
_JSON5_HEX_PREFIX_CHARS = frozenset("xX")
_JSON5_NONZERO_DIGITS = frozenset("123456789")
_JSON5_EXPONENT_CHARS = frozenset("eE")
_JSON5_SPECIAL_NUMBER_STARTS = frozenset("IN")
_JSON5_LINE_TERMINATORS = frozenset("\n\r\u2028\u2029")
_JSON5_SIMPLE_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
_JSON5_WHITESPACE = frozenset(
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_UNICODE_ESCAPE_LENGTH = 4
_HIGH_SURROGATE_START = 0xD800
_HIGH_SURROGATE_END = 0xDBFF
_LOW_SURROGATE_START = 0xDC00
_LOW_SURROGATE_END = 0xDFFF
_SURROGATE_CODE_POINT_BASE = 0x10000


class _JSON5Error(ValueError):
    """An invalid JSON5 document."""


def _is_whitespace(char: str | None) -> bool:
    """Return whether `char` is JSON5 whitespace."""
    return char is not None and char in _JSON5_WHITESPACE


class _Parser:
    """A small recursive-descent parser for the JSON5 grammar."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0
        self.length = len(text)

    def _error(self) -> NoReturn:
        message = f"Invalid JSON5 at offset {self.index}."
        raise _JSON5Error(message)

    def _peek(self) -> str | None:
        if self.index >= self.length:
            return None
        return self.text[self.index]

    def _advance(self) -> str | None:
        char = self._peek()
        if char is not None:
            self.index += 1
        return char

    def _skip_trivia(self) -> None:
        while True:
            while _is_whitespace(self._peek()):
                self.index += 1
            if self.text.startswith("//", self.index):
                self.index += 2
                while (
                    self._peek() is not None
                    and self._peek() not in _JSON5_LINE_TERMINATORS
                ):
                    self.index += 1
                if self._peek() is not None:
                    self.index += 1
                continue
            if self.text.startswith("/*", self.index):
                self.index += 2
                while not self.text.startswith("*/", self.index):
                    if self._peek() is None:
                        self._error()
                    self.index += 1
                self.index += 2
                continue
            return

    def parse(self) -> object:
        """Parse one complete JSON5 value.

        Returns:
            The parsed Python value.
        """
        self._skip_trivia()
        if self._peek() is None:
            self._error()
        value = self._parse_value()
        self._skip_trivia()
        if self._peek() is not None:
            self._error()
        return value

    def _parse_value(self) -> object:
        self._skip_trivia()
        char = self._peek()
        if char is None:
            self._error()
        if char == "{":
            value = self._parse_object()
        elif char == "[":
            value = self._parse_array()
        elif char in _JSON5_QUOTES:
            value = self._parse_string()
        elif char == "t":
            value = self._parse_literal("true", value=True)
        elif char == "f":
            value = self._parse_literal("false", value=False)
        elif char == "n":
            value = self._parse_literal("null", value=None)
        elif (
            char in _JSON5_SIGN_CHARS
            or char in _JSON5_NONZERO_DIGITS
            or char in _JSON5_SPECIAL_NUMBER_STARTS
            or char in {"0", "."}
        ):
            value = self._parse_number()
        else:
            self._error()
        return value

    def _parse_literal(self, literal: str, *, value: object) -> object:
        if not self.text.startswith(literal, self.index):
            self._error()
        self.index += len(literal)
        if _is_identifier_continue(self._peek()) or self._peek() == "\\":
            self._error()
        return value

    def _parse_number(self) -> int | float:
        sign = self._parse_number_sign()
        special = self._parse_special_number(sign)
        if special is not None:
            return special
        if self._peek() == "0":
            return self._parse_zero_number(sign)
        if self._peek() == ".":
            return self._parse_decimal_number(sign, leading_point=True)
        if self._peek() in _JSON5_NONZERO_DIGITS:
            return self._parse_decimal_number(sign, leading_point=False)
        self._error()

    def _parse_number_sign(self) -> int:
        if self._peek() in _JSON5_SIGN_CHARS:
            return -1 if self._advance() == "-" else 1
        return 1

    def _parse_special_number(self, sign: int) -> float | None:
        if self.text.startswith("Infinity", self.index):
            self.index += len("Infinity")
            self._ensure_number_boundary()
            return float("-inf") if sign < 0 else float("inf")
        if self.text.startswith("NaN", self.index):
            self.index += len("NaN")
            self._ensure_number_boundary()
            return float("nan")
        return None

    def _parse_zero_number(self, sign: int) -> int | float:
        self.index += 1
        if self._peek() in _JSON5_HEX_PREFIX_CHARS:
            return self._parse_hex_number(sign)
        if self._peek() in _DECIMAL_DIGITS:
            self._error()
        return self._finish_decimal_number(self.index - 1, sign)

    def _parse_hex_number(self, sign: int) -> int | float:
        self.index += 1
        start = self.index
        while self._peek() in _HEX_DIGITS:
            self.index += 1
        if self.index == start:
            self._error()
        self._ensure_number_boundary()
        value = int(self.text[start : self.index], 16) * sign
        return -0.0 if sign < 0 and value == 0 else value

    def _parse_decimal_number(
        self,
        sign: int,
        *,
        leading_point: bool,
    ) -> int | float:
        start = self.index
        if leading_point:
            self.index += 1
            if self._peek() not in _DECIMAL_DIGITS:
                self._error()
        else:
            self.index += 1
        while self._peek() in _DECIMAL_DIGITS:
            self.index += 1
        return self._finish_decimal_number(start, sign)

    def _finish_decimal_number(self, start: int, sign: int) -> int | float:
        if self._peek() == ".":
            self.index += 1
            while self._peek() in _DECIMAL_DIGITS:
                self.index += 1
        if self._peek() in _JSON5_EXPONENT_CHARS:
            self.index += 1
            if self._peek() in _JSON5_SIGN_CHARS:
                self.index += 1
            exponent_start = self.index
            while self._peek() in _DECIMAL_DIGITS:
                self.index += 1
            if self.index == exponent_start:
                self._error()

        self._ensure_number_boundary()
        number_text = self.text[start : self.index]
        if "." not in number_text and "e" not in number_text.lower():
            try:
                value = int(number_text, 10) * sign
            except ValueError:
                # JavaScript's Number parser returns Infinity for decimal
                # literals too large for a finite numeric representation;
                # do not expose Python's integer-string digit limit instead.
                return float(f"{'-' if sign < 0 else ''}{number_text}")
            return -0.0 if sign < 0 and value == 0 else value
        return float(f"{'-' if sign < 0 else ''}{number_text}")

    def _ensure_number_boundary(self) -> None:
        if _is_identifier_continue(self._peek()) or self._peek() in frozenset(".\\"):
            self._error()

    def _parse_string(self) -> str:
        quote = self._advance()
        if quote is None or quote not in "\"'":
            self._error()
        result: list[str] = []
        while True:
            char = self._advance()
            if char is None:
                self._error()
            if char == quote:
                return "".join(result)
            if char == "\\":
                result.append(self._parse_escape())
                continue
            if char in "\n\r":
                self._error()
            result.append(char)

    def _parse_escape(self) -> str:
        char = self._advance()
        if char is None:
            self._error()
        if char in _JSON5_LINE_TERMINATORS:
            if char == "\r" and self._peek() == "\n":
                self.index += 1
            result = ""
        elif char in _JSON5_SIMPLE_ESCAPES:
            result = _JSON5_SIMPLE_ESCAPES[char]
        elif char == "0":
            if self._peek() in _DECIMAL_DIGITS:
                self._error()
            result = "\0"
        elif char in "123456789":
            self._error()
        elif char == "x":
            result = chr(self._read_hex_digits(2))
        elif char == "u":
            result = self._parse_unicode_escape()
        else:
            result = char
        return result

    def _parse_unicode_escape(self) -> str:
        code_unit = self._read_hex_digits(_UNICODE_ESCAPE_LENGTH)
        if not (
            _HIGH_SURROGATE_START <= code_unit <= _HIGH_SURROGATE_END
            and self.text.startswith("\\u", self.index)
        ):
            return chr(code_unit)

        low_start = self.index + 2
        low_end = low_start + _UNICODE_ESCAPE_LENGTH
        low_digits = self.text[low_start:low_end]
        if len(low_digits) != _UNICODE_ESCAPE_LENGTH or any(
            char not in _HEX_DIGITS for char in low_digits
        ):
            return chr(code_unit)
        low_unit = int(low_digits, 16)
        if not _LOW_SURROGATE_START <= low_unit <= _LOW_SURROGATE_END:
            return chr(code_unit)
        self.index = low_end
        code_point = (
            _SURROGATE_CODE_POINT_BASE
            + ((code_unit - _HIGH_SURROGATE_START) << 10)
            + (low_unit - _LOW_SURROGATE_START)
        )
        return chr(code_point)

    def _read_hex_digits(self, count: int) -> int:
        start = self.index
        for _ in range(count):
            if self._peek() not in _HEX_DIGITS:
                self._error()
            self.index += 1
        return int(self.text[start : self.index], 16)

    def _parse_identifier(self) -> str:
        result: list[str] = []
        first = True
        while True:
            char = self._peek()
            if char == "\\":
                self.index += 1
                value = self._parse_identifier_escape()
            elif char is not None and _is_identifier_continue(char):
                self.index += 1
                value = char
            else:
                break
            if first and not _is_identifier_start(value):
                self._error()
            if not first and not _is_identifier_continue(value):
                self._error()
            result.append(value)
            first = False
        if first:
            self._error()
        return "".join(result)

    def _parse_identifier_escape(self) -> str:
        if self._advance() != "u":
            self._error()
        return chr(self._read_hex_digits(_UNICODE_ESCAPE_LENGTH))

    def _parse_member_name(self) -> str:
        if self._peek() in _JSON5_QUOTES:
            return self._parse_string()
        return self._parse_identifier()

    def _parse_object(self) -> dict[str, object]:
        self._advance()
        result: dict[str, object] = {}
        self._skip_trivia()
        if self._peek() == "}":
            self.index += 1
            return result
        while True:
            key = self._parse_member_name()
            self._skip_trivia()
            if self._advance() != ":":
                self._error()
            result[key] = self._parse_value()
            self._skip_trivia()
            char = self._advance()
            if char == "}":
                return result
            if char != ",":
                self._error()
            self._skip_trivia()
            if self._peek() == "}":
                self.index += 1
                return result

    def _parse_array(self) -> list[object]:
        self._advance()
        result: list[object] = []
        self._skip_trivia()
        if self._peek() == "]":
            self.index += 1
            return result
        while True:
            result.append(self._parse_value())
            self._skip_trivia()
            char = self._advance()
            if char == "]":
                return result
            if char != ",":
                self._error()
            self._skip_trivia()
            if self._peek() == "]":
                self.index += 1
                return result


def loads(text: str) -> object:
    """Parse one complete JSON5 value without a runtime dependency.

    Returns:
        The parsed Python value.

    Raises:
        _JSON5Error: If the document is malformed or exceeds Python's
            recursion capacity.
    """
    parser = _Parser(text)
    try:
        return parser.parse()
    except RecursionError as exc:
        message = f"Invalid JSON5 at offset {parser.index}: nesting too deep."
        raise _JSON5Error(message) from exc


def _skip_trivia_at(text: str, index: int) -> int:
    """Skip JSON5 whitespace/comments without validating the document.

    Returns:
        The first index after the skipped trivia.
    """
    length = len(text)
    while True:
        while index < length and _is_whitespace(text[index]):
            index += 1
        if text.startswith("//", index):
            index += 2
            while index < length and text[index] not in _JSON5_LINE_TERMINATORS:
                index += 1
            if index < length:
                index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return length
            index = end + 2
            continue
        return index


def _scan_string(text: str, index: int) -> tuple[str, int] | None:
    parser = _Parser(text)
    parser.index = index
    try:
        value = parser._parse_string()  # ruff: ignore[private-member-access] -- lexical fallback in this module
    except ValueError:
        return None
    return value, parser.index


def _skip_malformed_string(text: str, index: int) -> int:
    """Skip a malformed quoted span so later member keys remain visible.

    Returns:
        The first index after the closing quote, or the text length when the
        malformed span is unterminated.
    """
    quote = text[index]
    index += 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 1
            if index >= length:
                return length
            if text[index] == "\r":
                index += 1
                if index < length and text[index] == "\n":
                    index += 1
            else:
                index += 1
            continue
        index += 1
        if char == quote:
            return index
    return length


def _scan_identifier(text: str, index: int) -> tuple[str, int] | None:
    parser = _Parser(text)
    parser.index = index
    try:
        value = parser._parse_identifier()  # ruff: ignore[private-member-access] -- lexical fallback in this module
    except ValueError:
        return None
    return value, parser.index


def may_declare_member(text: str, names: Collection[str]) -> bool:
    """Return whether malformed text contains a plausible named member key.

    This conservative lexical scan is used only after `loads` rejects the
    complete document. It ignores comments and string contents that are not
    followed by a colon, allowing malformed local-only settings to preserve
    Oracle's empty-config fallback while refusing to trust an unparsed remote
    declaration.
    """
    wanted = frozenset(names)
    if not wanted:
        return False
    index = 0
    length = len(text)
    while index < length:
        index = _skip_trivia_at(text, index)
        if index >= length:
            return False
        char = text[index]
        if char in "\"'":
            scanned = _scan_string(text, index)
        elif char == "\\" or _is_identifier_start(char):
            scanned = _scan_identifier(text, index)
        else:
            index += 1
            continue
        if scanned is None:
            if char in _JSON5_QUOTES:
                recovered_end = _skip_malformed_string(text, index)
                if recovered_end == length:
                    return False
                index = recovered_end
            else:
                index += 1
            continue
        candidate, end = scanned
        member_end = _skip_trivia_at(text, end)
        if member_end < length and text[member_end] == ":" and candidate in wanted:
            return True
        index = end
    return False
