"""Tests for bounded subprocess execution and redaction."""

from __future__ import annotations

import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- test-controlled process
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from scripts import process as process_module
from scripts.models import LooprError
from scripts.process import CommandError, CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def test_redactor_matches_credential_aliases() -> None:
    """Credential-like environment names register their values as secrets."""
    runner = CommandRunner({
        "SSH_PRIVATE_KEY": "private-key-secret",
        "DB_PASSWD": "passwd-secret-value",
        "AWS_ACCESS_KEY_ID": "access-key-secret",
    })

    for secret in (
        "private-key-secret",
        "passwd-secret-value",
        "access-key-secret",
    ):
        assert runner.contains_secret(secret)
        assert runner.redact(f"leaked: {secret}") == "leaked: [REDACTED]"


def test_gh_env_preserves_ordinary_authentication_sources() -> None:
    """GitHub commands use the ordinary GH token and stored-auth settings."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "GH_TOKEN": "gh-token",
        "GITHUB_TOKEN": "github-token",
        "GH_CONFIG_DIR": "gh-config-dir",
    })

    environment = runner.gh_env()

    assert environment["GH_TOKEN"] == "gh-token"
    assert environment["GITHUB_TOKEN"] == "github-token"
    assert environment["GH_CONFIG_DIR"] == "gh-config-dir"


def test_oracle_env_preserves_remote_transport_configuration() -> None:
    """Oracle receives supported remote settings without putting tokens in argv."""
    runner = CommandRunner({
        "ORACLE_HOME_DIR": "oracle-home",
        "ORACLE_REMOTE_HOST": "oracle.example:9473",
        "ORACLE_REMOTE_TOKEN": "remote-token-value",
    })

    environment = runner.oracle_env()

    assert environment["ORACLE_HOME_DIR"] == "oracle-home"
    assert environment["ORACLE_REMOTE_HOST"] == "oracle.example:9473"
    assert environment["ORACLE_REMOTE_TOKEN"] == "remote-token-value"
    assert runner.redact("remote-token-value") == "[REDACTED]"


def test_oracle_remote_token_is_redacted_as_a_known_secret() -> None:
    """`ORACLE_REMOTE_TOKEN` is covered by the existing credential redaction."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_REMOTE_TOKEN": "remote-secret-token",
    })

    assert runner.contains_secret("remote-secret-token")
    assert runner.redact("token=remote-secret-token") == "token=[REDACTED]"


def test_command_error_keeps_bounded_redacted_completed_output(tmp_path: Path) -> None:
    """Retry classifiers can inspect failed streams without exposing secrets."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "API_TOKEN": "command-secret-value",
    })
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.stdout.write('stdout: command-secret-value'); "
            "sys.stderr.write('stderr: command-secret-value'); "
            "raise SystemExit(7)"
        ),
    ]

    with pytest.raises(CommandError) as captured:
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    error = captured.value
    assert error.returncode == 7
    assert error.stdout == "stdout: [REDACTED]"
    assert error.stderr == "stderr: [REDACTED]"
    assert "command-secret-value" not in str(error)


def test_oracle_remote_token_only_in_config_file_is_still_redacted(
    tmp_path: Path,
) -> None:
    """A token declared only in Oracle's config file is still a known secret."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteToken": "config-file-only-secret-token"}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    config_only_token = "config-file-only-secret-token"

    assert runner.contains_secret(config_only_token)
    assert runner.redact(f"token={config_only_token}") == "token=[REDACTED]"


def test_oracle_remote_token_from_env_is_trimmed_before_registration() -> None:
    """A whitespace-padded env token registers as the trimmed value Oracle uses."""
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_REMOTE_TOKEN": " remote-secret-token ",
    })

    assert runner.contains_secret("remote-secret-token")
    assert runner.redact("token=remote-secret-token") == "token=[REDACTED]"


def test_oracle_remote_token_from_env_whitespace_only_is_not_registered() -> None:
    """A whitespace-only env token is not a secret, matching Oracle's trimming.

    Oracle authenticates with no token in this case, so registering the
    raw whitespace string as a secret would make `redact()` rewrite every
    run of matching whitespace in unrelated output.
    """
    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_REMOTE_TOKEN": "    ",
    })

    assert runner.contains_secret("    ") is False
    assert runner.redact("a    b") == "a    b"


