# Operations and cross-agent smoke tests

The runtime is identical for every supported host. Codex CLI and Cursor CLI use `.agents/skills/pr-review-loop`; Claude Code uses `.claude/skills/pr-review-loop`. Both discovery locations resolve to the canonical `skills/pr-review-loop` directory.

## Common flow

Choose an iteration limit before starting.

1. Run `review` on the exact current PR head.
2. Finish on `APPROVE`.
3. On `REQUEST_CHANGES`, triage `blocking_findings` (dedupe, check current applicability, classify as fix/already_addressed/outdated/clarify/defer), implement only the `fix` dispositions, and run repository QA.
4. If triage produced no `fix` disposition, stop here instead of calling `submit` or re-running `review` on the unchanged head. Report the dispositions; a formal GitHub `REQUEST_CHANGES` review can be handed to the user or a maintainer, while a self-authored `COMMENT` publication remains the commit-anchored audit of Oracle's canonical result.
5. Otherwise, submit the complete patch with `submit --pr <NUMBER_OR_URL> --expected-head <REVIEWED_HEAD_SHA>`.
6. Confirm `resulting_head_sha == commit_sha`.
7. Run a fresh `review` on that head and repeat only when another `REQUEST_CHANGES` is returned.

Operational errors are stop conditions. Do not reinterpret them as review verdicts.

## Issue bootstrap

`bootstrap --issue <NUMBER_OR_URL>` is an optional entry point for work that has no pull request yet; see `SKILL.md` for the full bootstrap-to-review handoff diagram. It uses ordinary GitHub CLI authentication and the same Oracle browser profile as `review`. Once the host has implemented the change and opened a pull request, proceed with the common flow above unchanged.

## Codex CLI smoke test

Confirm `.agents/skills/pr-review-loop` resolves to the canonical skill and execute the common flow without adding Codex-specific runtime code.

## Claude Code smoke test

Confirm `.claude/skills/pr-review-loop` resolves to the canonical skill and execute the same common flow without a Claude-specific wrapper.

## Cursor CLI smoke test

Use `.agents/skills/pr-review-loop` and execute the same common flow. Repository instructions may point to this skill but must not duplicate it.

## Review setup

`review` requires Oracle, Chrome/Chromium, an authenticated persistent Oracle browser profile, and the ordinary authenticated GitHub CLI session. It publishes Oracle's canonical verdict as a commit-anchored comment for self-authored PRs and as the corresponding formal event for other PRs. Initialize the browser profile when necessary:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

### ChatGPT-side connector preflight

Oracle browser mode drives ChatGPT over CDP, and Oracle's documented [browser-tools helper](https://github.com/steipete/oracle/blob/main/docs/manual-tests.md) provides `inspect`, `eval`, and an interactive `pick` command; its [browser-mode documentation](https://askoracle.sh/browser-mode.html) also describes the underlying CDP path. These tools can support a manual preflight of the ChatGPT account and composer, but the preflight is not an end-to-end `pr-review-loop review` smoke test.

Use an appropriate disposable or otherwise authorized test repository because a real `review` publishes a GitHub review, and treat the authenticated browser/CDP session as privileged:

1. Connect and authorize GitHub in the ChatGPT account used by the Oracle browser profile.
2. Choose a test prompt whose answer requires a known repository value outside the supplied changed files, such as an unchanged caller or related test. Do not record cookies, tokens, or private account data.
3. Keep the browser session available and choose one CDP port. Run Oracle's `pnpm tsx scripts/browser-tools.ts inspect --ports <PORT> --json` to identify the intended ChatGPT page. Before using browser-tools `pick` or `eval`, make that ChatGPT page the only open tab on the selected port, rerun `inspect`, and verify it is the sole listed tab; pass `--port <PORT>` to every subsequent `pnpm tsx scripts/browser-tools.ts pick ...` and `pnpm tsx scripts/browser-tools.ts eval ...` command. `inspect` alone does not bind those commands to an inspected target. If other tabs must remain open, do not use `pick` or `eval`; instead use a DevTools/MCP client that explicitly attaches to the inspected ChatGPT target ID and verify the attached page URL or title before interacting. On the deterministically targeted page, identify the composer app/mention control, open the picker, and select GitHub. The picker must render a real GitHub app token or chip; do not type or paste `@GitHub`, because literal text is not app selection.
4. Submit the small ChatGPT-side test prompt in the selected app turn and inspect the UI/tool trace for an actual GitHub app invocation plus the known outside context. A response that merely mentions GitHub or outside context is not evidence of invocation.

This preflight demonstrates that the account, ChatGPT UI, and CDP/browser tooling can perform the smallest app-selection interaction. It does not demonstrate that the `review` command coordinated that interaction, preserved the selected app through submission, or validated the resulting structured review.

### End-to-end Oracle review smoke test still required

Issue #50 remains open until the actual `review` path is exercised. Existing CDP/browser-tools coordination, operator-assisted orchestration, and upstream Oracle integration are possible implementation routes; no single route is inherently required. The end-to-end test must:

1. Select the GitHub app token in the same Oracle-controlled browser turn before the review prompt is submitted, using a reproducible coordination point.
2. Run `review` on a test PR with the exact attached snapshot, patch, changed-file contents, and instruction files.
3. Verify the ChatGPT UI records an actual GitHub app/tool invocation and that the returned review uses known repository context outside the attachments; a literal `@GitHub` mention or matching prose is insufficient.
4. Verify the structured `repository`, `pr_number`, `base_sha`, and `head_sha` exactly match the attached snapshot, and inspect the published review's commit anchor.
5. Disconnect or unauthorize GitHub and repeat the test. Where ChatGPT permits fallback, the attachment-only review must still complete; an Oracle/UI operational error remains fail-closed and must be documented as such.

Until this end-to-end smoke test succeeds, keep connector data supplemental and untrusted, keep the attached evidence and exact identity binding authoritative, and treat the deterministic attachment-only path as the only guaranteed runtime behavior.

## Recovery

Oracle inputs and outputs are private command-scoped temporary files; GitHub and Git remain the source of truth.

- `stale_head`, other stale-state failures, or lease loss: refresh the checkout and PR state, run a new review, and use the newly reviewed head.
- `empty_patch`: return to the review result; do not create an empty commit.
- authentication failure: verify that the ordinary `gh` session is logged in and can publish pull-request reviews.
- Oracle/schema failure: restore the browser/session or reviewer output; do not weaken schema validation.
- repository/origin mismatch: stop rather than redirect the patch.
- `bootstrap`'s `workspace` precondition (local `HEAD` is not the returned `base_sha`, or the checkout has uncommitted tracked or untracked changes): the current checkout already holds implementation work from a prior `bootstrap`/implement cycle, or has unrelated local changes or stray untracked files. Set those aside (commit, stash, discard, or switch away), return to a clean checkout at the repository's current default-branch tip, and rerun `bootstrap`; do not point it at an in-progress implementation branch.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
