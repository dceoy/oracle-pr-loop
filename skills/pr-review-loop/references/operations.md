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

GitHub connector use is opportunistic: Oracle's browser engine has no CLI flag or documented mechanism to select, activate, or verify that a GitHub connector/app is available to a given ChatGPT turn, unlike its dedicated Deep Research tool-menu activation. `review` cannot detect, require, or confirm connector use, so treat the prompt's connector permission as advisory only. To manually spot-check that a connected ChatGPT account is actually using it, run `review` on a PR whose correct assessment depends on repository context outside the attached snapshot (for example, a caller of a changed function that lives outside the diff) and confirm the returned `review_body` or `non_blocking_notes` reflects that outside context; treat an unconfirmed check as inconclusive, not as a failure, since the unchanged deterministic path is always correct on its own.

## Recovery

Oracle inputs and outputs are private command-scoped temporary files; GitHub and Git remain the source of truth.

- `stale_head`, other stale-state failures, or lease loss: refresh the checkout and PR state, run a new review, and use the newly reviewed head.
- `empty_patch`: return to the review result; do not create an empty commit.
- authentication failure: verify that the ordinary `gh` session is logged in and can publish pull-request reviews.
- Oracle/schema failure: restore the browser/session or reviewer output; do not weaken schema validation.
- repository/origin mismatch: stop rather than redirect the patch.
- `bootstrap`'s `workspace` precondition (local `HEAD` is not the returned `base_sha`, or the checkout has uncommitted tracked or untracked changes): the current checkout already holds implementation work from a prior `bootstrap`/implement cycle, or has unrelated local changes or stray untracked files. Set those aside (commit, stash, discard, or switch away), return to a clean checkout at the repository's current default-branch tip, and rerun `bootstrap`; do not point it at an in-progress implementation branch.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
