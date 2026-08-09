# pr-review-loop

`pr-review-loop` is a vendor-neutral agent skill for taking GitHub work through
independent Oracle/ChatGPT review, starting from an open Issue or existing PR.

## Responsibilities

- **Host agent:** planning, implementation, repository QA, triage, iteration,
  and opening the initial pull request for Issue-started work.
- **Oracle/ChatGPT:** independent review of the exact pull-request head.
- **Deterministic commands:** bounded Issue/Git/GitHub inspection, review
  publication, validation, commit creation, and lease-protected submission.

The [canonical skill workflow](skills/pr-review-loop/SKILL.md) defines how
hosts coordinate those responsibilities. The [command contract](skills/pr-review-loop/references/command-contracts.md)
defines the internal CLI interfaces and structured results.

## Discovery

- Codex CLI and Cursor CLI discover `.agents/skills/pr-review-loop`.
- Claude Code discovers `.claude/skills/pr-review-loop`.

Both discovery paths resolve to the canonical skill. Compatible hosts select it
from user intent; users normally do not invoke the Python CLI directly.

## Requirements

- macOS or Linux;
- Python 3.12 or newer;
- Git and an authenticated GitHub CLI session;
- Oracle with Chrome/Chromium and an authenticated browser profile for Issue
  bootstrap and independent review;
- push access and Git commit identity when submitting changes.

Issue and pull-request workflows require matching same-repository GitHub.com
targets. GitHub Enterprise and fork targets are unsupported.

Initialize Oracle once when needed:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

## Usage

Ask a compatible host to implement an open Issue, create its PR, and carry it
through independent review, or review an existing PR. The host uses the
canonical workflow and command contract above.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check skills/pr-review-loop
uv run ruff format --check skills/pr-review-loop
uv run pyright
```
