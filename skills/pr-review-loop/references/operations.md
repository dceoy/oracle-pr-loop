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
   `head_sha` with the frozen values from step 1. Confirm the published review
   is anchored to the expected head with `gh api` and its review ID.

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
