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

## Review setup

`review` requires a configured Oracle CLI and the ordinary authenticated GitHub CLI session. It publishes Oracle's canonical verdict as a commit-anchored comment for self-authored PRs and as the corresponding formal event for other PRs. `pr-review-loop` only ever invokes the local `oracle` CLI (`--engine browser`, a thinking-time budget, and the output contract); it never adds a custom transport, and where Oracle's browser work actually runs is entirely Oracle's own configuration, in one of two supported ways.

### Local Oracle browser

`pr-review-loop` still passes `--browser-manual-login` on every Oracle invocation unless `ORACLE_REMOTE_HOST` is set in its process environment or Oracle's own config file declares `browser.remoteHost` (see below), so existing local-browser hosts with neither set keep reusing their persistent authenticated profile with no config changes required; set `ORACLE_BROWSER_PROFILE_DIR` to relocate that profile. Initialize the persistent profile once, signing in when Chrome opens:

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

`pr-review-loop` forwards `ORACLE_HOME_DIR`, `ORACLE_REMOTE_HOST`, and `ORACLE_REMOTE_TOKEN` to every Oracle invocation, and treats either an exported `ORACLE_REMOTE_HOST` or a config-declared `browser.remoteHost` as its supported signal for remote transport; `bootstrap` and `review` never add `--remote-host`/`--remote-token` flags or any other custom Oracle transport. Export both variables in the environment `pr-review-loop` runs in for the SSH-tunnel setup above, or rely on Oracle's config file alone. `pr-review-loop` also reads `$ORACLE_HOME_DIR/config.json` when `ORACLE_HOME_DIR` is set, otherwise `$HOME/.oracle/config.json` — the same file Oracle itself consults ahead of these environment variables — so a config-declared `browser.remoteToken` is registered for redaction/rejection, and a config-only `browser.remoteHost` selects remote mode just as it does for Oracle itself; `bootstrap`/`review` refuse to run only when both the config and the exported `ORACLE_REMOTE_HOST` declare a host and they disagree, rather than silently letting one override the other. The dependency-free config reader accepts Oracle's JSON5 syntax, including comments, trailing commas, unquoted keys, single-quoted strings, and extended numeric forms. If a malformed config still contains a member key resolving to `remoteHost` or `remoteToken` (including an escaped spelling), `bootstrap`/`review` refuse to run rather than risk missing a config-declared remote host or token; malformed local-only settings retain Oracle's empty-config fallback.

GitHub.com same-repository PRs only. Forks and GitHub Enterprise are unsupported; CI status is not an approval gate.
