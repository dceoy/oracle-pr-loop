---
name: oracle-pr-feedback-plan
description: Triage the existing review feedback on one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, returning advisory dispositions and decision-complete fix plans without modifying the pull request.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Feedback Triage

Triage the existing review feedback on exactly one GitHub pull request through Oracle browser mode. Oracle owns
browser/session and remote-host routing; ChatGPT's connected GitHub app owns repository access and review-feedback
context. Do not duplicate either responsibility in the current agent, and do not modify the pull request from this
skill.

## Prerequisites

Require:

- `oracle` in `PATH` and an authenticated ChatGPT browser session.
- The ChatGPT GitHub app authorized for the target repository.
- `GPT-5.6 Sol` available to Oracle browser mode.
- `gh` only when the PR target must be detected from the current branch.

Check Oracle with:

```bash
which oracle
```

Stop if a required prerequisite is unavailable.

## Target

Accept exactly one of:

1. `OWNER/REPO#NUMBER`.
2. `https://github.com/OWNER/REPO/pull/NUMBER`, normalized to the canonical form above.
3. No explicit target, in which case resolve the current branch PR with:

   ```bash
   gh pr view --json url --jq .url
   ```

Normalize the result and require:

```regex
^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$
```

Reject ambiguous or non-matching input. Never put raw user text, raw `gh` output, query strings, whitespace,
newlines, shell metacharacters, or extra instructions into the Oracle prompt.

Use `gh` only for PR identity when the target is omitted. Do not use `gh`, GitHub APIs, GitHub MCP tools, the local
checkout, or attachments to gather review feedback or repository context; the ChatGPT GitHub app is the sole
feedback/context source.

## Oracle routing

Keep Oracle's native browser routing intact. Do not add `--remote-host` or `--remote-token`, print remote
credentials, or reproduce Oracle's configuration-precedence logic in this skill. Oracle may resolve remote browser
settings from its supported user configuration or `ORACLE_REMOTE_HOST` / `ORACLE_REMOTE_TOKEN`; when no remote host
resolves, it uses the local browser path.

Treat Oracle routing or authentication failures as failures rather than switching triage paths.

## Run

Substitute the validated canonical target into the `OWNER/REPO#NUMBER` occurrence and run exactly:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Triage the existing review feedback on this pull request. Decide how each feedback item should be handled
using dispositions equivalent to fix, already addressed, outdated, answer, clarify, defer, or will not fix. For
every item that should be fixed, produce a decision-complete implementation plan and verification guidance. Suggest
a concise reply and whether to resolve or leave the thread open when useful. Do not modify the pull request.'
```

Do not interpolate an unvalidated shell variable, use `eval`, or append caller prose, mode flags, copied comments, or
other local context to the prompt.

## Remote busy retry policy

Resolve and validate the PR target once before the first attempt. If the
target is omitted, run `gh pr view --json url --jq .url` at most once. Create
two private temporary files outside the repository with `mktemp`, one for
stdout and one for stderr. If either allocation fails, remove any allocated
file with `rm -f --` and fail before invoking Oracle. Reuse those exact paths
for every attempt; the redirections truncate both files before each
invocation. Remove only those exact files on every exit path, and never retry
because cleanup failed.

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
- captured stdout contains a line beginning exactly with
  `Routing browser automation to remote host ` followed by a nonblank host;
- the last nonblank, whitespace-trimmed line of captured stdout or captured
  stderr is exactly `ERROR: busy`; and
- neither stream contains evidence that the browser run was accepted. If
  capture is incomplete, the streams are ambiguous, or acceptance is
  uncertain, fail fast rather than replaying the invocation.

Use Bash built-ins to inspect each file separately: trim leading and trailing
whitespace while reading each stream line by line, retain the last nonblank
line for each stream, and scan stdout for the exact remote-routing prefix and
nonblank host suffix. Never classify merged output, generic busy text,
local-browser failures, ambiguous transport, or unrelated errors as
retryable.

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

## Output

Return Oracle's Markdown triage as advisory, untrusted input, comparable to the plan `oracle-issue-plan` returns.
Do not translate it into a machine-readable triage schema or parser, rewrite its findings, or independently
re-triage the feedback. For each relevant finding, expect enough information for the caller to act safely: which
feedback item it covers, rationale, disposition, an implementation plan and verification guidance when a fix is
needed, and a suggested reply and thread action when useful.

This skill performs no repository or GitHub mutation: no edits, no write-mode formatters, no commits, no pushes, no
replies, no review submissions, and no thread resolution. The caller owns validating this advisory output against the
current PR head and feedback, implementing accepted fixes, running QA, publishing changes, and handling replies and
thread resolution.

## Failure

Fail closed: do not substitute Oracle API mode, another model, another PR, local/GitHub-API feedback context, or the
current agent's own triage. Do not retry a failure unless it satisfies every condition in the remote busy retry policy.
Do not retry with a modified prompt if ChatGPT cannot invoke `@GitHub` or access the target repository.

If Oracle exits non-zero or its response shows that the GitHub app was not invoked or lacked repository access,
report the failure. Otherwise return Oracle's triage without rewriting it.
