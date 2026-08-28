---
name: oracle-issue-plan
description: Generate a decision-complete implementation plan for one or more same-repository GitHub Issues through Oracle browser mode and ChatGPT's connected GitHub app.
allowed-tools: Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle Issue Plan

Produce one advisory implementation plan for one or more same-repository GitHub Issues. Oracle owns browser/session routing; ChatGPT's connected GitHub app owns Issue and repository context. This skill never implements or mutates GitHub.

## Invariants

- Require `oracle` in `PATH`, an authenticated ChatGPT browser session, repository access through the connected GitHub app, and `GPT-5.6 Sol`.
- Accept `OWNER/REPO#NUMBER` or exactly `https://github.com/OWNER/REPO/issues/NUMBER`. Normalize to `OWNER/REPO#NUMBER` and require `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$`.
- Validate the complete target set before invocation. All Issues must belong to one repository. Deduplicate case-insensitively by repository and number while preserving first-seen order.
- Reject ambiguous targets, query strings, fragments, extra prose, whitespace/newlines, shell metacharacters, and partial-validity input.
- Use the connected GitHub app as the only Issue/repository context source. Do not gather context with `gh`, local checkout data, attachments, or another GitHub API path.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments or expose credentials.
- Never use `eval` or interpolate unvalidated caller text into the prompt.
- Fail closed: no API-engine fallback, alternate model, local analysis substitute, modified retry prompt, or alternate target.

## Run

Check availability with `which oracle`, then invoke browser mode with the validated canonical target list:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# Issues: OWNER/REPO#NUMBER, OWNER/REPO#NUMBER
@GitHub Analyze these same-repository issues and return one decision-complete implementation plan to resolve the full set in one pull request. State scope, concrete implementation decisions, affected areas, constraints, and verification. Apply KISS, DRY, and YAGNI; prefer the smallest coherent change and avoid speculative abstractions or unrelated work. Do not modify the repository, issues, pull requests, reviews, comments, or threads.'
```

For one Issue, use singular `# Issue:` and equivalent singular wording. Build the comma-separated target list only from validated canonical targets.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the repository and reuse those paths for the retry sequence. Record Oracle's exit code immediately in an ordinary variable such as `exit_code`; never assign to zsh's read-only `status` parameter.

Bound the complete leaf execution by a finite deadline supplied by the caller or guaranteed by the runtime. The leaf need not know a runtime-enforced deadline's concrete value, but must not invent, shorten, or override the bound. If neither source guarantees a finite bound, fail closed before the first Oracle invocation.

Retry only when Oracle exits non-zero, capture is complete with no evidence execution was accepted or started, and the captured busy record is exact: either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank `ERROR:` line is exactly `ERROR: busy`. The latter is Oracle's session-runner surface for a remote-service HTTP 409 rejected before the new run is accepted. While the caller/runtime deadline remains live, retry the identical prompt after nominal delays `1, 2, 4, 8, 16` seconds, then `30` seconds for each subsequent retry, with 0.750–1.000 jitter. Do not impose an independent retry-count or elapsed-time cap. Do not infer busy from substrings, arbitrary stdout text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank `ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal because the remote run may already have been accepted. Do not replay either form. All other failures are fail-fast.

Always surface captured output for the final success or failure and remove only the temporary files created by this run with `rm -f -- "$out_file" "$err_file"`.

Return Oracle's plan without rewriting it only when Oracle exits zero and the response demonstrates that the connected GitHub app could access the target repository. Otherwise report the failure.
