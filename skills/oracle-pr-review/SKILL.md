---
name: oracle-pr-review
description: Review one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, publishing a COMMENT review with inline findings when possible.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Review

Review exactly one pull request through Oracle browser mode and publish the review through ChatGPT's connected GitHub app. Oracle owns browser/session routing; ChatGPT owns repository context and GitHub review publication.

## Invariants

- Require `oracle` in `PATH`, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`. Require `gh` only to detect an omitted target.
- Accept `OWNER/REPO#NUMBER`, exactly `https://github.com/OWNER/REPO/pull/NUMBER`, or no target. For no target, run `gh pr view --json url --jq .url` once.
- Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`. Reject ambiguity, query strings/fragments, extra prose, whitespace/newlines, shell metacharacters, or unvalidated `gh` output.
- Use `gh` only for PR identity. The connected GitHub app is the sole review-context and publication path.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Never use `eval` or append caller prose or local repository context to the prompt.
- Fail closed: no API-engine fallback, alternate model/PR/context source, local review substitute, or modified retry prompt.

## Run

Invoke:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Review this pull request and publish exactly one GitHub pull-request review before answering. Use COMMENT, always include a non-empty top-level body, and prefer inline comments for safely line-anchored findings. If there are no actionable findings, say so in the COMMENT review. Apply KISS, DRY, and YAGNI to concrete maintainability issues; avoid style-only findings. If publication fails, report failure rather than an unposted review. After confirmed publication, state that the review was posted and emit the exact final plain-text line ORACLE_PR_REVIEW_PUBLISHED.'
```

Substitute only the validated canonical PR target. The marker is valid only after the connected GitHub app confirms publication.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the repository and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter.

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. Allow six retries after the initial attempt, using nominal delays `1, 2, 4, 8, 16, 30` seconds with 0.750–1.000 jitter. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal with publication state **indeterminate**: the review may already have been posted. Never replay either form, and never use `gh`, GitHub API heuristics, review counts, timestamps, or a captured marker to prove non-publication. All other failures are fail-fast.

Always surface captured output for the final success or failure and remove only the temporary files created by this run.

Accept success only when Oracle exits zero and stdout's final nonblank line is exactly `ORACLE_PR_REVIEW_PUBLISHED`. Otherwise report failure. On success, return Oracle's review without rewriting its findings.
