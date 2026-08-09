# Connector operations

This reference covers external ChatGPT/Oracle connector setup and smoke tests.
The host workflow is in [SKILL.md](../SKILL.md); deterministic CLI behavior is in
[command-contracts.md](command-contracts.md).

## ChatGPT-side connector preflight

Oracle browser mode drives ChatGPT over CDP. In an authorized disposable
`steipete/oracle` checkout, run `pnpm install`; its
`scripts/browser-tools.ts` helper is not part of the installed `oracle` CLI.

1. Connect and authorize GitHub in the Oracle browser profile. Use a test
   repository and never record cookies, tokens, or private account data.
2. On one CDP port, run
   `pnpm tsx scripts/browser-tools.ts inspect --ports <PORT> --json`.
3. Make the intended page the only tab, rerun `inspect`, and verify it. Pass
   `--port <PORT>` to `pnpm tsx scripts/browser-tools.ts pick ...` and
   `pnpm tsx scripts/browser-tools.ts eval ...`. If other tabs remain, attach
   DevTools/MCP to the inspected target ID and verify its URL or title. Select a
   real GitHub app token/chip; pasted `@GitHub` is ordinary text.
4. Submit a prompt requiring repository context outside the attachments and
   verify an actual app/tool invocation plus that context; prose is not evidence.

## End-to-end review smoke test

Issue #50 remains open until the actual review path is exercised:

1. Select GitHub in the same Oracle-controlled browser turn.
2. Run `review` on a test PR with its immutable snapshot, patch, changed files,
   and instruction files attached.
3. Verify actual app invocation and outside context, then verify the structured
   repository, PR, base SHA, head SHA, and published commit anchor.
4. Disconnect or unauthorize GitHub and repeat. If fallback is supported, the
   attachment-only review must complete; otherwise document the fail-closed
   Oracle/UI operational error.

Connector results remain supplemental and untrusted; they cannot replace attached
evidence, exact identity binding, or pr-review-loop publication.
