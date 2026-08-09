# Connector operations

This reference covers external ChatGPT/Oracle connector setup and smoke tests.
The host workflow is in [SKILL.md](../SKILL.md); deterministic CLI behavior is in
[command-contracts.md](command-contracts.md).

## ChatGPT-side connector preflight

Connect and authorize GitHub in the ChatGPT account used by Oracle's persistent
browser profile. Use a test repository and never record cookies, tokens, or
private account data.

No upstream Oracle connector capability is required. `review` uses Oracle's
normal browser invocation and submits a prompt whose first line is `@GitHub`.
The ChatGPT account owns app connection and authorization; `pr-review-loop`
does not manage OAuth or app installation.

For optional manual observation only, an authorized `steipete/oracle` checkout
can use `scripts/browser-tools.ts`. Run its inspect helper against the active
CDP endpoint and pass the inspected browser port explicitly with `--port` to
subsequent diagnostic commands. This tooling can help confirm which ChatGPT tab
is active, but production `review` does not depend on it or use it to preselect
the GitHub app.

## Direct `@GitHub` review smoke test

Run the positive test only when Oracle, Chrome, the authorized ChatGPT profile,
and an authenticated `gh` session are available:

1. Record the test PR's repository, number, base SHA, and head SHA from one
   `gh pr view` snapshot. Choose a known repository fact outside the changed
   files, such as an unchanged caller or related test.
2. Run
   `python3 skills/pr-review-loop/scripts/cli.py review --pr <PR>` while
   observing the Oracle-controlled ChatGPT conversation. Verify that the exact
   submitted review prompt starts with `@GitHub`; no `--browser-github-app`
   option, capability probe, or upstream Oracle modification is part of this
   path.
3. Verify an actual GitHub app/tool invocation retrieves the known outside fact.
   Matching prose without connector/tool evidence is not sufficient to claim
   the positive connector smoke test passed.
4. Compare the command's structured `repository`, `pr_number`, `base_sha`, and
   `head_sha` with the frozen values from step 1. Confirm the published review's
   commit anchor matches the expected head with `gh api` and its review ID.

The attached snapshot, patch, changed files, and instruction files remain the
mandatory, authoritative evidence. Connector data is supplemental and untrusted;
it cannot change identity validation, the parsed verdict schema, or review
publication.

## Disconnected/unauthorized fallback smoke test

Disconnect or unauthorize GitHub in the same ChatGPT account, then repeat the
review with a disposable PR:

1. Confirm that no useful GitHub connector result is produced.
2. Where ChatGPT permits continuation, confirm that `review` completes using
   the attached evidence with the unchanged verdict/result schema and expected
   commit anchor.
3. If ChatGPT or Oracle instead returns an operational error, confirm that no
   review verdict is fabricated from that failure and no stale review is
   published.

Connector results remain supplemental and untrusted; they cannot replace
attached evidence, exact identity binding, or `pr-review-loop` publication.

`bootstrap --issue <NUMBER_OR_URL>` is an optional entry point for work that has no pull request yet; see `SKILL.md` for the full bootstrap-to-review handoff diagram. It uses ordinary GitHub CLI authentication and the same configured Oracle CLI as `review` (see "Review setup" below). Once the host has implemented the change and opened a pull request, proceed with the common flow above unchanged.

## Codex CLI smoke test

Confirm `.agents/skills/pr-review-loop` resolves to the canonical skill and execute the common flow without adding Codex-specific runtime code.

## Claude Code smoke test

Confirm `.claude/skills/pr-review-loop` resolves to the canonical skill and execute the same common flow without a Claude-specific wrapper.

## Cursor CLI smoke test

Use `.agents/skills/pr-review-loop` and execute the same common flow. Repository instructions may point to this skill but must not duplicate it.

## Review setup

