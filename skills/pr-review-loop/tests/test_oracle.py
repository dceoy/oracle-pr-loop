"""Oracle schema, evidence-bundle, and transport-boundary tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import pytest
from scripts.artifacts import TemporaryFileWriter
from scripts.models import (
    EXIT_ORACLE,
    EXIT_PRECONDITION,
    BlockingFinding,
    FindingLocation,
    IssueSnapshot,
    JsonObject,
    JsonValue,
    OracleReview,
    PullRequest,
    ReviewLoopError,
)
from scripts.oracle import (
    BOOTSTRAP_PROMPT,
    MAX_BOOTSTRAP_ATTACHMENTS,
    MAX_ORACLE_ATTACHMENTS,
    MAX_REVIEW_BODY_BYTES,
    PROMPT,
    REMOTE_BUSY_MAX_RETRIES,
    _is_remote_busy,
    _remote_busy_delay,
    build_bootstrap_bundle,
    build_review_bundle,
    invoke_oracle,
    parse_bootstrap,
    parse_review,
)
from scripts.process import CommandError, CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from scripts.github import GitHubClient, IssueClient

SHA_A = "a" * 40
SHA_B = "b" * 40


def sample_pr() -> PullRequest:
    return PullRequest(
        repository="owner/repository",
        number=21,
        url="https://github.com/owner/repository/pull/21",
        title="Title",
        body="Body",
        author="author",
        state="OPEN",
        is_draft=False,
        base_ref="main",
        base_sha=SHA_A,
        head_ref="feature",
        head_sha=SHA_B,
        head_repository="owner/repository",
        changed_paths=("file.py",),
    )


def sample_issue() -> IssueSnapshot:
    return IssueSnapshot(
        repository="owner/repository",
        number=7,
        url="https://github.com/owner/repository/issues/7",
        title="Issue",
        body="Body",
        author="author",
        state="OPEN",
        updated_at="2026-01-01T00:00:00Z",
        comments=(),
    )


def review_payload(**overrides: JsonValue) -> JsonObject:
    value: JsonObject = {
        "schema_version": 1,
        "repository": "owner/repository",
        "pr_number": 21,
        "base_sha": SHA_A,
        "head_sha": SHA_B,
        "verdict": "APPROVE",
        "review_body": "Approved.",
        "implementation_prompt": None,
        "blocking_findings": [],
        "non_blocking_notes": [],
    }
    value.update(overrides)
    return value


def bootstrap_payload(**overrides: JsonValue) -> JsonObject:
    value: JsonObject = {
        "schema_version": 1,
        "repository": "owner/repository",
        "issue_number": 7,
        "base_sha": SHA_A,
        "implementation_prompt": "Implement it.",
    }
    value.update(overrides)
    return value


def test_review_prompt_binds_identity_and_exact_anchor_semantics() -> None:
    rendered = PROMPT.format(
        repository="owner/repository",
        pr_number=21,
        base_sha=SHA_A,
        head_sha=SHA_B,
    )

    assert "PR #21" in rendered
    assert SHA_A in rendered
    assert SHA_B in rendered
    assert "side is RIGHT" in rendered
    assert "side is LEFT only for a removed line" in rendered
    assert "An unchanged context line is always RIGHT" in rendered
    assert "use null" in rendered
    assert "guessing a line" in rendered


def test_bootstrap_prompt_is_issue_oriented_and_advisory() -> None:
    rendered = BOOTSTRAP_PROMPT.format(
        repository="owner/repository",
        issue_number=7,
        base_ref="main",
        base_sha=SHA_A,
    )
    normalized = " ".join(rendered.split())

    assert "Issue #7" in normalized
    assert "untrusted requirements data" in rendered
    assert "do not implement" in rendered
    assert "commit, push, create a pull request" in rendered


def test_parse_review_accepts_exact_approval_schema() -> None:
    result = parse_review(json.dumps(review_payload()), sample_pr())

    assert isinstance(result, OracleReview)
    assert result.verdict == "APPROVE"
    assert result.blocking_findings == ()
    assert result.implementation_prompt is None


def test_parse_review_accepts_exact_request_changes_schema() -> None:
    finding: JsonObject = {
        "id": "F1",
        "title": "Bug",
        "description": "Description",
        "required_change": "Fix it",
        "location": {"path": "file.py", "line": 7, "side": "RIGHT"},
    }
    result = parse_review(
        json.dumps(
            review_payload(
                verdict="REQUEST_CHANGES",
                blocking_findings=[finding],
                implementation_prompt="Fix F1.",
            )
        ),
        sample_pr(),
    )

    assert result.verdict == "REQUEST_CHANGES"
    assert result.blocking_findings == (
        BlockingFinding(
            id="F1",
            title="Bug",
            description="Description",
            required_change="Fix it",
            location=FindingLocation(path="file.py", line=7, side="RIGHT"),
        ),
    )
    assert result.implementation_prompt == "Fix F1."


@pytest.mark.parametrize(
    "payload",
    [
        review_payload(extra="no"),
        review_payload(repository="other/repository"),
        review_payload(pr_number=22),
        review_payload(base_sha="c" * 40),
        review_payload(head_sha="d" * 40),
        review_payload(verdict="COMMENT"),
        review_payload(verdict="APPROVE", implementation_prompt="unexpected"),
        review_payload(
            verdict="REQUEST_CHANGES",
            implementation_prompt="Fix it",
            blocking_findings=[],
        ),
    ],
)
def test_parse_review_fails_closed_on_schema_or_identity_drift(
    payload: JsonObject,
) -> None:
    with pytest.raises(ReviewLoopError) as captured:
        parse_review(json.dumps(payload), sample_pr())

    assert captured.value.code == EXIT_ORACLE


@pytest.mark.parametrize(
    "location",
    [
        {"path": "file.py", "line": 0, "side": "RIGHT"},
        {"path": "file.py", "line": True, "side": "RIGHT"},
        {"path": "file.py", "line": 7, "side": "MIDDLE"},
        {"path": "file.py", "line": 7},
        "file.py:7",
    ],
)
def test_parse_review_rejects_malformed_locations(location: object) -> None:
    finding: JsonObject = {
        "id": "F1",
        "title": "Bug",
        "description": "Description",
        "required_change": "Fix it",
        "location": location,  # type: ignore[dict-item]
    }
    payload = review_payload(
        verdict="REQUEST_CHANGES",
        blocking_findings=[finding],
        implementation_prompt="Fix it",
    )

    with pytest.raises(ReviewLoopError) as captured:
        parse_review(json.dumps(payload), sample_pr())

    assert captured.value.category == "oracle_schema"


def test_parse_review_rejects_oversized_body() -> None:
    body = "x" * (MAX_REVIEW_BODY_BYTES + 1)
    with pytest.raises(ReviewLoopError) as captured:
        parse_review(
            json.dumps(review_payload(review_body=body)),
            sample_pr(),
        )

    assert captured.value.category == "oracle_schema"


@pytest.mark.parametrize("text", ["", "[]", "{} trailing", "not-json"])
def test_parse_review_requires_exactly_one_object(text: str) -> None:
    with pytest.raises(ReviewLoopError) as captured:
        parse_review(text, sample_pr())

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


def test_parse_bootstrap_accepts_exact_identity() -> None:
    result = parse_bootstrap(
        json.dumps(bootstrap_payload()),
        sample_issue(),
        SHA_A,
    )

    assert result.repository == "owner/repository"
    assert result.issue_number == 7
    assert result.base_sha == SHA_A
    assert result.implementation_prompt == "Implement it."


@pytest.mark.parametrize(
    "payload",
    [
        bootstrap_payload(extra="no"),
        bootstrap_payload(repository="other/repository"),
        bootstrap_payload(issue_number=8),
        bootstrap_payload(base_sha=SHA_B),
        bootstrap_payload(implementation_prompt=""),
    ],
)
def test_parse_bootstrap_fails_closed_on_invalid_output(payload: JsonObject) -> None:
    with pytest.raises(ReviewLoopError) as captured:
        parse_bootstrap(json.dumps(payload), sample_issue(), SHA_A)

    assert captured.value.code == EXIT_ORACLE


class FakeReviewGitHub:
    """Provide deterministic immutable evidence to bundle tests."""

    @staticmethod
    def patch(_pr: PullRequest, *, max_output: int) -> bytes:
        """Return the frozen pull-request patch.

        Returns:
            The deterministic patch bytes.
        """
        del max_output
        return b"diff --git a/file.py b/file.py\n"

    @staticmethod
    def tracked_paths(_pr: PullRequest) -> tuple[str, ...]:
        """Return the paths tracked by the frozen pull request.

        Returns:
            The deterministic changed-path inventory.
        """
        return ("AGENTS.md", "file.py")

    @staticmethod
    def changed_file_bytes(
        _pr: PullRequest,
        path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Return frozen content for one changed path.

        Returns:
            The path content, or ``None`` when no content is available.
        """
        del max_output
        return {
            "AGENTS.md": b"review rules\n",
            "file.py": b"print('ok')\n",
        }[path]


