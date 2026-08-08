# pr-review-loop

`pr-review-loop` is a vendor-neutral agent skill for improving one GitHub pull request through independent Oracle/ChatGPT review, optionally starting from an open GitHub Issue. The host agent owns implementation and repository QA; the skill owns deterministic Issue/Git/GitHub inspection, review transport, and guarded submission.

The canonical skill lives at `skills/pr-review-loop/`. Codex CLI and Cursor CLI discover it through `.agents/skills/pr-review-loop`; Claude Code uses `.claude/skills/pr-review-loop`. All discovery paths point to the same implementation.

## Requirements

- macOS or Linux with Python 3.10+, Git, and authenticated GitHub CLI;
- an open, same-repository GitHub Issue and matching `origin` for `bootstrap`;
- an open, non-draft, same-repository GitHub.com pull request and matching `origin` for `review` and `submit`;
- Oracle with Chrome/Chromium and an authenticated browser profile for `bootstrap` and `review`;
- `GH_REVIEW_TOKEN` for a dedicated reviewer account distinct from the PR author, for `review`;
- push access and Git commit identity for `submit`.

Initialize Oracle once when needed:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

## Workflow

Work that has no pull request yet can start from one open GitHub Issue:

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

`bootstrap` returns one Oracle-generated `implementation_prompt` bound to the Issue and an exact base-branch commit snapshot. The host agent implements the change, runs repository QA, commits, pushes, and opens a pull request that links the Issue (for example, `Fixes #123`). `bootstrap` never edits, commits, pushes, or creates a pull request itself; see `skills/pr-review-loop/SKILL.md` for the full handoff diagram.

Review the exact PR head:

```console
export GH_REVIEW_TOKEN='...'
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

`APPROVE` and `REQUEST_CHANGES` are both successful domain results. On `REQUEST_CHANGES`, the host agent triages the returned blocking findings (deduplicate, check current applicability, classify, fix only what is valid and applicable), implements only the applicable fixes, and runs normal repository QA. If triage produced no applicable fix, stop here and hand the still-open `REQUEST_CHANGES` review to the user or a maintainer instead of calling `submit` or re-running `review` on the unchanged head.

Otherwise, submit the complete workspace patch against the reviewed head:

```console
python3 skills/pr-review-loop/scripts/cli.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <REVIEWED_HEAD_SHA>
```

`submit` validates repository identity, exact local/remote head binding, conflicts, whitespace, staged content, credentials, repeated PR snapshots, and the remote branch lease. It creates one hook-free unsigned commit, pushes only with an explicit force-with-lease, and confirms the resulting GitHub PR head.

Run a fresh `review` after each successful submission. The host owns iteration and must stop on operational failure or the chosen iteration limit rather than manufacture approval.

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
