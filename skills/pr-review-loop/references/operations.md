# Operations and cross-agent smoke tests

The runtime is identical for every supported host. Codex CLI and Cursor CLI use `.agents/skills/pr-review-loop`; Claude Code uses `.claude/skills/pr-review-loop`. Both discovery locations resolve to the canonical `skills/pr-review-loop` directory.

## Common flow

Choose an iteration limit before starting.

1. Run `review` on the exact current PR head.
2. Finish on `APPROVE`.
3. On `REQUEST_CHANGES`, let the host implement only `blocking_findings` and run repository QA.
4. Submit the complete patch with `submit --pr <NUMBER_OR_URL> --expected-head <REVIEWED_HEAD_SHA>`.
5. Confirm `resulting_head_sha == commit_sha`.
6. Run a fresh `review` on that head and repeat only when another `REQUEST_CHANGES` is returned.

Operational errors are stop conditions. Do not reinterpret them as review verdicts.

## Codex CLI smoke test

Confirm `.agents/skills/pr-review-loop` resolves to the canonical skill and execute the common flow without adding Codex-specific runtime code.

## Claude Code smoke test

Confirm `.claude/skills/pr-review-loop` resolves to the canonical skill and execute the same common flow without a Claude-specific wrapper.

## Cursor CLI smoke test

Use `.agents/skills/pr-review-loop` and execute the same common flow. Repository instructions may point to this skill but must not duplicate it.

## Reviewer setup

`review` requires Oracle, Chrome/Chromium, an authenticated persistent Oracle browser profile, and `GH_REVIEW_TOKEN` for a dedicated reviewer account distinct from the PR author. Initialize the browser profile when necessary:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

## Recovery

Artifacts are private diagnostic evidence under `.pr-review-loop/runs/`; GitHub remains the source of truth.

- `stale_head`, other stale-state failures, or lease loss: refresh the checkout and PR state, run a new review, and use the newly reviewed head.
- `empty_patch`: return to the review result; do not create an empty commit.
- reviewer identity failure: verify `GH_REVIEW_TOKEN` belongs to the dedicated reviewer.
- Oracle/schema failure: restore the browser/session or reviewer output; do not weaken schema validation.
- repository/origin mismatch: stop rather than redirect the patch.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
