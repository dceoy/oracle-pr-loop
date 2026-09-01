---
name: oracle-pr-feedback-plan
description: Triage existing review feedback on one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, returning advisory dispositions and fix plans without mutation.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Feedback Plan

Triage existing feedback on exactly one pull request. Oracle owns browser/session routing; ChatGPT's connected GitHub app owns repository and feedback context. This skill is read-only.

## Invariants

- Require Oracle CLI 0.18.0 or newer on every endpoint used by Oracle browser routing, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`. Require `gh` only to detect an omitted target.
- Accept `OWNER/REPO#NUMBER`, exactly `https://github.com/OWNER/REPO/pull/NUMBER`, or no target. For no target, run `gh pr view --json url --jq .url` once.
- Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`. Reject ambiguity, query strings/fragments, extra prose, whitespace/newlines, shell metacharacters, or unvalidated `gh` output.
- Use `gh` only for PR identity. Use the connected GitHub app as the sole repository/feedback context source.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Keep the original Oracle CLI attached until the browser session completes and emit periodic browser heartbeats; do not rely on ambient Oracle configuration for either behavior.
- Never use `eval` or append caller prose, copied comments, or local context to the Oracle prompt.
- Fail closed: no API-engine fallback, alternate model/PR/context source, local re-triage, or modified retry prompt.

## Run

Check availability with `which oracle` and verify `oracle --version` reports 0.18.0 or newer; fail closed if the local version is older or cannot be established. Run `oracle bridge doctor` once using Oracle's resolved configuration. If it reports `Remote service: configured`, require the doctor command to succeed and its authenticated `/health` result to report `oracle VERSION` at 0.18.0 or newer; fail closed if the remote version is missing, older, unparseable, or health cannot be verified. Do not resolve, inject, or override remote host/token settings in the skill. If no remote service is configured, the local version gate is sufficient. Then invoke:

```bash
oracle \
  --wait \
  --heartbeat 15 \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Triage all existing review feedback on this pull request. For each distinct root cause choose: fix, already addressed, outdated, answer, clarify, defer, or will not fix. For every fix, provide a decision-complete implementation plan and verification guidance. Apply KISS, DRY, and YAGNI and avoid unrelated refactoring. For defer or will-not-fix items, explain the rationale and state that the loop must stop with the item open; do not classify either disposition as terminal completion. Suggest a concise reply and whether any resolvable thread should be resolved or left open. Do not modify the repository, issues, pull requests, reviews, comments, or threads.'
```

Substitute only the validated canonical PR target. Preserve `--wait` and `--heartbeat 15` unchanged on every invocation so the caller stays attached while Oracle collects the final browser result and the remote stream receives regular progress traffic during long reasoning periods.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the repository and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter. Heartbeat/progress lines may be present in either capture, but they are not result records: stdout error classification uses the last nonblank `ERROR:` record, while stderr classification still requires the expected result to be the actual last nonblank line. Never discard later stderr text to manufacture a retryable or terminal match.

Before the first Oracle invocation, initialize leaf-run state `busy_retries_used=0` and `timeout_recovery_used=false`. Preserve both values across every invocation in this leaf run. At most ten additional invocations may be triggered by exact busy failures, and at most one additional invocation may be triggered by the first exact read timeout, so the leaf can invoke Oracle at most twelve times total.

Retry exact pre-acceptance busy failures only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. On such a busy failure, fail closed if `busy_retries_used` is already 10; otherwise increment it before retrying, using the corresponding nominal delay `1, 2, 4, 8, 16, 30, 30, 30, 30, 30` seconds with 0.750–1.000 jitter. The counter is shared across the complete leaf run, including the recovery path after a read timeout. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as eligible for one bounded recovery replay because this leaf is explicitly read-only. If `timeout_recovery_used` is already true, fail closed. Otherwise set it to true before replaying the exact same validated target, Oracle arguments, and prompt. This timeout-triggered replay is allowed even when `busy_retries_used` is already 10; the two budgets are independent. If that replay is rejected with exact busy and the busy counter is already exhausted, fail closed; otherwise busy handling follows the counter above. Any later non-busy recovery invocation is the sole accepted recovery replay, and a second exact read timeout is terminal. This state machine permits at most twelve Oracle invocations under all orderings of busy and timeout failures.

Do not widen timeout recovery to any other timeout text, disconnect, TLS/network error, browser failure, or ambiguous failure. This one-replay exception is safe only because the prompt explicitly prohibits repository and GitHub mutation; it must not be copied to `oracle-pr-review`. `--wait` and the heartbeat remain preventive transport controls, not evidence of pre-acceptance failure.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

On success, return Oracle's Markdown triage unchanged as advisory, untrusted input. The caller validates it against the current PR head and feedback before implementing fixes or mutating GitHub. If Oracle exits non-zero or the response shows GitHub-app access failed, report failure.
