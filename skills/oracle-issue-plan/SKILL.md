---
name: oracle-issue-plan
description: Generate a decision-complete pull request implementation plan for one or more GitHub issues from the same repository through Oracle browser mode and ChatGPT's connected GitHub app.
allowed-tools: Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle Issue Plan

Generate a decision-complete implementation plan for one or more GitHub issues from the same repository, intended to
resolve the full set in one pull request, through Oracle browser mode. Oracle owns browser/session and remote-host
routing; ChatGPT's connected GitHub app owns issue and repository context. Do not duplicate either responsibility in
the current agent.

## Prerequisites

Require:

- `oracle` in `PATH` and an authenticated ChatGPT browser session.
- The ChatGPT GitHub app authorized for the target repository.
- `GPT-5.6 Sol` available to Oracle browser mode.

Check Oracle with:

```bash
which oracle
```

Stop if a required prerequisite is unavailable.

## Target

Accept one or more independent targets, each one of:

1. `OWNER/REPO#NUMBER`.
2. `https://github.com/OWNER/REPO/issues/NUMBER` with no query string, fragment, or extra path segments, normalized
   to the canonical form above.

Normalize each target on its own and require every one to match:

```regex
^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$
```

Reject missing, ambiguous, or non-matching input. Never put raw user text, query strings, whitespace, newlines, shell
metacharacters, or extra instructions into the Oracle prompt.

Before invoking Oracle, validate the whole target set as a unit:

- Reject the entire set if any single target is malformed, ambiguous, a non-Issue URL, or carries embedded whitespace,
  newlines, or extra prose — never plan with only the valid remainder.
- Require every target to share the same `OWNER/REPO`, comparing owner and repo case-insensitively (GitHub treats
  them as the same repository regardless of case); reject a set spanning more than one repository rather than
  letting Oracle decide.
- Deduplicate targets by `OWNER/REPO#NUMBER` compared case-insensitively on `OWNER/REPO`, keeping first-seen order
  and each target's first-seen spelling; do not sort.
- Join the validated, canonical targets with a fixed `,` separator that the skill generates; never copy the
  caller's own separators (newlines, list markers, extra whitespace) into the Oracle prompt.

Do not use `gh`, GitHub APIs, the local checkout, or attachments to gather issue or repository context; the ChatGPT
GitHub app is the sole context source.

## Oracle routing

Keep Oracle's native browser routing intact. Do not add `--remote-host` or `--remote-token`, print remote credentials,
or reproduce Oracle's configuration-precedence logic in this skill. Oracle may resolve remote browser settings from
its supported user configuration or `ORACLE_REMOTE_HOST` / `ORACLE_REMOTE_TOKEN`; when no remote host resolves, it
uses the local browser path.

Treat Oracle routing or authentication failures as failures rather than switching planning paths.

## Run