def test_oracle_config_remote_token_is_trimmed_before_registration(
    tmp_path: Path,
) -> None:
    """A whitespace-padded config token registers as the trimmed value Oracle uses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteToken": " config-file-only-secret-token "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.contains_secret("config-file-only-secret-token")
    assert runner.redact("token=config-file-only-secret-token") == "token=[REDACTED]"


def test_oracle_config_remote_host_is_trimmed(tmp_path: Path) -> None:
    """A whitespace-padded config host is trimmed to the value Oracle uses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "  10.0.0.9:9473  "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_remote_host_whitespace_only_is_treated_as_unset(
    tmp_path: Path,
) -> None:
    """A whitespace-only config host is unset, matching Oracle's own trimming."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "   "}}',
        encoding="utf-8",
    )
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None


def test_oracle_config_remote_host_is_none_when_config_file_is_absent(
    tmp_path: Path,
) -> None:
    """A missing Oracle config file leaves the config-backed remote host unset."""
    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None


def test_oracle_home_dir_config_is_read_without_an_extra_oracle_subdirectory(
    tmp_path: Path,
) -> None:
    """`ORACLE_HOME_DIR` points at Oracle's config directory, not its parent."""
    (tmp_path / "config.json").write_text(
        '{"browser": {"remoteHost": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({
        "PATH": os.environ["PATH"],
        "ORACLE_HOME_DIR": str(tmp_path),
    })

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_with_line_comments_and_trailing_commas_is_parsed(
    tmp_path: Path,
) -> None:
    """Oracle's documented JSON5 comment/trailing-comma syntax still parses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{\n  // remote transport\n  "browser": {"remoteHost": "10.0.0.9:9473"},\n}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_with_block_comment_is_parsed(tmp_path: Path) -> None:
    """A JSON5 block comment ahead of a value does not break parsing."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": /* remote */ {"remoteHost": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"


def test_oracle_config_with_unquoted_keys_and_single_quoted_strings_is_parsed(
    tmp_path: Path,
) -> None:
    """Oracle's documented unquoted-key, single-quoted-string style parses."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{ browser: { remoteHost: '127.0.0.1:9473',\n"
        "  remoteToken: 'remote-secret-token' } }",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "127.0.0.1:9473"
    assert runner.contains_secret("remote-secret-token")


def test_oracle_config_single_quoted_string_may_contain_a_double_quote(
    tmp_path: Path,
) -> None:
    """A literal `"` inside a single-quoted value survives requoting."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        """{ browser: { remoteHost: '10.0.0.9:9473', remoteToken: 'say "hi"' } }""",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "10.0.0.9:9473"
    assert runner.contains_secret('say "hi"')


def test_oracle_config_with_unquoted_keys_and_no_remote_fields_is_ignored(
    tmp_path: Path,
) -> None:
    """A local-settings-only config using unquoted keys must not block Oracle."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{browser: {manualLogin: true}}",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None
    assert runner.contains_secret("anything") is False


def test_oracle_config_with_unsupported_json5_syntax_fails_closed_on_access(
    tmp_path: Path,
) -> None:
    """A config using JSON5 syntax beyond this module's subset fails closed."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "10.0.0.9:9473"}, "extra": +5}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_unsupported_json5_syntax_does_not_break_construction(
    tmp_path: Path,
) -> None:
    """Construction must not raise, so commands that skip Oracle stay unaffected."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "10.0.0.9:9473"}, "extra": +5}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.contains_secret("anything") is False
    assert runner.redact("no secrets here") == "no secrets here"


def test_oracle_config_with_unsupported_json5_syntax_and_no_remote_fields_is_ignored(
    tmp_path: Path,
) -> None:
    """A local-settings-only config using exotic JSON5 syntax must not block Oracle."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{browser: {manualLogin: true}, extra: +5}",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None
    assert runner.contains_secret("anything") is False


def test_oracle_config_with_unsupported_json5_syntax_naming_remote_token_fails_closed(
    tmp_path: Path,
) -> None:
    """An unparseable config naming only `remoteToken` still fails closed."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteToken": "config-secret-token"}, "extra": +5}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_comment_stripping_is_string_aware(tmp_path: Path) -> None:
    """A `//` inside a quoted value is not mistaken for a line comment."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{\n  // remote transport\n"
        '  "browser": {"remoteHost": "https://10.0.0.9:9473"},\n}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host == "https://10.0.0.9:9473"


def test_oracle_config_block_comment_inside_a_key_does_not_splice_identifiers(
    tmp_path: Path,
) -> None:
    """A block comment cannot bridge two token halves into one key.

    Oracle's own `JSON5.parse` treats a comment as token-separating
    trivia, so `remote/*x*/Host` is two bare identifiers, not one
    `remoteHost` key, and its config loader rejects the file, falling
    back to an empty config. Silently deleting the comment here would
    instead splice the halves together and see a declared `remoteHost`
    that Oracle itself never loads.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{ browser: { remote/*x*/Host: '10.0.0.9:9473' } }",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None


