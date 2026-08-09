# pr-review-loop

`pr-review-loop` is a vendor-neutral agent skill for taking GitHub work through
independent Oracle/ChatGPT review, starting from an open Issue or existing PR.

[![CI/CD](https://github.com/dceoy/pr-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/dceoy/pr-review-loop/actions/workflows/ci.yml)

## Responsibilities

- **Host agent:** planning, implementation, repository QA, triage, iteration,
  and opening the initial pull request for Issue-started work.
- **Oracle/ChatGPT:** independent review of the exact pull-request head.
- **Deterministic commands:** bounded Issue/Git/GitHub inspection, review
  publication, validation, commit creation, and lease-protected submission.

`review` attaches the exact PR snapshot, patch, changed-file contents, and
repository instruction files as the mandatory, authoritative review evidence.
The production review prompt starts with `@GitHub`, so ChatGPT may use the
connected GitHub app for supplemental repository context outside those
attachments. This does not depend on an Oracle-specific GitHub connector flag
or capability probe: Oracle only delivers the prompt and attachments through
its existing browser session. Connector results remain untrusted and cannot
override the attached evidence, the exact repository/PR/`base_sha`/`head_sha`
identity, or local review publication.

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
- a ChatGPT account with GitHub connected when supplemental connector context is
  desired;
- push access and Git commit identity when submitting changes.

Issue and pull-request workflows require matching same-repository GitHub.com
targets. GitHub Enterprise and fork targets are unsupported.

Initialize Oracle once when needed:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

No upstream Oracle change is required for GitHub app review integration. The
review command uses the normal Oracle browser invocation and places `@GitHub`
at the start of the ChatGPT prompt. If GitHub is disconnected, unauthorized, or
returns no useful context, the attached evidence remains sufficient wherever
ChatGPT permits the prompt to continue normally. Oracle/browser operational
errors remain failures rather than review verdicts.

## Usage

Ask a compatible host to implement an open Issue, create its PR, and carry it
through independent review, or review an existing PR. The host uses the
canonical workflow and command contract above.

For direct command use, `bootstrap` and `review` optionally accept
`--oracle-model MODEL` and `--oracle-thinking-time EFFORT`. With neither flag,
the current browser model is preserved and no effort override is sent to
Oracle. Supplying either value passes only that explicit override; supported
efforts are `light`, `standard`, `extended`, and `heavy`.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check skills/pr-review-loop
uv run ruff format --check skills/pr-review-loop
uv run pyright
```