For a single validated canonical target, substitute it into the `OWNER/REPO#NUMBER` occurrence and run exactly:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# Issue: OWNER/REPO#NUMBER
@GitHub Analyze this issue, then produce a decision-complete implementation plan for a coding agent to resolve it
in one pull request. Apply KISS and DRY: prefer the smallest coherent change, reuse existing code and abstractions,
consolidate duplication when it materially simplifies the implementation, and avoid speculative abstractions,
compatibility layers, or new infrastructure unless required by the issue or existing repository compatibility
constraints.'
```

For two or more validated, deduplicated, same-repository canonical targets, join every one of them with a fixed `,`
separator — in first-seen order, with no trailing separator and nothing else — and substitute that list into the
`OWNER/REPO#NUMBER, OWNER/REPO#NUMBER` occurrence, then run exactly:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# Issues: OWNER/REPO#NUMBER, OWNER/REPO#NUMBER
@GitHub Analyze these issues, then produce a single decision-complete implementation plan for a coding agent to
resolve them in one pull request. Apply KISS and DRY: prefer the smallest coherent change, reuse existing code and
abstractions, consolidate duplication when it materially simplifies the implementation, and avoid speculative
abstractions, compatibility layers, or new infrastructure unless required by the issues or existing repository
compatibility constraints.'
```

Do not interpolate an unvalidated shell variable, use `eval`, or append repository context or user prose.

## Remote busy retry policy

Validate and canonicalize the complete Issue target set once before the first
attempt. Create two private temporary files outside the repository with
`mktemp`, one for stdout and one for stderr. If either allocation fails,
remove any allocated file with `rm -f --` and fail before invoking Oracle.
Reuse those exact paths for every attempt; the redirections truncate both
files before each invocation. Remove only those exact files on every exit path,
and never retry because cleanup failed.

For each attempt, run the exact Oracle command shown in the Run section with
only these shell redirections appended: `>"$stdout_file" 2>"$stderr_file"`.
Do not use `2>&1`, merge the streams, alter Oracle arguments, or change the
prompt. Record the exit status immediately, then inspect the two files
independently. On success or terminal failure, use `cat -- "$stdout_file"`
and `cat -- "$stderr_file" >&2` to surface the corresponding captured
streams without rewriting them. Suppress the captured output of an
intermediate retryable-busy attempt except for its concise retry diagnostic.

Classify a failure as retryable only when every condition below is true:

- the Oracle invocation exited unsuccessfully;
- captured stderr contains a line exactly equal to
  `ORACLE_REMOTE_BUSY_PRE_ACCEPTANCE`, excluding only its terminating newline;
- the marker is not inferred from stdout, generic `busy` text, `ERROR: busy`,
  HTTP status text, or any other prose; and
- capture is complete and does not contain evidence that the browser run was
  accepted. If capture is incomplete, the streams are ambiguous, or
  acceptance is uncertain, fail fast rather than replaying the invocation.

Use Bash built-ins to inspect each file separately and scan stderr line by line
for the exact marker without trimming whitespace. Never classify merged
output, stdout-only markers, generic busy text, local-browser failures,
ambiguous transport, or unrelated errors as retryable. This contract requires
an Oracle CLI version that emits the marker; do not add a heuristic fallback
for older versions.

The initial invocation is attempt 1. Allow six retry opportunities, for seven
total invocations. For retry number 1 through 6, use nominal delays of 1, 2,
4, 8, 16, and 30 seconds. Immediately before each retry, sample Bash
`$RANDOM` once:

```bash
random_value=$RANDOM
jitter_milliseconds=$((750 + random_value % 251))
delay_milliseconds=$((nominal_seconds * jitter_milliseconds))
printf -v delay_seconds '%d.%03d' \
  "$((delay_milliseconds / 1000))" "$((delay_milliseconds % 1000))"
printf 'Oracle remote busy on attempt %d; retrying attempt %d in %ss\n' \
  "$attempt" "$((attempt + 1))" "$delay_seconds" >&2
sleep "$delay_seconds"
```

This yields an independent multiplier from 0.750 through 1.000 and a delay
from 75% through 100% of nominal, never above 30 seconds. Do not reuse a
sample, choose jitter through model text, or modify the effective routing
environment. After the seventh matching failure, surface its captured streams,
remove the temporary files, and report that remote busy persisted for seven
attempts and the six-retry budget was exhausted. A successful invocation ends
this retry sequence; a later invocation starts with a fresh budget.

Authentication or authorization failures, GitHub-app routing or access
failures, invalid configuration, malformed targets or prompts, local-browser
failures, unrelated 4xx/5xx errors, timeouts, disconnects, ambiguous
transport, and any post-acceptance or otherwise uncertain failure remain
fail-fast. Do not use this policy to replay a run that may have been accepted.

## Failure and output

Fail closed: do not substitute Oracle API mode, another model, another issue, local/GitHub-API context, or the current
agent's own analysis. Do not retry a failure unless it satisfies every condition in the remote busy retry policy. Do not
retry with a modified prompt if ChatGPT cannot invoke `@GitHub` or access the target repository.

If Oracle exits non-zero or its response shows that the GitHub app was not invoked or lacked repository access, report
the failure. Otherwise return Oracle's implementation plan without rewriting it.