def test_oracle_config_with_unterminated_block_comment_fails_closed(
    tmp_path: Path,
) -> None:
    """An unterminated block comment must not be silently deleted.

    Oracle's own `JSON5.parse` rejects this file outright, so treating the
    otherwise-complete object ahead of it as the whole config would let
    pr-review-loop see a `remoteHost` that Oracle itself never loads.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{"browser": {"remoteHost": "10.0.0.9:9473"}} /*',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_unterminated_block_comment_and_no_remote_fields_is_ignored(
    tmp_path: Path,
) -> None:
    """An unterminated block comment in a local-only config must not block Oracle."""
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{browser: {manualLogin: true}} /*",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    assert runner.oracle_config_remote_host is None
    assert runner.contains_secret("anything") is False


def test_oracle_config_with_unterminated_string_fails_closed(tmp_path: Path) -> None:
    """An unterminated quoted string must not be silently auto-closed.

    Oracle's own `JSON5.parse` rejects this file outright, so accepting a
    truncated value here would let pr-review-loop see a `remoteHost` that
    Oracle itself never loads.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        "{ browser: { remoteHost: '10.0.0.9:9473 } }",
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_unicode_escaped_key_spelling_fails_closed(
    tmp_path: Path,
) -> None:
    r"""A `\uXXXX`-escaped `IdentifierName` spelling still fails closed.

    Oracle's `JSON5.parse` resolves `remoteHost` to `remoteHost`, so a
    literal-text fallback check that misses the escaped spelling would let
    a config-backed remote host bypass this module's precedence/secret
    checks entirely.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{browser: {remote\\u0048ost: "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_hex_escaped_key_spelling_fails_closed(
    tmp_path: Path,
) -> None:
    r"""A `\xHH`-escaped quoted-string spelling still fails closed.

    Oracle parses this file with the `json5` package, whose string parser
    accepts `\xHH` hex escapes, so `"remote\x48ost"` resolves to
    `remoteHost`. A literal-text fallback check that only decodes
    `\uXXXX` escapes would miss this spelling and let a config-backed
    remote host bypass this module's precedence/secret checks entirely.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{browser: {"remote\\x48ost": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_non_escape_character_key_spelling_fails_closed(
    tmp_path: Path,
) -> None:
    r"""A `NonEscapeCharacter`-escaped quoted-string spelling still fails closed.

    JSON5's string grammar resolves a backslash before any character it
    does not otherwise recognize as an escape to that character verbatim,
    so `"remote\Host"` also spells `remoteHost`. A fallback check that only
    decodes `\uXXXX`/`\xHH` escapes would miss this spelling and let a
    config-backed remote host bypass this module's precedence/secret
    checks entirely.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{browser: {"remote\\Host": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_oracle_config_with_line_continuation_key_spelling_fails_closed(
    tmp_path: Path,
) -> None:
    r"""A line-continuation-escaped quoted-string spelling still fails closed.

    JSON5's string grammar resolves a backslash immediately followed by a
    line terminator to nothing (a line continuation), so a quoted string
    spelling `remote`, backslash, newline, `Host` also spells `remoteHost`.
    A fallback check that does not resolve this escape form would miss
    this spelling and let a config-backed remote host bypass this
    module's precedence/secret checks entirely.
    """
    oracle_dir = tmp_path / ".oracle"
    oracle_dir.mkdir()
    (oracle_dir / "config.json").write_text(
        '{browser: {"remote\\\nHost": "10.0.0.9:9473"}}',
        encoding="utf-8",
    )

    runner = CommandRunner({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})

    with pytest.raises(LooprError, match="could not be parsed"):
        _ = runner.oracle_config_remote_host


def test_runner_rejects_output_overflow(tmp_path: Path) -> None:
    """Output growth past the configured bound terminates the command."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"]

    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            command,
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
        )


def test_runner_rejects_watched_file_overflow(tmp_path: Path) -> None:
    """A watched side-effect file is bounded independently of stdout."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    watch_path = tmp_path / "watched.bin"
    script = (
        "import pathlib, time\n"
        f"pathlib.Path({str(watch_path)!r}).write_bytes(b'x' * 65536)\n"
        "time.sleep(5)\n"
    )

    with pytest.raises(CommandError, match="output exceeded bound"):
        runner.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=runner.base_env(),
            timeout=5,
            max_output=1024,
            watch_path=watch_path,
        )


def test_runner_reaps_child_on_interrupt(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """KeyboardInterrupt still terminates and reaps the direct child."""
    runner = CommandRunner({"PATH": os.environ["PATH"]})
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    pids: list[int] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
                *args,  # type: ignore[arg-type]
                **kwargs,
            ),
        )
        pids.append(proc.pid)
        return proc

    def raise_interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    mocker.patch.object(
        process_module,
        "subprocess",
        SimpleNamespace(
            Popen=recording_popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    mocker.patch.object(
        process_module,
        "time",
        SimpleNamespace(monotonic=process_module.time.monotonic, sleep=raise_interrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(command, cwd=tmp_path, env=runner.base_env(), timeout=5)

    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
