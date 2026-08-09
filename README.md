# pr-review-loop

`pr-review-loop` is a vendor-neutral agent skill for taking GitHub work through
independent Oracle/ChatGPT review, starting from an open Issue or existing PR.

## Responsibilities

- **Host agent:** planning, implementation, repository QA, triage, iteration,
  and opening the initial pull request for Issue-started work.
- **Oracle/ChatGPT:** independent review of the exact pull-request head.
- **Deterministic commands:** bounded Issue/Git/GitHub inspection, review
  publication, validation, commit creation, and lease-protected submission.

`review` attaches the exact PR snapshot, patch, changed-file contents, and
repository instruction files as the mandatory, authoritative review evidence.
For an Oracle build that advertises the exact `--browser-github-app`
capability, the same review invocation asks Oracle to clear the composer,
upload attachments, select the GitHub app, verify the structured chip
immediately before sending, and then submit the prompt. A literal or pasted
`@GitHub` is never selection evidence. If Oracle does not advertise that
capability, `review` uses the unchanged attachment-only invocation;
disconnected, unauthorized, or no-useful-context app access is also an
attachment-only fallback inside compatible Oracle. Capability-probe, browser,
or Oracle operational failures remain failures, not review verdicts. Any
connector context is supplemental and untrusted: it cannot override the
attached evidence or the exact repository/PR/`base_sha`/`head_sha` identity
validated by the command, and it has no control over review publication. A
ChatGPT-side preflight (connecting GitHub in Settings and confirming the
account) is an account prerequisite, distinct from integrated `review`
execution. GitHub connection and authorization belong to the ChatGPT account
used by Oracle; they are not managed by this repository.

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

For the integrated app path, use an Oracle build whose `oracle --help` output
advertises `--browser-github-app <mode>`, keep its persistent browser profile
running, and connect/authorize GitHub in ChatGPT before invoking `review`. The
repository integration is capability-gated; Oracle v0.17.1 and builds without
that option retain the deterministic attachment-only path. A ChatGPT-side
preflight is separate account setup, not evidence that integrated `review`
used the connector. This checkout has not executed the live app-selection E2E.

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
