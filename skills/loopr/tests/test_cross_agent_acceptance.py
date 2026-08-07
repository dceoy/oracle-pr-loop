"""Cross-agent acceptance tests for the canonical loopr skill workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from test_review_command import (
    FakeGitHubClient,
    install_orchestration_fakes,
    sample_pr,
)
from test_submit_command import ScenarioRunner, _fixture_repo

from scripts import loopr as cli
from scripts.models import (
    EXIT_ORACLE,
    EXIT_RACE,
    JsonObject,
    LooprError,
    ReviewResult,
    SubmitResult,
)
from scripts.oracle import parse_review
from scripts.process import CommandResult, CommandRunner
from scripts.review import execute_review
from scripts.submit import execute_submit

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_SKILL = REPOSITORY_ROOT / "skills" / "loopr"
CLIENTS = (
    ("Codex CLI", Path(".agents/skills/loopr")),
    ("Claude Code", Path(".claude/skills/loopr")),
    ("Cursor CLI", Path(".agents/skills/loopr")),
)
REVIEW_SUCCESS_KEYS = {
    "schema_version",
    "command",
    "repository",
    "pr_number",
    "base_sha",
    "head_sha",
    "verdict",
    "github_review_id",
    "blocking_findings",
    "implementation_prompt",
    "artifacts_dir",
}
SUBMIT_SUCCESS_KEYS = {
    "schema_version",
    "command",
    "repository",
    "pr_number",
    "base_sha",
    "previous_head_sha",
    "resulting_head_sha",
    "commit_sha",
    "pushed_branch",
    "artifacts_dir",
}


class RecordingScenarioRunner(ScenarioRunner):
    """Record process identities while retaining the disposable Git transport."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
        """Initialize the scenario and an empty command record."""
        super().__init__(repo, remote, state)
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int = 120,
        input_text: str | None = None,
        check: bool = True,
        max_output: int = 24 * 1024 * 1024,
        watch_path: Path | None = None,
    ) -> CommandResult:
        """Record one argv before delegating to the real/fake scenario transport."""
        argv = tuple(str(value) for value in args)
        self.commands.append(argv)
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            check=check,
            max_output=max_output,
            watch_path=watch_path,
        )


@dataclass(frozen=True)
class AcceptanceFixture:
    """Disposable repository state shared by one acceptance flow."""

    repo: Path
    base_sha: str
    head_sha: str
    runner: RecordingScenarioRunner


def _acceptance_fixture(tmp_path: Path) -> AcceptanceFixture:
    """Create a disposable PR repository with recorded fake GitHub transport."""
    repo, remote, state, base_sha, head_sha = _fixture_repo(tmp_path)
    return AcceptanceFixture(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        runner=RecordingScenarioRunner(repo, remote, state),
    )


def _review_result(
    fixture: AcceptanceFixture,
    *,
    head_sha: str,
    verdict: str,
    artifacts_dir: Path,
) -> ReviewResult:
    """Create one stable fake reviewer result bound to a disposable PR head."""
    request_changes = verdict == "REQUEST_CHANGES"
    blockers = (
        (
            {
                "id": "B1",
                "title": "Fix the fixture",
                "description": "The fixture still contains the review blocker.",
                "required_change": "Replace feature content with fixed content.",
            },
        )
        if request_changes
        else ()
    )
    return ReviewResult(
        repository="acme/demo",
        pr_number=1,
        base_sha=fixture.base_sha,
        head_sha=head_sha,
        verdict=verdict,
        github_review_id=101 if request_changes else 102,
        blocking_findings=blockers,
        implementation_prompt=(
            "Implement only B1 and run repository QA." if blockers else None
        ),
        artifacts_dir=str(artifacts_dir),
    )


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    """Read the one structured stdout object emitted by the CLI."""
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    return cast(dict[str, object], json.loads(captured.out))


def _run_review_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    result: ReviewResult,
) -> tuple[int, dict[str, object]]:
    """Run the public review CLI with only external reviewer transport faked."""
    monkeypatch.setattr(cli, "execute_review", lambda **_kwargs: result)
    status = cli.main([
        "review",
        "--pr",
        "1",
        "--repo-dir",
        str(fixture.repo),
    ])
    return status, _stdout_json(capsys)


def _run_submit_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    artifacts_dir: Path,
) -> tuple[int, dict[str, object]]:
    """Run the public submit CLI against a real disposable Git repository."""

    def submit_with_fixture(**_kwargs: object) -> SubmitResult:
        return execute_submit(
            pr_value="1",
            expected_head=fixture.head_sha,
            repo_dir=fixture.repo,
            artifacts_dir=artifacts_dir,
            runner=fixture.runner,
        )

    monkeypatch.setattr(cli, "execute_submit", submit_with_fixture)
    status = cli.main([
        "submit",
        "--pr",
        "1",
        "--expected-head",
        fixture.head_sha,
        "--repo-dir",
        str(fixture.repo),
        "--artifacts-dir",
        str(artifacts_dir),
    ])
    return status, _stdout_json(capsys)


