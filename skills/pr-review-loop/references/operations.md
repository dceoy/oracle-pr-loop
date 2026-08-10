# Connector operations

This document owns Oracle/ChatGPT setup and smoke-test procedures only. Host
sequencing is defined in [SKILL.md](../SKILL.md); deterministic CLI behavior is
defined in [command-contracts.md](command-contracts.md).

## ChatGPT-side preflight

Use the ChatGPT account attached to Oracle's persistent browser profile. Connect
and authorize GitHub when supplemental repository context is desired. Never
record cookies, tokens, or private account data in repository files or test
artifacts.

`pr-review-loop` does not manage ChatGPT OAuth/app installation and does not
implement a separate Oracle transport. It invokes the local `oracle` CLI; where
browser automation runs is Oracle configuration.

## Local Oracle browser

Initialize and sign in to the persistent browser profile once:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

Set `ORACLE_BROWSER_PROFILE_DIR` when the profile should live outside Oracle's
default location.

## Remote `oracle serve`

A host with only the local Oracle CLI may route browser work to a machine that
already has the authenticated browser profile. Prefer a loopback listener over
an SSH tunnel instead of exposing Oracle publicly.

On the browser machine:

```console
oracle serve --host 127.0.0.1 --port 9473
```

On the `pr-review-loop` host:

```console
ssh -N -L 9473:127.0.0.1:9473 user@browser-host &
export ORACLE_REMOTE_HOST=127.0.0.1:9473
export ORACLE_REMOTE_TOKEN='...'
oracle --engine browser --prompt "Reply with ready"
```

Oracle's own config-file equivalents are also supported. `pr-review-loop`
forwards `ORACLE_HOME_DIR`, `ORACLE_REMOTE_HOST`, and `ORACLE_REMOTE_TOKEN` and
uses the presence of Oracle's configured remote host only to avoid forcing
local manual-login behavior. Effective host/token precedence remains Oracle's
responsibility.

## Direct `@GitHub` review smoke test

Run this only with Oracle, the authenticated browser profile, and `gh` available:

1. Record one disposable PR's repository, number, base SHA, and head SHA from a
   single `gh pr view` snapshot. Choose a known repository fact outside the
   changed files.
2. Run
   `python3 skills/pr-review-loop/scripts/cli.py review --pr <PR>` while
   observing the Oracle-controlled ChatGPT conversation.
3. Verify the submitted prompt begins with `@GitHub`. If connector behavior is
   under test, verify an actual GitHub app/tool invocation retrieves the known
   outside fact; matching prose alone is not sufficient evidence.
4. Compare the command result's repository/PR/base/head identity with the
   frozen values from step 1 and confirm the GitHub review is anchored to the
   expected head.

The immutable attachments remain sufficient review evidence. Connector use is
supplemental and does not change the command's identity, schema, or publication
contract.

## Disconnected/unauthorized fallback smoke test

Disconnect or unauthorize GitHub in the same ChatGPT account and repeat the
review with a disposable PR:

1. Confirm no useful GitHub connector result is produced.
2. Where ChatGPT permits normal continuation, confirm `review` completes from
   its attached evidence with the unchanged result schema and commit anchor.
3. If ChatGPT or Oracle instead returns an operational error, confirm no review
   verdict is fabricated from that failure.

GitHub.com same-repository targets only are supported. Forks and GitHub
Enterprise are outside the command contract.
