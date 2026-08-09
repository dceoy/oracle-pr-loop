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

The current Oracle browser command submits ordinary prompt text and files; it has no browser-control or app-selection/invocation-inspection interface. A pasted `@GitHub` is therefore only literal text, not evidence that ChatGPT selected the GitHub app. The guaranteed `review` path remains attachment-only. Do not claim connector support from a response that merely mentions GitHub or outside context.

To perform a manual connector smoke test in an Oracle-enabled environment, use a disposable or otherwise appropriate test PR because `review` publishes a GitHub review:

1. Connect and authorize GitHub in the ChatGPT account used by Oracle's persistent browser profile.
2. Choose a test PR whose correct assessment requires a known repository value outside the attached changed files, such as an unchanged caller or related test.
3. In the ChatGPT composer, open the app/mention picker and select GitHub so the composer renders a real GitHub app token or chip. Do not type or paste `@GitHub` as plain text; that does not select the app.
4. Submit the review prompt and attachments in the selected app turn, then verify the ChatGPT UI records an actual GitHub app/tool invocation and that the returned review uses the known outside context. A response that merely mentions GitHub is not evidence of invocation.
5. Verify the returned `repository`, `pr_number`, `base_sha`, and `head_sha` exactly match the attached snapshot and inspect the published review's commit anchor.
6. Disconnect or unauthorize GitHub and repeat the test. Where ChatGPT permits fallback, the attachment-only review must still complete; an Oracle/UI operational error must remain fail-closed and be documented as such.

The current Oracle command cannot perform steps 3–4 or inspect their result. Automating or verifying this path requires an upstream Oracle browser-control capability; until that exists and a live smoke test succeeds, Issue #50's connector-invocation acceptance criterion remains open.

## Recovery

Oracle inputs and outputs are private command-scoped temporary files; GitHub and Git remain the source of truth.

- `stale_head`, other stale-state failures, or lease loss: refresh the checkout and PR state, run a new review, and use the newly reviewed head.
- `empty_patch`: return to the review result; do not create an empty commit.
- authentication failure: verify that the ordinary `gh` session is logged in and can publish pull-request reviews.
- Oracle/schema failure: restore the browser/session or reviewer output; do not weaken schema validation.
- repository/origin mismatch: stop rather than redirect the patch.
- `bootstrap`'s `workspace` precondition (local `HEAD` is not the returned `base_sha`, or the checkout has uncommitted tracked or untracked changes): the current checkout already holds implementation work from a prior `bootstrap`/implement cycle, or has unrelated local changes or stray untracked files. Set those aside (commit, stash, discard, or switch away), return to a clean checkout at the repository's current default-branch tip, and rerun `bootstrap`; do not point it at an in-progress implementation branch.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
