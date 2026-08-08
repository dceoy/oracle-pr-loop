# pr-review-loop

`pr-review-loop` is a vendor-neutral agent skill for taking GitHub work through independent Oracle/ChatGPT review. It can start from an existing pull request or, optionally, from an open GitHub Issue. The host agent owns planning, implementation, repository QA, and iteration; the skill provides deterministic Issue/Git/GitHub inspection, review transport, and guarded submission.

## Agent usage

The primary interface is the host agent, not the Python CLI. Compatible hosts discover this skill and can select it from the user's intent; the user does not need to name `pr-review-loop` or invoke its scripts directly.

Typical requests include:

- review an open pull request and resolve blocking findings until approval;
- fix or finalize an existing pull request;
- implement an open Issue, create its pull request, and carry that pull request through independent review.

The high-level flow is:

```text
optional open Issue
        ↓
bootstrap
        ↓
host implements + runs QA + commits/pushes + opens PR
        ↓
review
        ↓
APPROVE ───────────────→ done
        │
        └─ REQUEST_CHANGES
                 ↓
           host triages + fixes + runs QA
                 ↓
              submit
                 ↓
           fresh review
                 └──────── repeat as needed
```

`bootstrap`, `review`, and `submit` are deterministic command primitives used by the skill. They are documented below for development, debugging, and hosts that execute the skill through shell commands; they are not the primary user-facing UI.

## Discovery

The canonical skill lives at `skills/pr-review-loop/`. Codex CLI and Cursor CLI discover it through `.agents/skills/pr-review-loop`; Claude Code uses `.claude/skills/pr-review-loop`. All discovery paths point to the same implementation.

See `skills/pr-review-loop/SKILL.md` for the host-agent activation policy and workflow contract.

## Requirements

- macOS or Linux with Python 3.12+, Git, and authenticated GitHub CLI;
- an open, same-repository GitHub Issue and matching `origin` when starting from an Issue;
- an open, non-draft, same-repository GitHub.com pull request and matching `origin` for the review loop;
- Oracle with Chrome/Chromium and an authenticated browser profile for `bootstrap` and `review`;
- `GH_REVIEW_TOKEN` for a dedicated reviewer account distinct from the PR author, for `review`;
- push access and Git commit identity for `submit`.

Initialize Oracle once when needed:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

## Skill workflow

### Starting from a GitHub Issue

For work that has no pull request yet, the skill may call `bootstrap` for one open GitHub Issue. `bootstrap` returns an Oracle-generated `implementation_prompt` bound to the Issue and an exact base-branch commit snapshot.

The host agent then implements the change, runs repository QA, commits, pushes, and opens a pull request that links the Issue (for example, `Fixes #123`). `bootstrap` never edits, commits, pushes, or creates a pull request itself. Once the pull request exists, the normal PR review loop begins; `review` and `submit` have no Issue-specific behavior or persistent dependency on `bootstrap`.

### Reviewing an existing pull request

The skill reviews the exact PR head. `APPROVE` and `REQUEST_CHANGES` are both successful domain results. On `REQUEST_CHANGES`, the host agent deduplicates and validates the blocking findings against the reviewed head, fixes only findings that are valid and applicable, and runs normal repository QA.

If triage produces no applicable fix, the loop stops rather than submitting or re-reviewing an unchanged head. Otherwise, the skill submits the complete workspace patch against the reviewed head and immediately performs a fresh independent review. The host owns iteration and must stop on operational failure or the chosen iteration limit rather than manufacture approval.

## Internal command interface

Start from an Issue:

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

`bootstrap` requires an open, same-repository GitHub Issue and Oracle with an authenticated browser profile; it does not require `GH_REVIEW_TOKEN`.

Review the exact PR head:

```console
export GH_REVIEW_TOKEN='...'
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

`review` requires an open, non-draft, same-repository GitHub.com PR, exact base/head binding, Oracle with an authenticated browser profile, and `GH_REVIEW_TOKEN` for a dedicated reviewer account different from the PR author.

Submit the host's complete patch against the reviewed head:

```console
python3 skills/pr-review-loop/scripts/cli.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <REVIEWED_HEAD_SHA>
```

`submit` validates repository identity, exact local/remote head binding, conflicts, whitespace, staged content, credentials, repeated PR snapshots, and the remote branch lease. It creates one hook-free unsigned commit, pushes only with an explicit force-with-lease, and confirms the resulting GitHub PR head.

## Contracts and limits

All three commands emit exactly one JSON object on stdout; diagnostics go to stderr. Private audit artifacts are written below `.pr-review-loop/runs/` by default. See `skills/pr-review-loop/references/command-contracts.md` for schemas and exit classes and `skills/pr-review-loop/references/operations.md` for the compact cross-client smoke-test procedure.

GitHub Enterprise and fork PRs/Issues are unsupported. CI status is not an approval gate. `bootstrap` never implements the Issue, edits, commits, pushes, or creates a pull request. `review` never edits, commits, pushes, or launches an implementation agent. `submit` never plans changes, interprets findings, runs repository QA, or launches a model process.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check skills/pr-review-loop
uv run ruff format --check skills/pr-review-loop
uv run pyright
```
