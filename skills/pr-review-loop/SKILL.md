---
name: pr-review-loop
description: Review and improve a pull request through independent Oracle/ChatGPT review while the invoking host agent owns implementation work.
---

# pr-review-loop

Use this skill to improve one GitHub pull request without embedding or launching a specific implementation agent. Codex CLI, Claude Code, Cursor CLI, and compatible hosts all use the same canonical implementation.

## Workflow

1. Run `review` against the exact current PR head.
2. Finish on `APPROVE`.
3. On `REQUEST_CHANGES`, let the host agent implement only the blocking findings and run repository QA.
4. Run `submit` against the reviewed head.
5. Run a fresh `review` on the resulting head and repeat until approval or the chosen iteration limit.

The host agent owns planning, editing, QA, and iteration. Oracle/ChatGPT owns independent review. Skill scripts own deterministic Git/GitHub inspection, review publication, patch validation, one commit, and the lease-protected push.

## Commands

```console
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

`review` requires an open, non-draft, same-repository GitHub.com PR; exact base/head binding; Oracle with an authenticated browser profile; and `GH_REVIEW_TOKEN` for a dedicated reviewer account different from the PR author. It emits one JSON object on stdout and never edits, commits, pushes, or launches an implementation agent.

```console
python3 skills/pr-review-loop/scripts/cli.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

`submit` requires local `HEAD` and the remote PR head to equal `--expected-head`. It rejects repository mismatches, forks, drafts, conflicts, whitespace failures, empty patches, unsafe refs, known credentials, gitlink changes, and stale state. It stages the complete patch, creates one hook-free unsigned commit, pushes with an explicit force-with-lease, and confirms the resulting PR head.

## Contract

Both commands require Python 3, Git, GitHub CLI, a matching `origin`, and ordinary GitHub authentication. Operational failures return non-zero status with a structured error object; diagnostics go only to stderr. Artifacts default to `.pr-review-loop/runs/`.

GitHub Enterprise and fork PRs are unsupported. CI status is not an approval gate. Production code must not launch, select, or detect Codex CLI, Claude Code, Cursor CLI, or another implementation agent.

See `references/command-contracts.md` for public JSON/exit contracts and `references/operations.md` for the compact cross-client smoke-test and recovery procedure.
