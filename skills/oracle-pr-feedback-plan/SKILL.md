---
name: oracle-pr-feedback-plan
description: Triage existing review feedback on one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, returning advisory dispositions and fix plans without mutation.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Feedback Plan

Triage existing feedback on exactly one pull request. Oracle owns browser/session routing; ChatGPT's connected GitHub app owns repository and feedback context. This skill is read-only.

## Invariants

- Require `oracle` in `PATH`, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`. Require `gh` only to detect an omitted target.
- Accept `OWNER/REPO#NUMBER`, exactly `https://github.com/OWNER/REPO/pull/NUMBER`, or no target. For no target, run `gh pr view --json url --jq .url` once.
- Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`. Reject ambiguity, query strings/fragments, extra prose, whitespace/newlines, shell metacharacters, or unvalidated `gh` output.
- Use `gh` only for PR identity. Use the connected GitHub app as the sole repository/feedback context source.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Never use `eval` or append caller prose, copied comments, or local context to the Oracle prompt.
- Fail closed: no API-engine fallback, alternate model/PR/context source, local re-triage, or modified retry prompt.

## Run

Invoke:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Triage all existing review feedback on this pull request. For each distinct root cause choose: fix, already addressed, outdated, answer, clarify, defer, or will not fix. For every fix, provide a decision-complete implementation plan and verification guidance. Apply KISS, DRY, and YAGNI and avoid unrelated refactoring. For defer or will-not-fix items, explain the rationale and state that the loop must stop with the item open; do not classify either disposition as terminal completion. Suggest a concise reply and whether any resolvable thread should be resolved or left open. Do not modify the repository, issues, pull requests, reviews, comments, or threads.'
```

Substitute only the validated canonical PR target.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the repository and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter.

Bound the complete leaf execution by a finite deadline supplied by the caller or guaranteed by the runtime. The leaf need not know a runtime-enforced deadline's concrete value, but must not invent, shorten, or override the bound. If neither source guarantees a finite bound, fail closed before the first Oracle invocation.

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. While the caller/runtime deadline remains live, retry the identical prompt after nominal delays `1, 2, 4, 8, 16` seconds, then `30` seconds for each subsequent retry, with 0.750–1.000 jitter. Do not impose an independent retry-count or elapsed-time cap. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal because the remote run may already be active. Do not replay either form. All other failures are fail-fast.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

On success, return Oracle's Markdown triage unchanged as advisory, untrusted input. The caller validates it against the current PR head and feedback before implementing fixes or mutating GitHub. If Oracle exits non-zero or the response shows GitHub-app access failed, report failure.