class FakeIssueClient:
    """Provide deterministic base-tree evidence to bootstrap bundle tests."""

    @staticmethod
    def tracked_paths_at(_sha: str) -> tuple[str, ...]:
        """Return the paths tracked by the frozen base commit.

        Returns:
            The deterministic base-tree inventory.
        """
        return ("AGENTS.md", "README.md")

    @staticmethod
    def blob_bytes_at(
        _sha: str,
        path: str,
        *,
        max_output: int,
    ) -> bytes | None:
        """Return frozen content for one base-tree path.

        Returns:
            The path content from the base tree.
        """
        del max_output
        return b"instructions\n" if path == "AGENTS.md" else b"readme\n"


def test_review_bundle_contains_snapshot_patch_manifest_and_text_evidence(
    tmp_path: Path,
) -> None:
    runner = CommandRunner({"PATH": "/usr/bin"})
    writer = TemporaryFileWriter(tmp_path / "private", runner)

    attachments = build_review_bundle(
        runner,
        cast("GitHubClient", FakeReviewGitHub()),
        writer,
        sample_pr(),
    )

    assert [path.name for path in attachments][:4] == [
        "snapshot.json",
        "patch.diff",
        "changed-paths.txt",
        "bundle-manifest.json",
    ]
    manifest = json.loads((writer.root / "bundle-manifest.json").read_text())
    assert {item["path"] for item in manifest} == {"AGENTS.md", "file.py"}
    assert all(item["attachment"] is not None for item in manifest)


