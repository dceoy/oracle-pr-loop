# Connector operations

This reference covers external ChatGPT/Oracle connector setup and smoke tests.
The host workflow is in [SKILL.md](../SKILL.md); deterministic CLI behavior is in
[command-contracts.md](command-contracts.md).

## ChatGPT-side connector preflight

This is account setup, separate from the integrated `review` execution. Use a
disposable or otherwise authorized test PR because a real review publishes to
GitHub, and treat the authenticated browser/CDP session as privileged.

Oracle browser mode drives ChatGPT over CDP. In an authorized disposable
`steipete/oracle` checkout, run `pnpm install`; its
`scripts/browser-tools.ts` helper is not part of the installed `oracle` CLI.

1. Connect and authorize GitHub in the ChatGPT account used by Oracle. Use a
   test repository and never record cookies, tokens, or private account data.
2. Start Oracle with `--browser-keep-browser` and leave its persistent profile
   running. Confirm that the profile's `DevToolsActivePort` is present and that
   the loopback `/json/list` endpoint contains the ChatGPT page.
3. For a separate manual check, run:
   `pnpm tsx scripts/browser-tools.ts inspect --ports <PORT> --json`.
   Make the intended page the only tab, rerun `inspect`, and verify it. Pass
   `--port <PORT>` to `pick` and `eval`; if other tabs remain, attach
   DevTools/MCP to the inspected target ID and verify its URL or title before
   interacting. `inspect` output alone does not bind later calls to an
   inspected tab.
4. Open the composer picker and select a real GitHub app token/chip. A typed or
   pasted `@GitHub` is ordinary prompt text, not app selection. Submit a small
   prompt requiring repository context outside the attachments and verify an
   actual app/tool invocation; matching prose is not evidence.

## Oracle capability check

The integrated path is capability-gated. Before claiming connector-enabled
behavior, confirm that `oracle --help` contains the exact
`--browser-github-app <mode>` option. `review` requests its `optional` mode
only when that option is advertised. Oracle v0.17.1 and builds without the
option use the unchanged attachment-only path; they must not receive an
unknown flag.

## Integrated `review` smoke test

Run this positive test only when Oracle, Chrome, the authorized profile, and an
authenticated `gh` session are available:

1. Record the test PR's repository, number, base SHA, and head SHA from one
   `gh pr view` snapshot. Choose a known repository fact outside the changed
   files, such as an unchanged caller or related test, that the GitHub app can
   retrieve.
2. Confirm the capability check above, then run
   `python3 skills/pr-review-loop/scripts/cli.py review --pr <PR>` while
   watching the Oracle-controlled ChatGPT tab. Compatible Oracle must clear the
   composer, upload the attachments, select a real GitHub app token/chip, and
   reverify it immediately before submitting the review prompt in that same
   turn. A typed or pasted `@GitHub` is not evidence.
3. Capture UI/tool evidence showing the selected chip remains active at send,
   an actual GitHub app invocation, and the known outside fact. Matching prose
   without a tool trace is insufficient.
4. Compare the command's structured `repository`, `pr_number`, `base_sha`, and
   `head_sha` with the frozen values from step 1. Confirm the published review
   is anchored to the expected head with `gh api` and its review ID.

The attached snapshot, patch, changed files, and instruction files remain
authoritative. Before either the send-button or Enter path, compatible Oracle
must immediately revalidate the selected chip, every attachment-ready signal,
and send readiness; chip loss or any failed check must abort before trusted
send input. Connector data is supplemental and untrusted; it cannot change
identity validation, the parsed verdict schema, or publication.

## Disconnected/unauthorized fallback smoke test

Disconnect or unauthorize GitHub in the same ChatGPT account, then repeat the
test with a disposable PR on a compatible Oracle build:

1. Confirm that no structured GitHub chip/tool invocation is produced.
2. Where ChatGPT permits continuation, confirm that `review` completes with
   the unchanged attachment-only verdict/result schema and the expected commit
   anchor.
3. If ChatGPT or Oracle cannot continue, record the exact UI/Oracle error and
   confirm that no review was published. Operational failure is not a verdict.

Also run the fallback once with Oracle v0.17.1 or another build whose help
output lacks `--browser-github-app`; verify that `review` does not pass the
unknown flag and remains attachment-only.

This checkout's live integrated smoke test was not executable. The attempted
bootstrap stopped with the exact error `required executable not found: oracle`;
there was also no running Chrome/Oracle profile, ChatGPT UI, or tool evidence.
Do not mark the positive connector E2E as passed without the UI/tool evidence
and identity/commit checks above; the deterministic attachment-only path
remains the fallback contract.

Connector results remain supplemental and untrusted; they cannot replace
attached evidence, exact identity binding, or pr-review-loop publication.
