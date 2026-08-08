---
name: pr-review-loop
description: Review and improve a pull request through independent Oracle/ChatGPT review while the invoking host agent owns implementation work; optionally bootstrap that work from an open GitHub Issue.
---

# pr-review-loop

Use this skill to improve one GitHub pull request without embedding or launching a specific implementation agent. Codex CLI, Claude Code, Cursor CLI, and compatible hosts all use the same canonical implementation.

## Starting from a GitHub Issue

`bootstrap` is a thin entry point for work that has no pull request yet. It reads one open Issue, asks Oracle/ChatGPT to turn that Issue and bounded repository evidence into an implementation-ready prompt, and returns the prompt to the host. It never implements the change, and it never creates a pull request.

```text
bootstrap --issue
        ↓
implementation_prompt
        ↓
host agent implements + runs repository QA + commits/pushes + opens a PR (e.g. "Fixes #123")
        ↓
review --pr
        ↓
existing pr-review-loop workflow below, unchanged
```

Once the host has opened the pull request, hand off completely to the workflow below; `review` and `submit` have no Issue-specific behavior and no persistent state connects them to `bootstrap`. `bootstrap` writes artifacts under `.pr-review-loop/runs/` before that first commit exists, so ensure `.pr-review-loop/` is excluded from the host's implementation commit (add it to `.gitignore` first if the repository does not already exclude it); `submit` later refuses to run if the artifact directory is tracked.

## Workflow

1. Run `review` against the exact current PR head.
2. Finish on `APPROVE`.
3. On `REQUEST_CHANGES`, triage the blocking findings (below), implement only what triage marks `fix`, and run repository QA.
4. Run `submit` against the reviewed head, but only when triage produced a real patch.
5. Run a fresh `review` on the resulting head and repeat until approval or the chosen iteration limit.

The host agent owns planning, triage, editing, QA, and iteration. Oracle/ChatGPT owns independent review. Skill scripts own deterministic Git/GitHub inspection, review publication, patch validation, one commit, and the lease-protected push.

## Triaging blocking findings

`review` returns `blocking_findings` as an array of `{id, title, description, required_change}` plus one `implementation_prompt`. Do not implement every finding verbatim; triage first.

For every finding:

1. Compare it against the exact reviewed `head_sha` and the current code, not a stale mental model of the diff.
2. Deduplicate findings that describe the same underlying defect (merge by matching `id` first, then by matching file/behavior); track one disposition per distinct defect even if several findings named it.
3. Classify the distinct finding as exactly one of:
   - `fix` — valid and applicable; implement the smallest sufficient change.
   - `already_addressed` — current code already satisfies the requested behavior; note the evidence (file/line or behavior) rather than editing.
   - `outdated` — the referenced problem no longer exists at the reviewed head; note why.
   - `clarify` — the requested change is ambiguous, contradictory, or needs information the host does not have.
   - `defer` — the concern is valid but out of scope for this PR, or cannot be safely addressed now.
4. Edit code only for `fix` findings, and keep every edit scoped to blocking findings — no incidental cleanup.
5. Run normal repository QA after edits.
6. Never fabricate a fix or manufacture an approval for `clarify` or `defer` findings; leave them for the user or a follow-up.
7. Call `submit` only when triage produced at least one real workspace patch to submit.
8. After a successful `submit`, run a fresh `review` before deciding the PR is done.
9. If triage produced no `fix` disposition — every blocking finding resolved to `already_addressed`, `outdated`, `clarify`, or `defer` — stop the loop instead of calling `submit` or re-running `review` on the unchanged head. Report each disposition with its evidence and hand the still-open `REQUEST_CHANGES` review to the user or a maintainer to dismiss or override; this skill never dismisses or overrides a review on the host's behalf.

## Commands

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

`bootstrap` requires an open, same-repository GitHub Issue and Oracle with an authenticated browser profile; it does not require `GH_REVIEW_TOKEN`. It emits one JSON object bound to the Issue's `updatedAt` and the base branch's exact commit SHA, and never edits, commits, pushes, or creates a pull request.

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

All three commands require Python 3, Git, GitHub CLI, a matching `origin`, and ordinary GitHub authentication. Operational failures return non-zero status with a structured error object; diagnostics go only to stderr. Artifacts default to `.pr-review-loop/runs/`.

GitHub Enterprise and fork PRs are unsupported. CI status is not an approval gate. Production code must not launch, select, or detect Codex CLI, Claude Code, Cursor CLI, or another implementation agent.

See `references/command-contracts.md` for public JSON/exit contracts and `references/operations.md` for the compact cross-client smoke-test and recovery procedure.