def test_review_bundle_rejects_known_credentials_in_patch(tmp_path: Path) -> None:
    secret = "sensitive-token"
    runner = CommandRunner({"PATH": "/usr/bin", "GH_TOKEN": secret})
    writer = TemporaryFileWriter(tmp_path / "private", runner)

    class SecretPatchGitHub(FakeReviewGitHub):
        """Return a patch containing a known credential."""

        @staticmethod
        def patch(_pr: PullRequest, *, max_output: int) -> bytes:
            del max_output
            return f"diff --git a/file.py b/file.py\n+{secret}\n".encode()

    with pytest.raises(ReviewLoopError) as captured:
        build_review_bundle(
            runner,
            cast("GitHubClient", SecretPatchGitHub()),
            writer,
            sample_pr(),
        )

    assert captured.value.code == EXIT_PRECONDITION
    assert captured.value.category == "bundle"


def test_bootstrap_bundle_only_attaches_instruction_files(tmp_path: Path) -> None:
    runner = CommandRunner({"PATH": "/usr/bin"})
    writer = TemporaryFileWriter(tmp_path / "private", runner)

    attachments = build_bootstrap_bundle(
        runner,
        cast("IssueClient", FakeIssueClient()),
        writer,
        sample_issue(),
        SHA_A,
    )

    assert [path.name for path in attachments][:2] == [
        "issue-snapshot.json",
        "bundle-manifest.json",
    ]
    manifest = json.loads((writer.root / "bundle-manifest.json").read_text())
    assert [item["path"] for item in manifest] == ["AGENTS.md"]


