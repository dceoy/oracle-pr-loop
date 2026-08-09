"""Cross-platform bounded subprocess execution."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- this module is the sole, argv-validated, shell=False subprocess boundary
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn, cast

from .models import EXIT_PRECONDITION, LooprError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .models import JsonObject

MAX_OUTPUT = 24 * 1024 * 1024
MAX_INPUT = 4 * 1024 * 1024
MAX_STDERR = 1024 * 1024
POLL_INTERVAL_SECONDS = 0.01
TERMINATION_GRACE_SECONDS = 2
MIN_SECRET_LENGTH = 4


def normalize_oracle_remote_value(value: object) -> str | None:
    """Trim a remote-transport value the way Oracle's own resolver does.

    Returns:
        The stripped value, or None if it is not a non-blank string.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _skip_json5_line_comment(text: str, start: int, length: int) -> tuple[int, bool]:
    r"""Return the index just past a `//` line comment starting at `start`.

    JSON5's `LineTerminator` -- `\r`, `\n`, U+2028, or U+2029 -- ends the
    comment. Only `\r`/`\n` are valid JSON whitespace, so a consumed
    U+2028/U+2029 terminator must not reach `json.loads` verbatim; the
    caller substitutes a JSON-legal separator for it instead.

    Returns:
        The index just past the comment, plus past its terminator when
        one was found before the text ended, and whether such a
        terminator was found.
    """
    index = start + 2
    while index < length and text[index] not in "\r\n\u2028\u2029":
        index += 1
    if index < length:
        return index + 1, True
    return index, False


def _skip_json5_block_comment(text: str, start: int, length: int) -> int:
    """Return the index just past a `/* */` block comment starting at `start`.

    Returns:
        The index of the first character after the comment's closing `*/`.

    Raises:
        ValueError: text ends before the comment is closed, which Oracle's
            own `JSON5.parse` also rejects rather than tolerating.
    """
    index = start + 2
    while index < length and text[index : index + 2] != "*/":
        index += 1
    if index >= length:
        message = "Unterminated JSON5 block comment."
        raise ValueError(message)
    return index + 2


def _strip_json5_comments(text: str) -> str:
    r"""Remove `//`/`/* */` comments and normalize JSON5-only whitespace.

    Both `"`- and `'`-delimited strings (JSON5 permits single-quoted
    strings) are honored as literal spans, so a `//`/`/*` inside either
    quote style is not mistaken for the start of a comment. U+2028/U+2029
    are also normalized to a plain space outside strings: JSON5's
    `WhiteSpace` includes them (as ordinary separators and as the
    `LineTerminator` a `//` comment ends at), but `json.loads` only
    accepts `\t`/`\n`/`\r`/space, so either character reaching it verbatim
    would fail a config Oracle itself parses.

    Returns:
        text with every `//` comment removed, every `/* */` block comment
        replaced by a single space (so a block comment can never splice
        two tokens, e.g. an unquoted key, together the way JSON5's own
        tokenizer -- which treats comments as trivia between tokens, not
        as zero-width -- never would), and every stray U+2028/U+2029
        outside a string replaced by a single space.

    Raises:
        ValueError: text ends inside an unterminated `'`/`"`-delimited
            string or an unterminated `/* */` block comment. Silently
            tolerating either would let this module accept a file that
            Oracle's own `JSON5.parse` rejects outright.
    """
    result: list[str] = []
    length = len(text)
    index = 0
    string_quote: str | None = None
    while index < length:
        char = text[index]
        if string_quote is not None:
            result.append(char)
            if char == "\\" and index + 1 < length:
                result.append(text[index + 1])
                index += 2
                continue
            if char == string_quote:
                string_quote = None
            index += 1
            continue
        if char in "\"'":
            string_quote = char
            result.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index, terminated = _skip_json5_line_comment(text, index, length)
            if terminated:
                result.append(" ")
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index = _skip_json5_block_comment(text, index, length)
            result.append(" ")
            continue
        # JSON5 `WhiteSpace` includes U+2028/U+2029 outside strings, but
        # `json.loads` only accepts `\t\n\r `, so a config using either as
        # ordinary inter-token whitespace (not just as a comment terminator,
        # handled above) must still be normalized to plain-space here.
        result.append(" " if char in "\u2028\u2029" else char)
        index += 1
    if string_quote is not None:
        message = "Unterminated JSON5 string literal."
        raise ValueError(message)
    return "".join(result)


