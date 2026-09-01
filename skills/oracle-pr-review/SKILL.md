---
name: oracle-pr-review
description: Review one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, publishing a COMMENT review with inline findings when possible.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*), Bash(bash:*)
---

# Oracle PR Review

Review exactly one pull request through Oracle browser mode and publish the review through ChatGPT's connected GitHub app. Oracle owns browser/session routing; ChatGPT owns repository context and GitHub review publication.

## Invariants

- Require Oracle CLI 0.18.0 or newer on every endpoint used by Oracle browser routing, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`. Require `gh` only to detect an omitted target and to perform the exact-marker timeout recovery defined below.
- Accept `OWNER/REPO#NUMBER`, exactly `https://github.com/OWNER/REPO/pull/NUMBER`, or no target. For no target, run `gh pr view --json url --jq .url` once.
- Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`. Reject ambiguity, query strings/fragments, extra prose, whitespace/newlines, shell metacharacters, or unvalidated `gh` output.
- Use the connected GitHub app as the sole review-context and publication path. Outside timeout recovery, use `gh` only for PR identity.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Keep the original Oracle CLI attached until the browser session completes and emit periodic browser heartbeats; do not rely on ambient Oracle configuration for either behavior.
- Never use `eval` or append caller prose or local repository context to the prompt.
- Fail closed: no API-engine fallback, alternate model/PR/context source, local review substitute, or modified retry prompt.

## Run

Check availability with `which oracle` and verify `oracle --version` reports 0.18.0 or newer; fail closed if the local version is older or cannot be established. Run `oracle bridge doctor` once using Oracle's resolved configuration. If it reports `Remote service: configured`, require the doctor command to succeed and its authenticated `/health` result to report `oracle VERSION` at 0.18.0 or newer; fail closed if the remote version is missing, older, unparseable, or health cannot be verified. Do not resolve, inject, or override remote host/token settings in the skill. If no remote service is configured, the local version gate is sufficient.

Before the first Oracle attempt, create the stdout and stderr capture files with separate `mktemp` calls. Derive one per-run `review_token` from their random suffixes, validate it against `^[A-Za-z0-9_-]+$`, and define the exact correlation marker `<!-- oracle-pr-review:REVIEW_TOKEN -->`. Reuse the same files, token, marker, and prompt for every busy retry. The token is correlation data only; it must not encode caller prose or repository context.

Invoke:

```bash
oracle \
  --wait \
  --heartbeat 15 \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Review this pull request and publish exactly one GitHub pull-request review before answering. Use COMMENT, always include a non-empty top-level body, and prefer inline comments for safely line-anchored findings. If there are no actionable findings, say so in the COMMENT review. Apply KISS, DRY, and YAGNI to concrete maintainability issues; avoid style-only findings. Append the exact HTML comment <!-- oracle-pr-review:REVIEW_TOKEN --> to the top-level review body. If publication fails, report failure rather than an unposted review. After confirmed publication, state that the review was posted and emit the exact final plain-text line ORACLE_PR_REVIEW_PUBLISHED.'
```

Substitute only the validated canonical PR target and validated per-run review token. Preserve `--wait` and `--heartbeat 15` unchanged on every invocation so the caller stays attached while Oracle collects the final browser result and the remote stream receives regular progress traffic during long reasoning periods. The stdout publication marker is valid only after the connected GitHub app confirms publication. The HTML correlation marker exists only to prove that a specific timed-out invocation already published its review.

## Retry and result contract

Capture stdout and stderr separately in the private temporary files created above and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter. Heartbeat/progress lines may be present in either capture, but they are not result records: stdout error classification uses the last nonblank `ERROR:` record, while stderr busy/timeout classification still requires the expected result to be the actual last nonblank stderr line. Never discard later stderr text to manufacture a retryable or terminal match. Normal publication success separately requires `ORACLE_PR_REVIEW_PUBLISHED` to be stdout's actual final nonblank line; any later progress output therefore fails closed instead of being ignored heuristically.

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. Allow ten retries after the initial attempt, using nominal delays `1, 2, 4, 8, 16, 30, 30, 30, 30, 30` seconds with 0.750–1.000 jitter. This keeps the retry path bounded while allowing roughly three minutes for a legitimate single-flight remote run to finish. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal for Oracle transport and never replay it. `--wait` and the heartbeat are preventive transport controls, not evidence that a timed-out accepted run is safe to replay. Instead, invoke the bundled recovery script exactly once with the validated repository, PR number, and per-run review token:

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/wait-for-review-marker.sh" OWNER/REPO NUMBER REVIEW_TOKEN
```

Do not reproduce its polling loop with agent-driven `gh` or `sleep` calls. The script reads the PR's review collection with `gh api --paginate` and looks only for a persisted review whose state is `COMMENTED` and whose top-level body contains the exact per-run correlation marker. It checks immediately, then performs up to 15 additional reads separated by exactly 60 seconds while zero reviews match, for at most 900 seconds of recovery waiting and 16 total reads. Exit 0 with `RESULT=FOUND` proves exactly one matching review and recovered publication. Exit 1 with `RESULT=NOT_FOUND` means the bounded window was exhausted; exit 2 with `RESULT=MULTIPLE` means more than one review matched; exit 3 with `RESULT=ERROR` means validation or a GitHub read failed. Every nonzero result leaves publication **indeterminate** and fails closed. The exact marker is positive proof of publication, not a heuristic. Do not use review counts, timestamps, reviewer identity, partial-marker matches, stdout content, or absence of the marker to infer publication or non-publication.

Always surface captured Oracle output and the recovery script's final result for the final success or failure, then remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

Accept success in either of two cases: Oracle exits zero and stdout's final nonblank line is exactly `ORACLE_PR_REVIEW_PUBLISHED`; or Oracle ends with the exact `read ETIMEDOUT` form above and the recovery script exits zero after proving one exact-marker `COMMENTED` review. On normal success, return Oracle's review without rewriting its findings. On recovered success, do not reconstruct findings from partial stdout; report the recovered review identity and let GitHub remain the durable handoff to feedback triage.
