---
name: oracle-pr-review
description: Review one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, publishing a COMMENT review with inline findings when possible.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Review

Review exactly one pull request through Oracle browser mode and publish the review through ChatGPT's connected GitHub app. Oracle owns browser/session routing; ChatGPT owns repository context and GitHub review publication.

## Invariants

- Require `oracle` in `PATH`, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`. Require `gh` only to detect an omitted target and to perform the exact-marker timeout recovery defined below.
- Accept `OWNER/REPO#NUMBER`, exactly `https://github.com/OWNER/REPO/pull/NUMBER`, or no target. For no target, run `gh pr view --json url --jq .url` once.
- Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`. Reject ambiguity, query strings/fragments, extra prose, whitespace/newlines, shell metacharacters, or unvalidated `gh` output.
- Use the connected GitHub app as the sole review-context and publication path. Outside timeout recovery, use `gh` only for PR identity.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Never use `eval` or append caller prose or local repository context to the prompt.
- Fail closed: no API-engine fallback, alternate model/PR/context source, local review substitute, or modified retry prompt.

## Run

Before the first Oracle attempt, create the stdout and stderr capture files with separate `mktemp` calls. Derive one per-run `review_token` from their random suffixes, validate it against `^[A-Za-z0-9_-]+$`, and define the exact correlation marker `<!-- oracle-pr-review:REVIEW_TOKEN -->`. Reuse the same files, token, marker, and prompt for every busy retry. The token is correlation data only; it must not encode caller prose or repository context.

Invoke:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Review this pull request and publish exactly one GitHub pull-request review before answering. Use COMMENT, always include a non-empty top-level body, and prefer inline comments for safely line-anchored findings. If there are no actionable findings, say so in the COMMENT review. Apply KISS, DRY, and YAGNI to concrete maintainability issues; avoid style-only findings. Append the exact HTML comment <!-- oracle-pr-review:REVIEW_TOKEN --> to the top-level review body. If publication fails, report failure rather than an unposted review. After confirmed publication, state that the review was posted and emit the exact final plain-text line ORACLE_PR_REVIEW_PUBLISHED.'
```

Substitute only the validated canonical PR target and validated per-run review token. The stdout publication marker is valid only after the connected GitHub app confirms publication. The HTML correlation marker exists only to prove that a specific timed-out invocation already published its review.

## Retry and result contract

Capture stdout and stderr separately in the private temporary files created above and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter.

Bound the complete leaf execution, including busy retries and any timeout-recovery polling, by a finite deadline supplied by the caller or guaranteed by the runtime. The leaf need not know a runtime-enforced deadline's concrete value, but must not invent, shorten, or override the bound. If neither source guarantees a finite bound, fail closed before the first Oracle invocation.

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. While the caller/runtime deadline remains live, retry the identical prompt after nominal delays `1, 2, 4, 8, 16` seconds, then `30` seconds for each subsequent retry, with 0.750–1.000 jitter. Do not impose an independent retry-count or elapsed-time cap. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal for Oracle transport and never replay it. Instead, poll read-only with `gh api --paginate` against that PR's review collection while the same caller/runtime deadline remains live. Accept recovered publication as soon as exactly one persisted review has state `COMMENTED` and its top-level body contains the exact per-run correlation marker. If zero reviews match, keep polling after a short bounded sleep; do not create a separate recovery deadline. If more than one review matches or a GitHub read fails, keep publication state **indeterminate** and fail closed. The exact marker is positive proof of publication, not a heuristic. Do not use review counts, timestamps, reviewer identity, partial-marker matches, stdout content, or absence of the marker to infer publication or non-publication. If the caller/runtime deadline expires before one exact match is proven, remain **indeterminate** and stop.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

Accept success in either of two cases: Oracle exits zero and stdout's final nonblank line is exactly `ORACLE_PR_REVIEW_PUBLISHED`; or Oracle ends with the exact `read ETIMEDOUT` form above and exact-marker GitHub recovery proves one `COMMENTED` review. On normal success, return Oracle's review without rewriting its findings. On recovered success, do not reconstruct findings from partial stdout; report the recovered review identity and let GitHub remain the durable handoff to feedback triage.