class RecordingOracleRunner(CommandRunner):
    """Capture Oracle argv/environment and optionally inject command failures."""

    def __init__(
        self,
        source_env: Mapping[str, str],
        *,
        output: str = '{"ok":true}',
        failures: list[CommandError] | None = None,
    ) -> None:
        """Initialize the runner with captured output and queued failures."""
        super().__init__(source_env)
        self.output = output
        self.failures = list(failures or [])
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    @override
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = 24 * 1024 * 1024,
        watch_path: Path | None = None,
    ) -> CommandResult:
        del cwd, timeout, input_text, check, max_output, watch_path
        argv = tuple(str(value) for value in args)
        self.calls.append((argv, dict(env)))
        if self.failures:
            raise self.failures.pop(0)
        output_index = argv.index("--write-output") + 1
        Path(argv[output_index]).write_text(self.output, encoding="utf-8")
        return CommandResult(argv, 0, b"", "")


def _writer(tmp_path: Path, runner: CommandRunner) -> TemporaryFileWriter:
    return TemporaryFileWriter(tmp_path / "private", runner)


def test_local_oracle_uses_manual_login_and_private_oracle_home(
    tmp_path: Path,
) -> None:
    runner = RecordingOracleRunner({"PATH": "/usr/bin", "HOME": "/home/test"})
    writer = _writer(tmp_path, runner)
    attachment = writer.text("input.txt", "evidence")

    raw = invoke_oracle(
        runner,
        writer,
        tmp_path,
        None,
        "prompt",
        (attachment,),
        "slug",
        max_attachments=MAX_ORACLE_ATTACHMENTS,
    )

    assert raw == '{"ok":true}'
    argv, env = runner.calls[0]
    assert "--browser-manual-login" in argv
    assert "--remote-token" not in argv
    assert env["HOME"] == "/home/test"
    assert env["ORACLE_HOME_DIR"].startswith(str(writer.root))
    assert env["ORACLE_HOME_DIR"] != "/home/test/.oracle"


def test_remote_oracle_uses_environment_only_and_never_token_argv(
    tmp_path: Path,
) -> None:
    token = "remote-secret-token"
    runner = RecordingOracleRunner({
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "ORACLE_REMOTE_HOST": " 127.0.0.1:9473 ",
        "ORACLE_REMOTE_TOKEN": token,
        "ORACLE_HOME_DIR": "/host/oracle-config",
    })
    writer = _writer(tmp_path, runner)
    attachment = writer.text("input.txt", "evidence")

    invoke_oracle(
        runner,
        writer,
        tmp_path,
        "heavy",
        "prompt",
        (attachment,),
        "slug",
        model="gpt-5.6-sol",
        max_attachments=MAX_ORACLE_ATTACHMENTS,
    )

    argv, env = runner.calls[0]
    assert "--browser-manual-login" not in argv
    assert token not in argv
    assert "--remote-token" not in argv
    assert env["ORACLE_REMOTE_TOKEN"] == token
    assert env["ORACLE_REMOTE_HOST"] == " 127.0.0.1:9473 "
    assert env["ORACLE_HOME_DIR"].startswith(str(writer.root))
    assert env["ORACLE_HOME_DIR"] != "/host/oracle-config"
    assert argv[argv.index("--browser-model-strategy") + 1] == "select"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    effort_index = argv.index("--browser-thinking-time") + 1
    assert argv[effort_index] == "heavy"


def test_blank_remote_host_keeps_local_manual_login(tmp_path: Path) -> None:
    runner = RecordingOracleRunner({
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "ORACLE_REMOTE_HOST": "\ufeff  ",
    })
    writer = _writer(tmp_path, runner)

    invoke_oracle(
        runner,
        writer,
        tmp_path,
        None,
        "prompt",
        (),
        "slug",
        max_attachments=MAX_ORACLE_ATTACHMENTS,
    )

    assert "--browser-manual-login" in runner.calls[0][0]


