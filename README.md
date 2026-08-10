# pr-review-loop

`pr-review-loop` is an agent skill for taking GitHub work through independent
Oracle/ChatGPT review, starting from an open Issue or existing PR.

[![CI/CD](https://github.com/dceoy/pr-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/dceoy/pr-review-loop/actions/workflows/ci.yml)

## Responsibilities

- **Host agent:** planning, implementation, repository QA, triage, iteration,
  and opening the initial pull request for Issue-started work.
- **Oracle/ChatGPT:** independent review of the exact pull-request head.
- **Deterministic commands:** bounded Issue/Git/GitHub inspection, review
  publication, validation, commit creation, and lease-protected submission.

The [canonical skill workflow](skills/pr-review-loop/SKILL.md) owns host
sequencing and trust boundaries. The
[command contract](skills/pr-review-loop/references/command-contracts.md) owns
CLI syntax, schemas, preconditions, and side effects. The
[operations reference](skills/pr-review-loop/references/operations.md) owns
Oracle/ChatGPT setup and smoke tests.

## Discovery

- Codex CLI and Cursor CLI discover `.agents/skills/pr-review-loop`.
- Claude Code discovers `.claude/skills/pr-review-loop`.

Both discovery paths resolve to the canonical skill. Compatible hosts select it
from user intent; users normally do not invoke the Python CLI directly.

## Requirements

- macOS or Linux;
- Python 3.12 or newer;
- Git and an authenticated GitHub CLI session;
- Oracle CLI configured with either a local authenticated Chrome/Chromium
  profile or a remote `oracle serve` instance;
- push access and Git commit identity when submitting changes.

Issue and pull-request workflows require matching same-repository GitHub.com
targets. GitHub Enterprise and fork targets are unsupported.

## Usage

Ask a compatible host to implement an open Issue and carry its pull request
through review, or to review and improve an existing pull request. See the
canonical workflow and command contract above for the normative behavior.
