# Operations and cross-agent smoke tests

This reference describes the manual end-to-end workflow for the canonical `loopr`
skill. The runtime is identical for every supported host; only the discovery path
used by the host differs.

## Supported host discovery

| Host | Discovery path | Client-specific requirement |
| --- | --- | --- |
| Codex CLI | `.agents/skills/loopr` | Invoke the repository skill named `loopr`. |
| Claude Code | `.claude/skills/loopr` | Invoke the repository skill named `loopr`. |
| Cursor CLI | `.agents/skills/loopr` | If repository instructions are needed for discovery, point them at the existing `loopr` skill instead of copying or wrapping it. |

Both discovery links resolve to `skills/loopr`. Do not create a client-specific
runtime, wrapper, or fork of the skill.

## Prerequisites

Before a manual smoke test, verify all of the following:

- Python 3.10 or newer and Git are available.
- GitHub CLI is authenticated for ordinary pull-request reads.
- `origin` identifies the same GitHub.com repository as the target pull request.
- The pull request is open, non-draft, and same-repository.
- The local checkout is on the exact pull-request head that will be reviewed.
- The host has local push access and a configured Git commit identity.
- Oracle is installed and Chrome or Chromium is available.
- Oracle has a persistent browser profile authenticated to ChatGPT.
- `GH_REVIEW_TOKEN` belongs to a dedicated reviewer account that is different
  from the pull-request author and can write pull-request reviews.

Initialize Oracle's browser profile once before the first review:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The default profile is `~/.oracle/browser-profile`. Set
`ORACLE_BROWSER_PROFILE_DIR` when a different persistent profile is required.

## Common acceptance flow

Choose the iteration limit before starting. A manual smoke test should normally
use a small limit such as 3. The host agent owns this loop; `loopr` itself does
not launch or manage an implementation agent.

1. Discover and invoke the canonical `loopr` skill through the host-specific
   discovery path above.
2. Run a review against the exact current pull-request head:

   ```console
   python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
   ```

3. Inspect the single JSON object on stdout.
   - `APPROVE` is a successful terminal result.
   - `REQUEST_CHANGES` is also a successful command result. The host implements
     only the returned `blocking_findings`, using `implementation_prompt` as
     reviewer guidance rather than as an instruction to launch another agent.
   - An `error` object is an operational failure; stop and resolve it before
     editing or submitting.
4. After `REQUEST_CHANGES`, let the host agent edit the current checkout and run
   the repository's normal QA workflow. `loopr` does not select or run QA.
5. Submit the complete intended workspace patch against the reviewed head:

   ```console
   python3 skills/loopr/scripts/loopr.py submit \
     --pr <NUMBER_OR_URL> \
     --expected-head <REVIEWED_HEAD_SHA>
   ```

6. Confirm that `submit` reports one new `resulting_head_sha` and that it equals
   `commit_sha`.
7. Run a fresh `review` against the resulting head.
8. Finish on `APPROVE`. On another `REQUEST_CHANGES`, repeat from step 4 until
   approval or the chosen iteration limit. If the limit is reached, stop without
   manufacturing an approval.

A valid verdict exits with status `0`, including `REQUEST_CHANGES`. Input,
Oracle/schema, GitHub/write, and stale-state failures use non-zero statuses. See
`command-contracts.md` for the normative JSON fields and exit classes.

## Codex CLI smoke test

1. Open the repository from its pull-request head.
2. Confirm `.agents/skills/loopr` resolves to `skills/loopr`.
3. Ask Codex CLI to use the repository `loopr` skill for the target pull request.
4. Execute the common acceptance flow without adding Codex-specific scripts or
   runtime branches.
5. Pass when the flow reaches a fresh `APPROVE`, or record an iteration-limit
   stop as an incomplete smoke test rather than success.

## Claude Code smoke test

1. Open the repository from its pull-request head.
2. Confirm `.claude/skills/loopr` resolves to `skills/loopr`.
3. Ask Claude Code to use the repository `loopr` skill for the target pull
   request.
4. Execute the common acceptance flow without adding Claude-specific scripts or
   runtime branches.
5. Pass when the flow reaches a fresh `APPROVE`, or record an iteration-limit
   stop as incomplete.

## Cursor CLI smoke test

1. Open the repository from its pull-request head.
2. Confirm `.agents/skills/loopr` resolves to `skills/loopr`.
3. Ask Cursor CLI to use the repository `loopr` skill. If the local Cursor setup
   requires repository instructions to surface the skill, reference the existing
   `.agents/skills/loopr` path there; do not duplicate the skill.
4. Execute the common acceptance flow without adding Cursor-specific production
   code.
5. Pass when the flow reaches a fresh `APPROVE`, or record an iteration-limit
   stop as incomplete.

## Audit artifacts

Each command creates a private run directory below `.pr-loopr/runs/` by default.
Review artifacts capture the frozen pull-request snapshot, evidence bundle,
validated Oracle result, posted review metadata, and final result. Submit
artifacts capture the validated staged patch, commit metadata, push metadata,
and final result.

Treat artifacts as diagnostic evidence, not as a second source of truth for the
current pull-request head. Re-read GitHub state by running a fresh command after
any race or manual intervention.

## Troubleshooting

### Oracle cannot produce a review

Re-run the manual-login command and confirm the persistent browser profile is
usable. Do not weaken Oracle schema validation or repair malformed output.

### Reviewer identity is rejected

Confirm `GH_REVIEW_TOKEN` belongs to the dedicated reviewer and not to the
pull-request author. Keep reviewer credentials out of Oracle input and repository
files.

### `stale_head`, another stale-state error, or lease loss

Do not force the previous result through. Refresh the checkout and pull-request
state, run a new `review`, and use the newly reviewed head as the next
`--expected-head`.

### `empty_patch`

The host made no submit-worthy workspace change. Do not create an empty commit;
return to the review result and determine whether a blocking finding still needs
implementation.

### Repository or origin mismatch

Stop. `submit` intentionally refuses to redirect a patch to a repository other
than the pull request's same-repository head.

## Known limitations

- GitHub.com only; GitHub Enterprise is not supported.
- Fork pull requests are not supported.
- CI status is not an approval gate.
- Review posting is aggregate rather than inline.
- The host agent, not `loopr`, owns editing, repository QA, iteration count, and
  interpretation of non-blocking notes.
- `loopr` does not sandbox or contain the host agent and does not launch Codex,
  Claude Code, Cursor CLI, or another implementation agent.
