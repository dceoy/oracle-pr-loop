"""Cross-agent acceptance tests for the canonical loopr skill workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from test_review_command import (
    FakeGitHubClient,
    install_orchestration_fakes,
    sample_pr,
)
from test_submit_command import ScenarioRunner, _fixture_repo

from scripts import loopr as cli, review as review_module
from scripts.github import GitHubClient
from scripts.models import (
    EXIT_ORACLE,
    EXIT_RACE,
    JsonObject,
    JsonValue,
    LooprError,
    PullRequest,
    ReviewResult,
    SubmitResult,
)
from scripts.oracle import parse_review
from scripts.process import CommandResult, CommandRunner
from scripts.review import execute_review
from scripts.submit import execute_submit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


class AcceptanceReviewRunner(CommandRunner):
    """Fake Oracle while retaining production local subprocess execution."""

    def __init__(self, oracle_payload: JsonObject) -> None:
        """Initialize one deterministic Oracle response and command record."""
        super().__init__()
        self.source_env["GH_REVIEW_TOKEN"] = "token"
        self.secrets.add("token")
        self.oracle_payload = oracle_payload
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
        """Fake Oracle only; execute local Git through the production runner."""
        argv = tuple(str(value) for value in args)
        self.commands.append(argv)
        if argv and argv[0] == "oracle":
            if watch_path is None:
                message = "Oracle invocation must provide a watched output path"
                raise AssertionError(message)
            watch_path.write_text(json.dumps(self.oracle_payload), encoding="utf-8")
            return CommandResult(args=argv, returncode=0, stdout=b"", stderr="")
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


class AcceptanceGitHubClient(GitHubClient):
    """Fake GitHub network I/O while retaining production immutable Git reads."""

    instance: ClassVar[AcceptanceGitHubClient | None] = None
    snapshots: ClassVar[list[PullRequest]] = []

    def __init__(
        self,
        runner: CommandRunner,
        repo_dir: Path,
        token: str,
    ) -> None:
        """Initialize deterministic GitHub snapshots around real local Git evidence."""
        super().__init__(runner, repo_dir, token)
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)
        self.post_count = 0
        self.posted_events: list[str] = []

    def initialize(self, pr_value: str) -> None:
        """Bind fake network identity to the configured disposable PR snapshot."""
        del pr_value
        if not self._snapshots:
            raise AssertionError("acceptance review requires at least one PR snapshot")
        initial = self._snapshots[0]
        self.repository = initial.repository
        self.number = initial.number
        self.url = initial.url
        self.reviewer_login = "reviewer"

    def snapshot(self) -> PullRequest:
        """Return the next deterministic GitHub snapshot."""
        if not self._snapshots:
            raise AssertionError("acceptance GitHub snapshot sequence was exhausted")
        return self._snapshots.pop(0)

    def post_review(
        self,
        pull_request: PullRequest,
        verdict: str,
        body: str,
    ) -> tuple[int, JsonObject]:
        """Record the GitHub write that production review orchestration requested."""
        del body
        self.post_count += 1
        self.posted_events.append(verdict)
        review_id = 101 if verdict == "REQUEST_CHANGES" else 102
        return review_id, {"id": review_id, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        pull_request: PullRequest,
        review_id: int,
    ) -> JsonObject:
        """Return the state corresponding to the event posted by the real flow."""
        del pull_request, review_id
        if not self.posted_events:
            raise AssertionError("review verification occurred before posting")
        state = (
            "CHANGES_REQUESTED"
            if self.posted_events[-1] == "REQUEST_CHANGES"
            else "APPROVED"
        )
        return {"state": state}


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


def _review_snapshot(fixture: AcceptanceFixture, *, head_sha: str) -> PullRequest:
    """Bind the fake GitHub review snapshot to the disposable repository SHAs."""
    return replace(
        sample_pr(base_sha=fixture.base_sha, head_sha=head_sha),
        repository="acme/demo",
        number=1,
        url="https://github.com/acme/demo/pull/1",
        base_ref="main",
        head_ref="feature",
        head_repository="acme/demo",
        changed_paths=("file.txt",),
        raw={"baseRefOid": fixture.base_sha, "headRefOid": head_sha},
    )


def _oracle_payload(pull_request: PullRequest, *, verdict: str) -> JsonObject:
    """Return strict fake Oracle JSON for the exact frozen pull-request identity."""
    request_changes = verdict == "REQUEST_CHANGES"
    blockers: list[JsonValue] = []
    if request_changes:
        finding: JsonObject = {
            "id": "B1",
            "title": "Fix the fixture",
            "description": "The fixture still contains the review blocker.",
            "required_change": "Replace feature content with fixed content.",
        }
        blockers.append(finding)
    return {
        "schema_version": 1,
        "repository": pull_request.repository,
        "pr_number": pull_request.number,
        "base_sha": pull_request.base_sha,
        "head_sha": pull_request.head_sha,
        "verdict": verdict,
        "review_body": "Fix the fixture." if request_changes else "Approved.",
        "implementation_prompt": (
            "Implement only B1 and run repository QA." if request_changes else None
        ),
        "blocking_findings": blockers,
        "non_blocking_notes": [],
    }


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    """Read the one structured stdout object emitted by the CLI."""
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    return cast("dict[str, object]", json.loads(captured.out))


def _run_review_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    pull_request: PullRequest,
    verdict: str,
) -> tuple[int, dict[str, object], AcceptanceReviewRunner]:
    """Run the public review CLI with only Oracle/GitHub network I/O faked."""
    AcceptanceGitHubClient.snapshots = [pull_request, pull_request, pull_request]
    monkeypatch.setattr(review_module, "GitHubClient", AcceptanceGitHubClient)
    runner = AcceptanceReviewRunner(_oracle_payload(pull_request, verdict=verdict))
    monkeypatch.setattr(cli, "CommandRunner", lambda: runner)
    status = cli.main([
        "review",
        "--pr",
        "1",
        "--repo-dir",
        str(fixture.repo),
    ])
    return status, _stdout_json(capsys), runner


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


def _assert_skill_discovery(client: str, discovery_path: Path) -> None:
    """Verify one host resolves the shared canonical skill implementation."""
    discovered = REPOSITORY_ROOT / discovery_path
    assert discovered.is_symlink(), client
    assert discovered.resolve(strict=True) == CANONICAL_SKILL.resolve(strict=True)
    assert (discovered / "scripts" / "loopr.py").samefile(
        CANONICAL_SKILL / "scripts" / "loopr.py"
    )


def _assert_review_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    *,
    head_sha: str,
    verdict: str,
    expected_patch: str,
    rejected_patch: str,
) -> AcceptanceReviewRunner:
    """Run one review and verify its schema, event, artifacts, and frozen patch."""
    pull_request = _review_snapshot(fixture, head_sha=head_sha)
    status, payload, runner = _run_review_cli(
        monkeypatch,
        capsys,
        fixture,
        pull_request,
        verdict,
    )
    assert status == 0
    assert set(payload) == REVIEW_SUCCESS_KEYS
    assert payload["verdict"] == verdict
    assert payload["head_sha"] == head_sha

    github = AcceptanceGitHubClient.instance
    assert isinstance(github, AcceptanceGitHubClient)
    assert github.posted_events == [verdict]

    artifacts = Path(cast("str", payload["artifacts_dir"]))
    for artifact in (
        "snapshot.json",
        "patch.diff",
        "bundle-manifest.json",
        "validated-review.json",
        "github-review.json",
        "result.json",
    ):
        assert (artifacts / artifact).is_file()
    patch = (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert expected_patch in patch
    assert rejected_patch not in patch
    assert ".pr-loopr/" not in patch
    return runner


def _assert_host_programs(
    fixture: AcceptanceFixture,
    *review_runners: AcceptanceReviewRunner,
) -> None:
    """Verify the host workflow uses Git/Oracle without nested implementation agents."""
    for review_runner in review_runners:
        programs = {command[0] for command in review_runner.commands if command}
        assert {"git", "oracle"} <= programs

    commands = [
        *(command for runner in review_runners for command in runner.commands),
        *fixture.runner.commands,
    ]
    invoked_programs = {command[0] for command in commands if command}
    assert not invoked_programs.intersection({"codex", "claude", "cursor"})


@pytest.mark.parametrize(("client", "discovery_path"), CLIENTS)
def test_cross_agent_request_submit_rereview_flow(
    client: str,
    discovery_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every supported host uses the same request/fix/submit/re-review contract."""
    _assert_skill_discovery(client, discovery_path)
    fixture = _acceptance_fixture(tmp_path)
    request_runner = _assert_review_result(
        monkeypatch,
        capsys,
        fixture,
        head_sha=fixture.head_sha,
        verdict="REQUEST_CHANGES",
        expected_patch="+feature\n",
        rejected_patch="+fixed\n",
    )

    (fixture.repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    submit_status, submit_payload = _run_submit_cli(
        monkeypatch,
        capsys,
        fixture,
        Path(".pr-loopr"),
    )
    assert submit_status == 0
    assert set(submit_payload) == SUBMIT_SUCCESS_KEYS
    assert submit_payload["previous_head_sha"] == fixture.head_sha
    assert submit_payload["resulting_head_sha"] == submit_payload["commit_sha"]

    resulting_head = cast("str", submit_payload["resulting_head_sha"])
    approve_runner = _assert_review_result(
        monkeypatch,
        capsys,
        fixture,
        head_sha=resulting_head,
        verdict="APPROVE",
        expected_patch="+fixed\n",
        rejected_patch="+feature\n",
    )
    _assert_host_programs(fixture, request_runner, approve_runner)


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
    error = cast("dict[str, object]", payload["error"])

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
