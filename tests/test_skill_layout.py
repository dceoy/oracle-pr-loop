"""Structural tests for the canonical loopr skill layout."""

from __future__ import annotations

import pathlib

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL_SKILL = REPOSITORY_ROOT / "skills" / "loopr"
DISCOVERY_LINKS = (
    REPOSITORY_ROOT / ".agents" / "skills" / "loopr",
    REPOSITORY_ROOT / ".claude" / "skills" / "loopr",
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
    """The canonical skill owns the instructions and planned subdirectories."""
    skill_file = CANONICAL_SKILL / "SKILL.md"
    _require(skill_file.is_file(), "missing canonical SKILL.md")
    for directory in ("scripts", "references", "tests"):
        path = CANONICAL_SKILL / directory
        _require(path.is_dir(), f"missing canonical directory: {path}")


def test_loopr_discovery_links_are_direct_and_canonical() -> None:
    """Both clients discover the canonical directory through direct symlinks."""
    canonical = CANONICAL_SKILL.resolve(strict=True)
    expected_target = pathlib.Path("../../skills/loopr")
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
        "name: loopr",
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
