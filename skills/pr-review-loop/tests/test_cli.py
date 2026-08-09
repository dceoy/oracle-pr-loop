"""CLI acceptance and repository-surface tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NoReturn, cast

import pytest
from scripts import cli
from scripts import review as review_module
from scripts.github import GitHubClient
from scripts.models import (
    EXIT_ORACLE,
    EXIT_PRECONDITION,
    EXIT_RACE,
    BootstrapResult,
    JsonObject,
    JsonValue,
    LooprError,
    PullRequest,
    ReviewComment,
)
from scripts.oracle import parse_review
from scripts.process import CommandResult, CommandRunner
from scripts.review import execute_review
from scripts.submit import execute_submit
from test_review import FakeGitHubClient, install_orchestration_fakes, sample_pr
from test_submit import ScenarioRunner, _fixture_repo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pytest_mock import MockerFixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_SKILL = REPOSITORY_ROOT / "skills" / "pr-review-loop"
CLIENTS = (
    ("Codex CLI", Path(".agents/skills/pr-review-loop")),
    ("Claude Code", Path(".claude/skills/pr-review-loop")),
    ("Cursor CLI", Path(".agents/skills/pr-review-loop")),
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
}
BOOTSTRAP_SUCCESS_KEYS = {
    "schema_version",
    "command",
    "repository",
    "issue_number",
    "issue_url",
    "issue_updated_at",
    "base_ref",
    "base_sha",
    "implementation_prompt",
}


class RecordingScenarioRunner(ScenarioRunner):
    """Record process identities while retaining the disposable Git transport."""

    def __init__(self, repo: Path, remote: Path, state: JsonObject) -> None:
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
        super().__init__()
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
        argv = tuple(str(value) for value in args)
        self.commands.append(argv)
        if argv and argv[0] == "oracle":
            if watch_path is None:
                msg = "Oracle invocation must provide a watched output path"
                raise AssertionError(msg)
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
    ) -> None:
        super().__init__(runner, repo_dir)
        type(self).instance = self
        self._snapshots = list(type(self).snapshots)
        self.post_count = 0
        self.posted_events: list[str] = []
        self.posted_comments: list[tuple[ReviewComment, ...]] = []

    def initialize(self, pr_value: str) -> None:
        del pr_value
        if not self._snapshots:
            msg = "acceptance review requires at least one PR snapshot"
            raise AssertionError(msg)
        initial = self._snapshots[0]
        self.repository = initial.repository
        self.number = initial.number
        self.url = initial.url
        self.authenticated_login = "reviewer"

    def review_event(self, pull_request: PullRequest, verdict: str) -> str:
        return "COMMENT" if pull_request.author == self.authenticated_login else verdict

    def snapshot(self, *, require_open: bool = True) -> PullRequest:
        del require_open
        if not self._snapshots:
            msg = "acceptance GitHub snapshot sequence was exhausted"
            raise AssertionError(msg)
        return self._snapshots.pop(0)

    def post_review(
        self,
        pull_request: PullRequest,
        event: str,
        body: str,
        comments: tuple[ReviewComment, ...] = (),
    ) -> tuple[int, JsonObject]:
        del body
        self.post_count += 1
        self.posted_events.append(event)
        self.posted_comments.append(comments)
        review_id = 101 if event == "REQUEST_CHANGES" else 102
        return review_id, {"id": review_id, "commit_id": pull_request.head_sha}

    def verify_posted(
        self,
        pull_request: PullRequest,
        review_id: int,
        body: str,
    ) -> JsonObject:
        del pull_request, review_id, body
        if not self.posted_events:
            msg = "review verification occurred before posting"
            raise AssertionError(msg)
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
    repo, remote, state, base_sha, head_sha = _fixture_repo(tmp_path)
    return AcceptanceFixture(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        runner=RecordingScenarioRunner(repo, remote, state),
    )


def _review_snapshot(fixture: AcceptanceFixture, *, head_sha: str) -> PullRequest:
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
    request_changes = verdict == "REQUEST_CHANGES"
    blockers: list[JsonValue] = []
    if request_changes:
        finding: JsonObject = {
            "id": "B1",
            "title": "Fix the fixture",
            "description": "The fixture still contains the review blocker.",
            "required_change": "Replace feature content with fixed content.",
            "location": {"path": "file.txt", "line": 1, "side": "RIGHT"},
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
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    return cast("dict[str, object]", json.loads(captured.out))


def _run_review_cli(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    pull_request: PullRequest,
    verdict: str,
) -> tuple[int, dict[str, object], AcceptanceReviewRunner]:
    AcceptanceGitHubClient.snapshots = [pull_request, pull_request, pull_request]
    mocker.patch.object(review_module, "GitHubClient", AcceptanceGitHubClient)
    runner = AcceptanceReviewRunner(_oracle_payload(pull_request, verdict=verdict))
    mocker.patch.object(cli, "CommandRunner", return_value=runner)
    status = cli.main([
        "review",
        "--pr",
        "1",
        "--repo-dir",
        str(fixture.repo),
    ])
    return status, _stdout_json(capsys), runner


def _run_submit_cli(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
) -> tuple[int, dict[str, object]]:
    mocker.patch.object(cli, "CommandRunner", return_value=fixture.runner)
    status = cli.main([
        "submit",
        "--pr",
        "1",
        "--expected-head",
        fixture.head_sha,
        "--repo-dir",
        str(fixture.repo),
    ])
    assert any(
        command[:3] == ("git", "diff-tree", "--no-commit-id")
        for command in fixture.runner.commands
    )
    return status, _stdout_json(capsys)


def _assert_skill_discovery(client: str, discovery_path: Path) -> None:
    discovered = REPOSITORY_ROOT / discovery_path
    assert discovered.is_symlink(), client
    assert discovered.resolve(strict=True) == CANONICAL_SKILL.resolve(strict=True)
    assert (discovered / "scripts" / "cli.py").samefile(
        CANONICAL_SKILL / "scripts" / "cli.py"
    )


def test_skill_discovery_directories_contain_only_known_skills() -> None:
    """No standalone skill, such as the removed pr-feedback-triage, can return."""
    known_skills = {"pr-review-loop", "local-qa"}
    for discovery_dir in (
        REPOSITORY_ROOT / ".agents" / "skills",
        REPOSITORY_ROOT / ".claude" / "skills",
    ):
        entries = {entry.name for entry in discovery_dir.iterdir()}
        assert entries == known_skills, discovery_dir


def _assert_review_result(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    fixture: AcceptanceFixture,
    *,
    head_sha: str,
    verdict: str,
    expected_patch: str,
    rejected_patch: str,
) -> AcceptanceReviewRunner:
    pull_request = _review_snapshot(fixture, head_sha=head_sha)
    status, payload, runner = _run_review_cli(
        mocker,
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
    anchored = [
        (comment.path, comment.side, comment.line)
        for comment in github.posted_comments[0]
    ]
    assert anchored == (
        [("file.txt", "RIGHT", 1)] if verdict == "REQUEST_CHANGES" else []
    )

    assert "artifacts_dir" not in payload
    oracle_commands = [
        command for command in runner.commands if command[:1] == ("oracle",)
    ]
    assert oracle_commands
    for command in oracle_commands:
        for index, value in enumerate(command[:-1]):
            if value in {"--file", "--write-output"}:
                assert not Path(command[index + 1]).exists()
    assert expected_patch
    assert rejected_patch
    return runner


def _assert_host_programs(
    fixture: AcceptanceFixture,
    *review_runners: AcceptanceReviewRunner,
) -> None:
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
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every supported host uses the same request/fix/submit/re-review contract."""
    _assert_skill_discovery(client, discovery_path)
    fixture = _acceptance_fixture(tmp_path)
    request_runner = _assert_review_result(
        mocker,
        capsys,
        fixture,
        head_sha=fixture.head_sha,
        verdict="REQUEST_CHANGES",
        expected_patch="+feature\n",
        rejected_patch="+fixed\n",
    )

    (fixture.repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    submit_status, submit_payload = _run_submit_cli(
        mocker,
        capsys,
        fixture,
    )
    assert submit_status == 0
    assert set(submit_payload) == SUBMIT_SUCCESS_KEYS
    assert submit_payload["previous_head_sha"] == fixture.head_sha
    assert submit_payload["resulting_head_sha"] == submit_payload["commit_sha"]

    resulting_head = cast("str", submit_payload["resulting_head_sha"])
    approve_runner = _assert_review_result(
        mocker,
        capsys,
        fixture,
        head_sha=resulting_head,
        verdict="APPROVE",
        expected_patch="+fixed\n",
        rejected_patch="+feature\n",
    )
    _assert_host_programs(fixture, request_runner, approve_runner)


@pytest.mark.parametrize(
    ("command", "args", "function_name", "error_code", "category"),
    [
        pytest.param(
            "review",
            ("review", "--pr", "1"),
            "execute_review",
            EXIT_ORACLE,
            "oracle_schema",
            id="review",
        ),
        pytest.param(
            "bootstrap",
            ("bootstrap", "--issue", "7"),
            "execute_bootstrap",
            EXIT_RACE,
            "stale_state",
            id="bootstrap",
        ),
    ],
)
def test_operational_failure_uses_stable_nonzero_error_schema(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
    function_name: str,
    error_code: int,
    category: str,
) -> None:
    def fail_command(**_kwargs: object) -> NoReturn:
        raise LooprError(error_code, category, "command failed")

    mocker.patch.object(cli, function_name, fail_command)
    status = cli.main(list(args))
    payload = _stdout_json(capsys)
    error = cast("dict[str, object]", payload["error"])

    assert status == error_code
    assert payload["command"] == command
    assert set(payload) == {"schema_version", "command", "error"}
    assert error["category"] == category


@pytest.mark.parametrize(
    ("args", "command"),
    [
        pytest.param(("review",), "review", id="review"),
        pytest.param(("bootstrap",), "bootstrap", id="bootstrap"),
    ],
)
def test_argument_failure_uses_structured_error_schema(
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    command: str,
) -> None:
    status = cli.main(list(args))
    payload = _stdout_json(capsys)
    error = cast("dict[str, object]", payload["error"])

    assert status == EXIT_PRECONDITION
    assert payload["command"] == command
    assert error["category"] == "input"


@pytest.mark.parametrize(
    ("command", "target"),
    [("bootstrap", "execute_bootstrap"), ("review", "execute_review")],
)
@pytest.mark.parametrize(
    ("overrides", "expected_model", "expected_effort"),
    [
        ((), None, None),
        (("--oracle-model", "gpt-5.6-sol"), "gpt-5.6-sol", None),
        (("--oracle-thinking-time", "extended"), None, "extended"),
        (
            (
                "--oracle-model",
                "gpt-5.6-sol",
                "--oracle-thinking-time",
                "heavy",
            ),
            "gpt-5.6-sol",
            "heavy",
        ),
    ],
)
def test_cli_propagates_oracle_overrides_consistently(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    target: str,
    overrides: tuple[str, ...],
    expected_model: str | None,
    expected_effort: str | None,
) -> None:
    """Bootstrap and review pass both optional Oracle values unchanged."""
    captured: dict[str, object] = {}

    def fake_execute(**kwargs: object) -> object:
        captured.update(kwargs)
        if command == "bootstrap":
            return BootstrapResult(
                repository="acme/demo",
                issue_number=1,
                issue_url="https://github.com/acme/demo/issues/1",
                issue_updated_at="2026-01-01T00:00:00Z",
                base_ref="main",
                base_sha="a" * 40,
                implementation_prompt="Implement the requested change.",
            )
        return ReviewResult(
            repository="acme/demo",
            pr_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            verdict="APPROVE",
            github_review_id=1,
            blocking_findings=(),
            implementation_prompt=None,
        )

    monkeypatch.setattr(cli, target, fake_execute)
    identifier_flag = "--issue" if command == "bootstrap" else "--pr"
    status = cli.main([command, identifier_flag, "1", *overrides])
    _stdout_json(capsys)

    assert status == 0
    assert captured["model"] == expected_model
    assert captured["thinking_time"] == expected_effort


@pytest.mark.parametrize(
    "effort",
    ["light", "standard", "extended", "heavy"],
)
def test_cli_accepts_all_oracle_thinking_time_values(effort: str) -> None:
    """The CLI accepts every browser effort value delegated to Oracle."""
    args = cli.parser().parse_args([
        "review",
        "--pr",
        "1",
        "--oracle-thinking-time",
        effort,
    ])

    assert args.oracle_thinking_time == effort


@pytest.mark.parametrize("effort", ["extra-high", "pro", "unsupported"])
def test_cli_rejects_invalid_oracle_thinking_time(
    capsys: pytest.CaptureFixture[str],
    effort: str,
) -> None:
    """Invalid effort remains a structured input error before dispatch."""
    status = cli.main(["review", "--pr", "1", "--oracle-thinking-time", effort])
    payload = _stdout_json(capsys)
    error = cast("dict[str, object]", payload["error"])

    assert status == EXIT_PRECONDITION
    assert error["category"] == "input"


def test_artifacts_directory_argument_is_removed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The old persistent-directory option is rejected by the CLI."""
    status = cli.main([
        "review",
        "--pr",
        "1",
        "--artifacts-dir",
        "retained",
    ])
    payload = _stdout_json(capsys)
    error = cast("dict[str, object]", payload["error"])

    assert status == EXIT_PRECONDITION
    assert error["category"] == "input"


def test_bootstrap_cli_emits_the_stable_success_schema(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The public bootstrap command emits exactly one bootstrap JSON object."""
    expected = BootstrapResult(
        repository="acme/demo",
        issue_number=7,
        issue_url="https://github.com/acme/demo/issues/7",
        issue_updated_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha="a" * 40,
        implementation_prompt="Implement the requested change.",
    )

    def fake_bootstrap(**_kwargs: object) -> BootstrapResult:
        return expected

    mocker.patch.object(cli, "execute_bootstrap", fake_bootstrap)
    status = cli.main(["bootstrap", "--issue", "7"])
    payload = _stdout_json(capsys)

    assert status == 0
    assert set(payload) == BOOTSTRAP_SUCCESS_KEYS
    assert payload == expected.as_json()


def test_help_has_no_implementation_agent_dependency() -> None:
    help_text = cli.parser().format_help().lower()
    assert "review" in help_text
    assert not {"codex", "claude", "cursor"}.intersection(help_text.split())


def test_malformed_oracle_output_fails_without_repair() -> None:
    with pytest.raises(LooprError) as captured:
        parse_review("not-json", sample_pr())

    assert captured.value.code == EXIT_ORACLE
    assert captured.value.category == "oracle_schema"


def test_stale_review_head_fails_before_post(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    initial = sample_pr()
    changed = sample_pr(head_sha="c" * 40)
    FakeGitHubClient.snapshots = [initial, changed]
    install_orchestration_fakes(mocker)

    with pytest.raises(LooprError) as captured:
        execute_review(
            pr_value="21",
            repo_dir=tmp_path,
            thinking_time="heavy",
            runner=CommandRunner(),
        )

    assert captured.value.code == EXIT_RACE
    assert FakeGitHubClient.instance is not None
    assert FakeGitHubClient.instance.post_count == 0


def test_stale_submit_head_fails_before_workspace_mutation(tmp_path: Path) -> None:
    repo, remote, state, _base_sha, head_sha = _fixture_repo(tmp_path)
    state["headRefOid"] = "d" * 40
    (repo / "file.txt").write_text("fixed\n", encoding="utf-8")
    runner = ScenarioRunner(repo, remote, state)

    with pytest.raises(LooprError) as captured:
        execute_submit(
            pr_value="1",
            expected_head=head_sha,
            repo_dir=repo,
            runner=runner,
        )

    assert captured.value.code == EXIT_RACE
    assert captured.value.category == "stale_head"


def test_test_modules_match_production_modules() -> None:
    """Every production module has exactly one same-named test module."""
    production = {
        path.stem
        for path in (CANONICAL_SKILL / "scripts").glob("*.py")
        if path.name != "__init__.py"
    }
    tests = {
        path.stem.removeprefix("test_")
        for path in (CANONICAL_SKILL / "tests").glob("test_*.py")
    }

    assert tests == production


def test_documentation_links_preserve_canonical_ownership() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (CANONICAL_SKILL / "SKILL.md").read_text(encoding="utf-8")
    contracts = (CANONICAL_SKILL / "references" / "command-contracts.md").read_text(
        encoding="utf-8"
    )
    operations_path = CANONICAL_SKILL / "references" / "operations.md"
    operations = " ".join(operations_path.read_text(encoding="utf-8").split())

    assert "skills/pr-review-loop/SKILL.md" in readme
    assert "skills/pr-review-loop/references/command-contracts.md" in readme
    assert "references/command-contracts.md" in skill
    assert "references/operations.md" in skill
    assert "../SKILL.md" in contracts
    assert "operations.md" in contracts
    assert operations_path.exists()
    for concept in ("browser-tools.ts", "--port", "@GitHub", "commit anchor"):
        assert concept in operations
    for heading in (
        "Common flow",
        "Issue bootstrap",
        "Codex CLI smoke test",
        "Claude Code smoke test",
        "Cursor CLI smoke test",
        "Recovery",
    ):
        assert heading not in operations