def _requote_json5_single_quoted_body(text: str, start: int) -> tuple[list[str], int]:
    r"""Convert one `'`-delimited string body, starting just past the `'`.

    An unescaped `"` inside the string is escaped, an escaped `\'` is
    unescaped (it needs no escaping once re-delimited by double quotes),
    and every other escape sequence (`\\`, `\n`, `\uXXXX`, ...) is passed
    through unchanged since JSON already supports it. Callers only ever
    reach this on text `_strip_json5_comments` has already accepted, which
    guarantees the `'`-delimited string starting at `start` is terminated.

    Returns:
        The double-quoted-string characters (including both delimiters)
        and the index just past the closing `'`.
    """
    length = len(text)
    index = start
    body: list[str] = ['"']
    while index < length and text[index] != "'":
        next_char = text[index + 1] if index + 1 < length else ""
        if text[index] == "\\" and next_char == "'":
            body.append("'")
            index += 2
        elif text[index] == "\\" and index + 1 < length:
            body.extend((text[index], next_char))
            index += 2
        elif text[index] == '"':
            body.append('\\"')
            index += 1
        else:
            body.append(text[index])
            index += 1
    body.append('"')
    return body, index + 1


def _convert_json5_single_quoted_strings(text: str) -> str:
    """Rewrite `'`-delimited JSON5 strings as `"`-delimited JSON strings.

    Must run on comment-free text.

    Returns:
        text with every single-quoted string rewritten as a double-quoted
        one; double-quoted strings are left untouched.
    """
    result: list[str] = []
    length = len(text)
    index = 0
    in_double_string = False
    while index < length:
        char = text[index]
        if in_double_string:
            result.append(char)
            if char == "\\" and index + 1 < length:
                result.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_double_string = False
            index += 1
            continue
        if char == '"':
            in_double_string = True
            result.append(char)
            index += 1
            continue
        if char == "'":
            body, index = _requote_json5_single_quoted_body(text, index + 1)
            result.extend(body)
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _is_json5_identifier_start(char: str) -> bool:
    """Return whether `char` can start a bare JSON5 identifier key."""
    return char in "_$" or char.isalpha()


def _is_json5_identifier_continue(char: str) -> bool:
    """Return whether `char` can continue a bare JSON5 identifier key."""
    return _is_json5_identifier_start(char) or char.isdigit()


def _quote_json5_unquoted_keys(text: str) -> str:
    """Wrap a bare JSON5 identifier object key in double quotes.

    Must run after single-quoted strings have already been converted to
    double-quoted ones, so only double-quoted spans need to be treated as
    literal. An identifier is only treated as a key -- as opposed to a
    bare value such as `true`/`false`/`null` -- when it is immediately
    followed (ignoring whitespace) by `:`.

    Returns:
        text with every such bare key wrapped in double quotes.
    """
    result: list[str] = []
    length = len(text)
    index = 0
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if char == "\\" and index + 1 < length:
                result.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if _is_json5_identifier_start(char):
            start = index
            index += 1
            while index < length and _is_json5_identifier_continue(text[index]):
                index += 1
            identifier = text[start:index]
            lookahead = index
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] == ":":
                result.extend(('"', identifier, '"'))
            else:
                result.append(identifier)
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _strip_json_trailing_commas(text: str) -> str:
    """Remove commas outside strings that only precede a closing `}`/`]`.

    Must run on comment-free text, so a plain whitespace lookahead is
    enough to find the next structural character.

    Returns:
        text with every such trailing comma removed.
    """
    result: list[str] = []
    length = len(text)
    index = 0
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if char == "\\" and index + 1 < length:
                result.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _loads_json5_subset(text: str) -> object:
    """Parse JSON, tolerating a subset of Oracle's JSON5 config syntax.

    Oracle parses its config file as JSON5. This project has no JSON5
    dependency, so this only supports the JSON5 features Oracle's own
    documented config examples rely on -- `//`/`/* */` comments, trailing
    commas, unquoted identifier keys, and single-quoted strings -- via a
    sequence of text-level rewrites before delegating to `json.loads`. A
    config using other JSON5-only syntax (a leading `+` on a number,
    hexadecimal literals, `Infinity`/`NaN`, ...), or one whose own
    comment/string syntax is malformed (an unterminated `/* */` comment or
    `'`/`"`-delimited string), still fails to parse here and is treated as
    unparseable, raising `ValueError` (propagated from `json.loads` or
    from `_strip_json5_comments`) rather than silently yielding an
    incomplete or truncated result.

    Returns:
        The parsed JSON value.
    """
    without_comments = _strip_json5_comments(text)
    with_double_quoted_strings = _convert_json5_single_quoted_strings(without_comments)
    with_quoted_keys = _quote_json5_unquoted_keys(with_double_quoted_strings)
    return json.loads(_strip_json_trailing_commas(with_quoted_keys))