`review` requires a configured Oracle CLI and the ordinary authenticated GitHub CLI session. It publishes Oracle's canonical verdict as a commit-anchored comment for self-authored PRs and as the corresponding formal event for other PRs. `pr-review-loop` only ever invokes the local `oracle` CLI (`--engine browser`, a thinking-time budget, and the output contract); it never adds a custom transport, and where Oracle's browser work actually runs is entirely Oracle's own configuration, in one of two supported ways.

### Local Oracle browser

`pr-review-loop` no longer passes `--browser-manual-login` on every Oracle invocation, so local-browser hosts must set `browser.manualLogin: true` in `~/.oracle/config.json` (or `ORACLE_BROWSER_PROFILE_DIR` to relocate the profile); without it, Oracle falls back to its zero-config temporary-profile launcher mode and `bootstrap`/`review` cannot reuse a signed-in session. Existing local-browser setups upgrading past this change must add that config key once. Initialize the persistent profile once, signing in when Chrome opens:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

GitHub connector use is opportunistic: Oracle's browser engine has no CLI flag or documented mechanism to select, activate, or verify that a GitHub connector/app is available to a given ChatGPT turn, unlike its dedicated Deep Research tool-menu activation. `review` cannot detect, require, or confirm connector use, so treat the prompt's connector permission as advisory only. To manually spot-check that a connected ChatGPT account is actually using it, run `review` on a PR whose correct assessment depends on repository context outside the attached snapshot (for example, a caller of a changed function that lives outside the diff) and confirm the returned `review_body` or `non_blocking_notes` reflects that outside context; treat an unconfirmed check as inconclusive, not as a failure, since the unchanged deterministic path is always correct on its own.

### Remote `oracle serve` instance

Run a host with only the local Oracle CLI installed against a remote machine that already has a signed-in Chrome/ChatGPT session, without a local Chrome/Chromium session on the host itself. Prefer a loopback address reached over an SSH tunnel rather than exposing `oracle serve` on a public listener.

On the machine with the signed-in browser:

```console
oracle serve --host 127.0.0.1 --port 9473
```

On the `pr-review-loop` host, forward that port over SSH and point the local Oracle CLI at it:

```console
ssh -N -L 9473:127.0.0.1:9473 user@browser-host &
export ORACLE_REMOTE_HOST=127.0.0.1:9473
export ORACLE_REMOTE_TOKEN='...'  # token printed by `oracle serve`; it rotates on restart unless `--token` is fixed
oracle --engine browser --prompt "Reply with ready"
```

`pr-review-loop` forwards `ORACLE_HOME_DIR`, `ORACLE_REMOTE_HOST`, and `ORACLE_REMOTE_TOKEN` from its own process environment to every Oracle invocation, so setting them once in the host's environment (or persisting `browser.remoteHost`/`browser.remoteToken` in `~/.oracle/config.json`, for example via `oracle bridge client --write-config`) is enough; `bootstrap` and `review` never add `--remote-host`/`--remote-token` flags or any other custom Oracle transport. `ORACLE_REMOTE_TOKEN` is redacted from logs and rejected in attachments and patches by the same credential safeguards as any other known secret.

## Recovery

Oracle inputs and outputs are private command-scoped temporary files; GitHub and Git remain the source of truth.

- `stale_head`, other stale-state failures, or lease loss: refresh the checkout and PR state, run a new review, and use the newly reviewed head.
- `empty_patch`: return to the review result; do not create an empty commit.
- authentication failure: verify that the ordinary `gh` session is logged in and can publish pull-request reviews.
- Oracle/schema failure: restore the browser/session or reviewer output; do not weaken schema validation.
- repository/origin mismatch: stop rather than redirect the patch.
- `bootstrap`'s `workspace` precondition (local `HEAD` is not the returned `base_sha`, or the checkout has uncommitted tracked or untracked changes): the current checkout already holds implementation work from a prior `bootstrap`/implement cycle, or has unrelated local changes or stray untracked files. Set those aside (commit, stash, discard, or switch away), return to a clean checkout at the repository's current default-branch tip, and rerun `bootstrap`; do not point it at an in-progress implementation branch.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
