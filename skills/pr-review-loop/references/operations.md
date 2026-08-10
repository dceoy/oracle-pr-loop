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
implement a separate Oracle transport. It invokes the local `oracle` CLI only.
Oracle remains responsible for its own browser automation and configuration
grammar.

For optional diagnostics against an authorized Oracle checkout,
`scripts/browser-tools.ts` may inspect the active CDP endpoint. Pass the
observed browser port explicitly with `--port`. Production `review` does not
depend on this diagnostic path.

## Local Oracle browser

Initialize and sign in to the persistent browser profile once:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The skill preserves `HOME`, so Oracle's normal persistent manual-login profile
continues to work. Set `ORACLE_BROWSER_PROFILE_DIR` when that profile should
live outside Oracle's default location.

`pr-review-loop` deliberately does not load the account-level
`~/.oracle/config.json` (or an inherited `ORACLE_HOME_DIR/config.json`) during
its Oracle invocation. This is the security boundary that prevents an
undiscoverable config-only remote token from outranking the environment values
the skill can register for redaction. Consequently, account-level browser
defaults such as a custom ChatGPT URL, Chrome path, timeout, or model strategy
must be supplied through the corresponding supported Oracle environment/CLI
interface when the skill needs them. The persistent manual-login profile itself
remains HOME-based and is not moved by this isolation.

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
python3 skills/pr-review-loop/scripts/cli.py review --pr <PR>
```

For `pr-review-loop`, remote host and token discovery is intentionally limited
to `ORACLE_REMOTE_HOST` and `ORACLE_REMOTE_TOKEN`. Config-only
`browser.remoteHost` / `browser.remoteToken` are not supported by the skill.
This boundary avoids reimplementing Oracle's JSON5 grammar or configuration
precedence and guarantees that every effective remote token is known to the
skill before Oracle runs, so it is registered for redaction and checked against
temporary attachments and structured output. The token is passed only through
the Oracle process environment, never through argv.

Each Oracle invocation receives a private, command-scoped `ORACLE_HOME_DIR`.
That prevents account-level Oracle config from silently supplying a
higher-priority remote host/token that the skill could not safely discover.
`HOME` and the optional `ORACLE_BROWSER_PROFILE_DIR` remain available,
preserving local authenticated-browser operation. Safe project configuration,
where Oracle allows it, is still discovered and parsed by Oracle itself; the
skill does not parse or emulate Oracle configuration files.

Direct Oracle invocations outside `pr-review-loop` may support additional
Oracle-native configuration sources. Those sources are outside this skill's
credential-redaction and transport contract.

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
   frozen values from step 1 and confirm the GitHub review's commit anchor is
   the expected head.

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
