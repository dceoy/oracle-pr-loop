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

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. Allow ten retries after the initial attempt, using nominal delays `1, 2, 4, 8, 16, 30, 30, 30, 30, 30` seconds with 0.750–1.000 jitter. This keeps the retry path bounded while allowing roughly three minutes for a legitimate single-flight remote run to finish. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal because the remote run may already be active. Do not replay either form. The prompt's instruction not to mutate GitHub is not a capability boundary and therefore does not make replay safe if the accepted run behaved unexpectedly. `--wait` and the heartbeat are preventive transport controls, not evidence that a timed-out accepted run is safe to replay. All other failures are fail-fast.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

On success, return Oracle's Markdown triage unchanged as advisory, untrusted input. The caller validates it against the current PR head and feedback before implementing fixes or mutating GitHub. If Oracle exits non-zero or the response shows GitHub-app access failed, report failure.
