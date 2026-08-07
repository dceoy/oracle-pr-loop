"""Structural tests for the canonical pr-review-loop skill layout."""

from __future__ import annotations

import pathlib

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]
CANONICAL_SKILL = REPOSITORY_ROOT / "skills" / "pr-review-loop"
DISCOVERY_LINKS = (
    REPOSITORY_ROOT / ".agents" / "skills" / "pr-review-loop",
    REPOSITORY_ROOT / ".claude" / "skills" / "pr-review-loop",
)
PRODUCTION_MODULES = (
    "artifacts",
    "cli",
    "github",
    "models",
    "oracle",
    "process",
    "review",
    "submission",
    "submit",
)
UNRELATED_DISCOVERY = (
    (
        REPOSITORY_ROOT / ".claude" / "skills" / "local-qa",
        REPOSITORY_ROOT / ".agents" / "skills" / "local-qa",
    ),
    (
        REPOSITORY_ROOT / ".claude" / "skills" / "pr-feedback-triage",
        REPOSITORY_ROOT / ".agents" / "skills" / "pr-feedback-triage",
    ),
)


def _require(condition: bool, message: str) -> None:
    """Raise an assertion with a focused diagnostic."""
    if not condition:
        raise AssertionError(message)


def test_canonical_skill_layout_exists() -> None:
    """The canonical skill owns all production code, references, and tests."""
    skill_file = CANONICAL_SKILL / "SKILL.md"
    _require(skill_file.is_file(), "missing canonical SKILL.md")
    for directory in ("scripts", "references", "tests"):
        path = CANONICAL_SKILL / directory
        _require(path.is_dir(), f"missing canonical directory: {path}")


def test_production_module_names_are_simple() -> None:
    """Production modules use short lowercase names without underscores."""
    scripts = CANONICAL_SKILL / "scripts"
    actual = tuple(
        sorted(path.stem for path in scripts.glob("*.py") if path.name != "__init__.py")
    )
    _require(actual == PRODUCTION_MODULES, f"unexpected production modules: {actual}")
    for name in actual:
        _require(
            name.isalpha() and name.islower(),
            f"production module name must be lowercase letters only: {name}",
        )


def test_cli_module_is_named_by_responsibility() -> None:
    """The command entrypoint uses a responsibility-based module name."""
    _require((CANONICAL_SKILL / "scripts" / "cli.py").is_file(), "missing cli.py")


def test_pr_review_loop_discovery_links_are_direct_and_canonical() -> None:
    """Both clients discover the canonical directory through direct symlinks."""
    canonical = CANONICAL_SKILL.resolve(strict=True)
    expected_target = pathlib.Path("../../skills/pr-review-loop")
    for link in DISCOVERY_LINKS:
        _require(link.is_symlink(), f"not a symlink: {link}")
        _require(link.readlink() == expected_target, f"unexpected target: {link}")
        _require(link.resolve(strict=True) == canonical, f"wrong target: {link}")
        _require(
            (link / "SKILL.md").samefile(CANONICAL_SKILL / "SKILL.md"),
            f"copied skill instructions detected: {link}",
        )


def test_skill_contract_is_vendor_neutral_and_complete() -> None:
    """The canonical instructions contain the required workflow boundaries."""
    text = (CANONICAL_SKILL / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    required = (
        "name: pr-review-loop",
        "Host agent",
        "Oracle/ChatGPT",
        "Skill scripts",
        "GitHub/Git",
        "exact current head",
        "structured review result",
        "applicable validation",
        "fresh Oracle/ChatGPT review",
        "iteration limit",
        "machine-readable JSON",
        "review --pr <NUMBER_OR_URL>",
        "submit --pr <NUMBER_OR_URL> --expected-head <SHA>",
        "APPROVE",
        "REQUEST_CHANGES",
        "non-zero exit status",
        "Codex CLI",
        "Claude Code",
        "Cursor CLI",
    )
    for concept in required:
        _require(concept in normalized, f"missing skill concept: {concept}")

    forbidden = ("codex exec", "claude -p", "cursor agent")
    for command in forbidden:
        _require(command not in text.lower(), f"host-specific command: {command}")


def test_runtime_has_no_agent_or_linux_containment() -> None:
    """Production modules contain no embedded agent or Linux supervisor path."""
    forbidden = (
        "codex exec",
        "codex login",
        "pidfd_open",
        "pr_set_child_subreaper",
        "subreaper",
        "/proc/",
        "git worktree add",
    )
    scripts = CANONICAL_SKILL / "scripts"
    for path in scripts.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for concept in forbidden:
            _require(
                concept not in text,
                f"unsupported runtime concept in {path}: {concept}",
            )


def test_docs_describe_current_interface() -> None:
    """Public documentation describes only the current skill commands."""
    documents = (
        REPOSITORY_ROOT / "README.md",
        REPOSITORY_ROOT / "pyproject.toml",
        CANONICAL_SKILL / "SKILL.md",
        CANONICAL_SKILL / "references" / "command-contracts.md",
    )
    forbidden = (
        "node.js 24",
        "codex login",
        "disposable worktree",
        "issue #18",
        "pidfd_open",
    )
    for path in documents:
        text = path.read_text(encoding="utf-8").lower()
        for concept in forbidden:
            _require(concept not in text, f"stale documentation in {path}: {concept}")


def test_unrelated_skill_discovery_remains_valid() -> None:
    """Replacing the directory-level symlink preserves unrelated skills."""
    claude_skills = REPOSITORY_ROOT / ".claude" / "skills"
    _require(claude_skills.is_dir(), "Claude skills discovery is not a directory")
    _require(not claude_skills.is_symlink(), "Claude skills still uses a chain")
    for discovery, canonical in UNRELATED_DISCOVERY:
        _require(discovery.is_symlink(), f"not a symlink: {discovery}")
        _require(
            discovery.resolve(strict=True) == canonical.resolve(strict=True),
            f"unrelated skill target changed: {discovery}",
        )
        skill_file = discovery / "SKILL.md"
        _require(skill_file.is_file(), f"skill is undiscoverable: {discovery}")
