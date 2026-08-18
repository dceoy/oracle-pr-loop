---
name: oracle-pr-review
description: Review one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, prioritizing inline review comments. Use when the user explicitly wants ChatGPT-via-Oracle review; if no PR target is supplied, detect the pull request for the current branch.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Review

Review exactly one GitHub pull request through Oracle browser mode. Oracle owns browser/session and remote-host
routing; ChatGPT's connected GitHub app owns repository access, review context, and publication of the review to
GitHub. Do not duplicate either responsibility in the current agent.

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

Use `gh` only for PR identity when the target is omitted. Do not use `gh`, GitHub APIs, the local checkout, or
attachments to gather review context; the ChatGPT GitHub app is the sole review-context source.

## Oracle routing

Keep Oracle's native browser routing intact. Do not add `--remote-host` or `--remote-token`, print remote credentials,
or reproduce Oracle's configuration-precedence logic in this skill. Oracle may resolve remote browser settings from
its supported user configuration or `ORACLE_REMOTE_HOST` / `ORACLE_REMOTE_TOKEN`; when no remote host resolves, it
uses the local browser path.

Treat Oracle routing or authentication failures as failures rather than switching review paths.

## Run

Substitute the validated canonical target into `OWNER/REPO#NUMBER` and run exactly:

```bash
oracle \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# PR: OWNER/REPO#NUMBER
@GitHub Review this pull request and publish the review to GitHub. You must use the connected GitHub app to submit
a GitHub pull-request review before answering; the task is not complete until that submission succeeds. Prioritize
inline review comments for line-specific findings, and always use COMMENT as the review action and include a top-level
review body so a review is posted even when there are no inline findings. If there are no actionable findings, state
that no actionable issues were found in that COMMENT review; do not return only a chat summary. If publication cannot
be completed, report the publication failure instead of presenting an unposted review as success. After successful
submission, explicitly state that the review was posted to GitHub. Apply KISS, DRY, and YAGNI when evaluating
maintainability: flag concrete duplication, unnecessary complexity, or speculative functionality, flexibility,
abstractions, compatibility layers, extension points, or infrastructure without a current requirement; prefer
existing code and the smallest coherent solution, and avoid style-only simplification suggestions.'
```

Do not interpolate an unvalidated shell variable, use `eval`, or append repository context or user prose.

## Oracle retry and timeout policy

Resolve and validate the PR target once before the first attempt. If the target is omitted, run
`gh pr view --json url --jq .url` at most once. Create two private temporary files outside the repository with
`mktemp`, one for stdout and one for stderr. If either allocation fails, remove any allocated file with `rm -f --` and
fail before invoking Oracle. Reuse those exact paths for every attempt; redirections truncate both before each
invocation. Remove only those exact files on every exit path, and never retry because cleanup failed.

For each attempt, run the exact Oracle command shown above with only these redirections appended:
`>"$stdout_file" 2>"$stderr_file"`. Do not merge streams, alter Oracle arguments, or change the prompt. Record the exit
status immediately, then inspect stdout and stderr independently. On success or terminal failure, surface both streams
with `cat --`; suppress captured output for an intermediate retryable-busy attempt except for its concise retry
diagnostic.

### Remote busy

A failure is retryable as remote busy only when every condition below is true:

- the Oracle invocation exited unsuccessfully;
- the last nonblank line of captured stderr is exactly `✖ busy`, excluding only its terminating newline and without
  trimming any other whitespace;
- the match is not inferred from stdout, a substring, generic `busy` prose, `ERROR: busy`, HTTP status text, or any
  differently formatted message; and
- capture is complete and contains no evidence that browser execution was accepted or started.

The current Oracle remote server returns HTTP 409 `{"error":"busy"}` before `/runs` acceptance when its single-flight
guard is occupied, while the remote client currently collapses a non-200 response to `Error(message)`. Treat exact
`✖ busy` as a narrow compatibility classifier, not a protocol-level proof.

The initial invocation is attempt 1. Allow six busy retry opportunities, for seven total invocations. For retry numbers
1 through 6, use nominal delays of 1, 2, 4, 8, 16, and 30 seconds. Before each retry, sample Bash `$RANDOM` once and
apply a 0.750 through 1.000 jitter multiplier:

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

After the seventh matching busy failure, surface its captured streams, remove the temporary files, and report that the
six-retry budget was exhausted.

### `read ETIMEDOUT`

Treat a last nonblank stderr line exactly equal to `✖ read ETIMEDOUT` as a special terminal transport condition, not a
retryable review failure. A review run has a GitHub write side effect, so a transport timeout can occur after ChatGPT
already submitted the review but before Oracle delivered its final result. Blind replay could therefore publish a
duplicate review.

Never automatically replay an Oracle PR-review invocation after exact `✖ read ETIMEDOUT`, and do not use `gh`, GitHub
APIs, timestamps, review counts, or other local heuristics to infer that no review was published. Those signals cannot
prove that the timed-out browser run will not publish later.

If captured stdout already contains the same explicit affirmative publication confirmation required for a normal
successful result — that the GitHub pull-request review was posted to GitHub — and it contains no publication-failure
statement, accept the invocation as a recovered success even though Oracle exited non-zero. This covers a late read
timeout after the browser response was already captured without replaying the side effect.

Otherwise surface the captured streams, remove the temporary files, and fail closed with the publication state marked
indeterminate. Do not retry automatically. Do not widen this recovery rule to other `ETIMEDOUT` text, generic timeouts,
disconnects, TLS errors, or ambiguous transport failures.

Authentication or authorization failures, GitHub-app routing or access failures, invalid configuration, malformed
targets or prompts, local-browser failures, unrelated 4xx/5xx errors, all non-exact timeouts or disconnects, acceptance-
evidenced busy failures, and every non-exact busy message remain fail-fast.

## Failure and output

Fail closed: do not substitute Oracle API mode, another model, another PR, local/GitHub-API review context, or the
current agent's own review. Do not retry a failure unless it satisfies every condition in the remote-busy policy. Do
not retry with a modified prompt if ChatGPT cannot invoke `@GitHub` or access the target repository.

Accept the result only when either:

1. Oracle exits zero and its response explicitly confirms that a GitHub pull-request review was posted to GitHub; or
2. exact `✖ read ETIMEDOUT` occurs and captured stdout already contains that same affirmative publication confirmation
   without a publication-failure statement.

If neither success condition holds, or the response shows that the GitHub app was not invoked, lacked repository
access, or failed to publish the review, report the failure. Otherwise return Oracle's ChatGPT review without rewriting
its findings.