def _runner_with_token() -> CommandRunner:
    """Return a command runner carrying only the fake reviewer token."""
    return CommandRunner({"GH_REVIEW_TOKEN": "token"})


@pytest.mark.parametrize(("client", "discovery_path"), CLIENTS)
def test_cross_agent_request_submit_rereview_flow(
    client: str,
    discovery_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every supported host uses the same request/fix/submit/re-review contract."""
    discovered = REPOSITORY_ROOT / discovery_path
    assert discovered.is_symlink(), client
    assert (
        discovered.resolve(strict=True) == CANONICAL_SKILL.resolve(strict=True)
    ), client
    assert (discovered / "scripts" / "loopr.py").samefile(
        CANONICAL_SKILL / "scripts" / "loopr.py"
    )

    fixture = _acceptance_fixture(tmp_path)
    request = _review_result(
        fixture,
        head_sha=fixture.head_sha,
        verdict="REQUEST_CHANGES",
        artifacts_dir=tmp_path / "review-request",
    )
    request_status, request_payload = _run_review_cli(
        monkeypatch,
        capsys,
        fixture,
        request,
    )
    assert request_status == 0
    assert set(request_payload) == REVIEW_SUCCESS_KEYS
    assert request_payload["verdict"] == "REQUEST_CHANGES"
    assert request_payload["head_sha"] == fixture.head_sha

    (fixture.repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    submit_status, submit_payload = _run_submit_cli(
        monkeypatch,
        capsys,
        fixture,
        tmp_path / "submit",
    )
    assert submit_status == 0
    assert set(submit_payload) == SUBMIT_SUCCESS_KEYS
    assert submit_payload["previous_head_sha"] == fixture.head_sha
    assert submit_payload["resulting_head_sha"] == submit_payload["commit_sha"]

    resulting_head = cast(str, submit_payload["resulting_head_sha"])
    approval = _review_result(
        fixture,
        head_sha=resulting_head,
        verdict="APPROVE",
        artifacts_dir=tmp_path / "review-approve",
    )
    approve_status, approve_payload = _run_review_cli(
        monkeypatch,
        capsys,
        fixture,
        approval,
    )
    assert approve_status == 0
    assert set(approve_payload) == REVIEW_SUCCESS_KEYS
    assert approve_payload["verdict"] == "APPROVE"
    assert approve_payload["head_sha"] == resulting_head

    invoked_programs = {command[0] for command in fixture.runner.commands if command}
    assert not invoked_programs.intersection({"codex", "claude", "cursor"})


def test_operational_failure_uses_stable_nonzero_error_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operational failures remain non-zero while domain verdicts remain successful."""

    def fail_review(**_kwargs: object) -> ReviewResult:
        raise LooprError(EXIT_ORACLE, "oracle_schema", "malformed Oracle output")

    monkeypatch.setattr(cli, "execute_review", fail_review)
    status = cli.main(["review", "--pr", "1"])
    payload = _stdout_json(capsys)
    error = cast(dict[str, object], payload["error"])

    assert status == EXIT_ORACLE
    assert set(payload) == {"schema_version", "command", "error"}
    assert set(error) == {"category", "message"}
    assert error["category"] == "oracle_schema"


def test_malformed_oracle_output_fails_without_repair() -> None:
    """Malformed reviewer output deterministically fails the public review contract."""
    with pytest.raises(LooprError) as captured:
        parse_review("not-json", sample_pr())

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


def test_stale_review_head_fails_before_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A review snapshot that moves before posting fails deterministically."""
    initial = sample_pr()
    changed = sample_pr(head_sha="c" * 40)
    FakeGitHubClient.snapshots = [initial, changed]
    install_orchestration_fakes(monkeypatch)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            artifacts_dir=Path("artifacts"),
            thinking_time="heavy",
            runner=_runner_with_token(),
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_state"
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_stale_submit_head_fails_before_workspace_mutation(tmp_path: Path) -> None:
    """A stale expected submit head fails before staging or pushing."""
    repo, remote, state, _base_sha, head_sha = _fixture_repo(tmp_path)
    state["headRefOid"] = "d" * 40
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head_sha,
            repo_dir=repo,
            artifacts_dir=tmp_path / "artifacts",
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_head"


def test_manual_smoke_documentation_covers_all_supported_clients() -> None:
    """Operational docs retain executable discovery and stop-condition guidance."""
    text = (CANONICAL_SKILL / "references" / "operations.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())
    required = (
        "Codex CLI smoke test",
        "Claude Code smoke test",
        "Cursor CLI smoke test",
        ".agents/skills/loopr",
        ".claude/skills/loopr",
        "REQUEST_CHANGES",
        "blocking_findings",
        "--expected-head",
        "fresh `review`",
        "iteration limit",
        ".pr-loopr/runs/",
        "stale_head",
    )
    for concept in required:
        assert concept in normalized