def test_remote_token_is_rejected_from_attachment_before_oracle_runs(
    tmp_path: Path,
) -> None:
    token = "remote-secret-token"
    runner = RecordingOracleRunner({
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "ORACLE_REMOTE_HOST": "127.0.0.1:9473",
        "ORACLE_REMOTE_TOKEN": token,
    })
    writer = _writer(tmp_path, runner)
    attachment = writer.root / "untrusted.txt"
    attachment.write_text(f"evidence {token}\n", encoding="utf-8")

    with pytest.raises(ReviewLoopError) as captured:
        invoke_oracle(
            runner,
            writer,
            tmp_path,
            None,
            "prompt",
            (attachment,),
            "slug",
            max_attachments=MAX_ORACLE_ATTACHMENTS,
        )

    assert captured.value.category == "bundle"
    assert runner.calls == []


def test_oracle_output_containing_remote_token_fails_closed(
    tmp_path: Path,
) -> None:
    token = "remote-secret-token"
    runner = RecordingOracleRunner(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "ORACLE_REMOTE_HOST": "127.0.0.1:9473",
            "ORACLE_REMOTE_TOKEN": token,
        },
        output=f'{{"leak":"{token}"}}',
    )
    writer = _writer(tmp_path, runner)

    with pytest.raises(ReviewLoopError) as captured:
        invoke_oracle(
            runner,
            writer,
            tmp_path,
            None,
            "prompt",
            (),
            "slug",
            max_attachments=MAX_ORACLE_ATTACHMENTS,
        )

    assert captured.value.code == EXIT_ORACLE
    assert token not in str(captured.value)


def test_oracle_attachment_count_is_bounded(tmp_path: Path) -> None:
    runner = RecordingOracleRunner({"PATH": "/usr/bin", "HOME": "/home/test"})
    writer = _writer(tmp_path, runner)
    attachments = tuple(
        writer.text(f"{index}.txt", "x")
        for index in range(MAX_BOOTSTRAP_ATTACHMENTS + 1)
    )

    with pytest.raises(ReviewLoopError) as captured:
        invoke_oracle(
            runner,
            writer,
            tmp_path,
            None,
            "prompt",
            attachments,
            "slug",
            max_attachments=MAX_BOOTSTRAP_ATTACHMENTS,
        )

    assert captured.value.category == "bundle"


def test_remote_busy_detection_requires_routing_and_terminal_error() -> None:
    retryable = CommandError(
        "busy",
        returncode=1,
        stdout=("Routing browser automation to remote host http://host\nERROR: busy\n"),
    )
    local = CommandError("busy", returncode=1, stdout="ERROR: busy\n")
    ambiguous = CommandError(
        "busy",
        returncode=1,
        stdout=(
            "Routing browser automation to remote host http://host\nERROR: busy later\n"
        ),
    )

    assert _is_remote_busy(retryable)
    assert not _is_remote_busy(local)
    assert not _is_remote_busy(ambiguous)


def test_remote_busy_delay_is_bounded_exponential_with_jitter() -> None:
    assert _remote_busy_delay(1, lambda: 0.0) == pytest.approx(0.75)
    assert _remote_busy_delay(1, lambda: 1.0) == pytest.approx(1.0)
    assert _remote_busy_delay(10, lambda: 1.0) == pytest.approx(30.0)


def test_remote_busy_retries_are_bounded(tmp_path: Path) -> None:
    busy = CommandError(
        "busy",
        returncode=1,
        stdout=("Routing browser automation to remote host http://host\nERROR: busy\n"),
    )
    runner = RecordingOracleRunner(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "ORACLE_REMOTE_HOST": "host:9473",
        },
        failures=[busy] * (REMOTE_BUSY_MAX_RETRIES + 1),
    )
    writer = _writer(tmp_path, runner)
    sleeps: list[float] = []

    with pytest.raises(ReviewLoopError) as captured:
        invoke_oracle(
            runner,
            writer,
            tmp_path,
            None,
            "prompt",
            (),
            "slug",
            max_attachments=MAX_ORACLE_ATTACHMENTS,
            _sleep=sleeps.append,
            _random_value=lambda: 1.0,
        )

    assert captured.value.code == EXIT_ORACLE
    assert len(runner.calls) == REMOTE_BUSY_MAX_RETRIES + 1
    assert len(sleeps) == REMOTE_BUSY_MAX_RETRIES