_JSON5_STRING_ESCAPE = re.compile(
    r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\\r\n|\\(.)", re.DOTALL
)


def _decode_json5_escapes(text: str) -> str:
    r"""Resolve JSON5 string/identifier escapes so an escaped spelling is visible.

    JSON5 object keys may be ECMAScript `IdentifierName`s, which can spell
    any character via a `\uXXXX` escape, code point `0x0048` being `H`; a
    key spelled `remote`, backslash, `u0048ost` therefore names
    `remoteHost`. A quoted JSON5 string -- including a quoted key -- has a
    wider `EscapeSequence` grammar: `\xHH` hex escapes, a line continuation
    (a backslash immediately followed by a line terminator, which resolves
    to nothing), and a `NonEscapeCharacter` escape, where a backslash
    before any other character resolves to that character verbatim (so
    `"remote\Host"` also spells `remoteHost`). Enumerating only some of
    these escape forms leaves the others as a bypass, so every backslash
    escape is resolved rather than only `\uXXXX`/`\xHH`. This is
    deliberately over-inclusive: it also decodes escapes that happen to
    appear inside comments or ordinary string bodies, but an extra match
    there only makes a fail-closed check that relies on this function
    trigger more often, not less.

    Returns:
        text with every backslash escape resolved to the character(s) it
        encodes.
    """

    def _decode(match: re.Match[str]) -> str:
        unicode_digits, hex_digits, other = match.groups()
        if unicode_digits or hex_digits:
            return chr(int(unicode_digits or hex_digits, 16))
        if other is None:
            # A bare `\` followed by CRLF: JSON5's line-continuation form.
            return ""
        # A line terminator after `\` (LF, CR, U+2028, U+2029) is also a
        # line continuation and resolves to nothing; any other character
        # is a `NonEscapeCharacter` escape and resolves to itself.
        return "" if other in "\r\n\u2028\u2029" else other

    return _JSON5_STRING_ESCAPE.sub(_decode, text)


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
            if key != "ORACLE_REMOTE_TOKEN"
            and value
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
        # Registered from its normalized form only: Oracle's own resolver
        # trims this value before use, so a whitespace-only token
        # authenticates as no token at all and must not become a
        # registered secret (e.g. redacting every run of matching
        # whitespace in Oracle's output).
        normalized_remote_token = normalize_oracle_remote_value(
            self.source_env.get("ORACLE_REMOTE_TOKEN")
        )
        if (
            normalized_remote_token
            and len(normalized_remote_token) >= MIN_SECRET_LENGTH
        ):
            self.secrets.add(normalized_remote_token)
        self._oracle_config_remote_error: LooprError | None = None
        self._oracle_config_remote_host: str | None = None
        try:
            config_remote_host, config_remote_token = self._read_oracle_config_remote()
        except LooprError as exc:
            # Defer raising to first access of the `oracle_config_remote_host`
            # property (the Oracle-only call path), so a config file Oracle
            # itself accepts (JSON5) but this module cannot parse does not
            # break commands, such as `submit`, that never invoke Oracle.
            self._oracle_config_remote_error = exc
        else:
            self._oracle_config_remote_host = config_remote_host
            if config_remote_token and len(config_remote_token) >= MIN_SECRET_LENGTH:
                self.secrets.add(config_remote_token)

    @property
    def oracle_config_remote_host(self) -> str | None:
        """Oracle's config-declared `browser.remoteHost`, or None if unset.

        Deferred from construction to this first access, on the Oracle-only
        call path, so a config file this module cannot parse never breaks
        commands that never invoke Oracle.
        """
        if self._oracle_config_remote_error is not None:
            raise self._oracle_config_remote_error
        return self._oracle_config_remote_host

    def _read_oracle_config_remote(self) -> tuple[str | None, str | None]:
        r"""Read Oracle's own remote-transport fields from its config file.

        Oracle resolves `browser.remoteHost`/`browser.remoteToken` from its
        config file (`$ORACLE_HOME_DIR/config.json`, or `~/.oracle/config.json`
        when unset) ahead of `ORACLE_REMOTE_HOST`/`ORACLE_REMOTE_TOKEN`, so
        this module must know the config-declared values too rather than
        trusting its own env export alone. Values are trimmed the way
        Oracle's own `resolveRemoteServiceConfig()` trims them, so a
        whitespace-padded config value cannot desync from what Oracle
        actually uses for transport selection and secret redaction.

        Returns:
            The config-declared (remote host, remote token); each is None if
            unset or blank, or if the config file is absent, unreadable, or
            unparseable while spelling out neither field name, directly or
            via any backslash-escaped spelling.

        Raises:
            LooprError: the config file cannot be parsed and spells out
                `remoteHost` or `remoteToken`, directly or via any
                backslash-escaped spelling, so a config-declared remote
                host or token cannot be ruled out.
        """
        oracle_home_dir = self.source_env.get("ORACLE_HOME_DIR")
        if oracle_home_dir:
            config_path = Path(oracle_home_dir) / "config.json"
        else:
            home = self.source_env.get("HOME")
            if not home:
                return (None, None)
            config_path = Path(home) / ".oracle" / "config.json"
        try:
            raw_text = config_path.read_text(encoding="utf-8")
        except OSError:
            return (None, None)
        try:
            raw_config: object = _loads_json5_subset(raw_text)
        except ValueError as exc:
            spelled_out = _decode_json5_escapes(raw_text)
            if "remoteHost" not in spelled_out and "remoteToken" not in spelled_out:
                # Oracle's JSON5 syntax (unquoted keys, single-quoted
                # strings, ...) beyond comments/trailing commas is not
                # supported here, but neither field spelling -- after
                # resolving every backslash escape -- appears anywhere in
                # the file, so it cannot declare either one; a purely
                # local-settings config must not block Oracle.
                return (None, None)
            message = (
                f"Oracle's config file at {config_path} could not be parsed "
                "even after tolerating comments, trailing commas, unquoted "
                "keys, and single-quoted strings (Oracle's own config format "
                "is JSON5, which also allows syntax such as a leading '+' on "
                "a number, hexadecimal literals, or 'Infinity'/'NaN' that "
                "this module does not support), and it spells out "
                "'remoteHost' or 'remoteToken', including via a "
                "backslash-escaped spelling; a config-declared "
                "browser.remoteHost or browser.remoteToken cannot be ruled "
                "out, so refusing to proceed. Convert the config to plain "
                "JSON or remove the config-backed remote-transport fields."
            )
            raise LooprError(EXIT_PRECONDITION, "bundle", message) from exc
        if not isinstance(raw_config, dict):
            return (None, None)
        config = cast("JsonObject", raw_config)
        browser_config = config.get("browser")
        if not isinstance(browser_config, dict):
            return (None, None)
        host = browser_config.get("remoteHost")
        token = browser_config.get("remoteToken")
        return (
            normalize_oracle_remote_value(host),
            normalize_oracle_remote_value(token),
        )

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
