---
name: oracle-issue-plan
description: Generate a decision-complete implementation plan for one or more same-repository GitHub Issues through Oracle browser mode and ChatGPT's connected GitHub app.
allowed-tools: Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle Issue Plan

Produce one advisory implementation plan for one or more same-repository GitHub Issues. Oracle owns browser/session routing; ChatGPT's connected GitHub app owns Issue and repository context. This skill never implements or mutates GitHub.

## Invariants

- Require Oracle CLI 0.18.0 or newer on every endpoint used by Oracle browser routing, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`.
- Accept `OWNER/REPO#NUMBER` or exactly `https://github.com/OWNER/REPO/issues/NUMBER`. Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`.
- Validate the complete target set before invocation. All Issues must belong to one repository. Deduplicate case-insensitively by repository and number while preserving first-seen order.
- Reject ambiguous targets, query strings, fragments, extra prose, whitespace/newlines, shell metacharacters, and partial-validity input.
- Use the connected GitHub app as the only Issue/repository context source. Do not gather context with `gh`, local checkout data, attachments, or another GitHub API path.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Keep the original Oracle CLI attached until the browser session completes and emit periodic browser heartbeats; do not rely on ambient Oracle configuration for either behavior.
- Never use `eval` or interpolate unvalidated caller text into the prompt.
- Fail closed: no API-engine fallback, alternate model, local analysis substitute, modified retry prompt, or alternate target.

## Run

Check availability with `which oracle` and verify `oracle --version` reports 0.18.0 or newer; fail closed if the local version is older or cannot be established. Run `oracle bridge doctor` once using Oracle's resolved configuration. If it reports `Remote service: configured`, require the doctor command to succeed and its authenticated `/health` result to report `oracle VERSION` at 0.18.0 or newer; fail closed if the remote version is missing, older, unparseable, or health cannot be verified. Do not resolve, inject, or override remote host/token settings in the skill. If no remote service is configured, the local version gate is sufficient. Then invoke browser mode with the validated canonical target list:

```bash
oracle \
  --wait \
  --heartbeat 15 \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# Issues: OWNER/REPO#NUMBER, OWNER/REPO#NUMBER
@GitHub Analyze these same-repository issues and return one decision-complete implementation plan to resolve the full set in one pull request. State scope, concrete implementation decisions, affected areas, constraints, and verification. Apply KISS, DRY, and YAGNI; prefer the smallest coherent change and avoid speculative abstractions or unrelated work. Do not modify the repository, issues, pull requests, reviews, comments, or threads.'
```

For one Issue, use singular `# Issue:` and equivalent singular wording. Build the comma-separated target list only from validated canonical targets. Preserve `--wait` and `--heartbeat 15` unchanged on every invocation so the caller stays attached while Oracle collects the final browser result and the remote stream receives regular progress traffic during long reasoning periods.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the repository and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter. Heartbeat/progress lines may be present in either capture, but they are not result records: stdout error classification uses the last nonblank `ERROR:` record, while stderr classification still requires the expected result to be the actual last nonblank line. Never discard later stderr text to manufacture a retryable or terminal match.

Retry exact pre-acceptance busy failures as before. Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. Allow ten retry opportunities after the first attempt, using nominal delays `1, 2, 4, 8, 16, 30, 30, 30, 30, 30` seconds with 0.750–1.000 jitter. The busy budget is shared across the complete leaf run, including any recovery sequence after a read timeout. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as eligible for one bounded recovery replay because this leaf is explicitly read-only. A read timeout can occur after the remote run was accepted and may leave that original ChatGPT run active, so never treat it as proof that execution did not start. Instead, permit at most one timeout-triggered replay of the exact same validated target set, Oracle arguments, and prompt. Mark that recovery as used before replaying. If the original run is still active, exact `busy` responses from recovery attempts may use the remaining busy-retry budget above; those rejected pre-acceptance busy attempts do not authorize another timeout-triggered replay. The first non-busy recovery invocation is the sole accepted recovery attempt. If it also ends in exact `read ETIMEDOUT`, or if the busy budget is exhausted before recovery can run, fail closed and do not replay again.

Do not widen timeout recovery to any other timeout text, disconnect, TLS/network error, browser failure, or ambiguous failure. This one-replay exception is safe only because the prompt explicitly prohibits repository and GitHub mutation; it must not be copied to `oracle-pr-review`. `--wait` and the heartbeat remain preventive transport controls, not evidence of pre-acceptance failure.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

Return Oracle's plan without rewriting it only when Oracle exits zero and the response demonstrates that the connected GitHub app could access the target repository. Otherwise report the failure.
