---
name: oracle-pr-sweep
description: Discover and batch-review open pull requests across non-archived repositories owned by the authenticated GitHub user through Oracle browser mode and ChatGPT's connected GitHub app, without modifying GitHub.
allowed-tools: Bash(oracle:*), Bash(which:*), Bash(sleep:*), Bash(mktemp:*), Bash(cat --:*), Bash(printf:*), Bash(rm -f --:*)
---

# Oracle PR Sweep

Discover and review a bounded batch of open pull requests across repositories
owned by the authenticated GitHub user. Oracle owns browser/session routing;
ChatGPT's connected GitHub app owns account, repository, pull-request, diff,
check, and review context. This skill is read-only and never publishes reviews,
comments, replies, labels, merges, commits, or other GitHub mutations.

## Invariants

- Require Oracle CLI 0.18.0 or newer on every endpoint used by Oracle browser
  routing, an authenticated ChatGPT browser session, repository access through
  the connected GitHub app, and `GPT-5.6 Sol`.
- Use the connected GitHub app as the only GitHub identity, repository, PR,
  diff, check, and review-context source. Do not gather GitHub context with
  `gh`, a local checkout, attachments, another GitHub API path, or caller-
  supplied repository data.
- Resolve the authenticated GitHub user through the connected GitHub app and
  include only repositories whose owner login exactly matches that user.
  Exclude archived repositories, closed PRs, and draft PRs. Do not broaden the
  scope to organization-owned or collaborator repositories.
- Review at most 20 PRs by default, ordered by most recently updated first.
  Honor an explicit caller-supplied maximum only when it is a decimal integer
  from 1 through 50. Substitute only that validated integer into the Oracle
  prompt; never append caller prose.
- If more eligible PRs exist than the effective maximum, review only the
  bounded newest set and clearly report truncation and the omitted count when
  the connected GitHub app can establish it.
- Bind every reviewed PR result to its exact head SHA. Before finalizing, re-
  read each reviewed PR head. If it changed, mark that PR `STALE` and do not
  present its findings as current.
- Keep Oracle's native browser routing. Do not add remote-host/token arguments
  or expose credentials.
- Keep the original Oracle CLI attached until the browser session completes
  and emit periodic browser heartbeats; do not rely on ambient Oracle
  configuration for either behavior.
- Never use `eval` or interpolate unvalidated caller text into the prompt.
- Fail closed: no API-engine fallback, alternate model, local review
  substitute, modified retry prompt, alternate GitHub context source, or
  automatic scope broadening.

## Review contract

For each selected PR, inspect the current diff and enough repository context to
evaluate correctness, regressions, maintainability, security implications, and
dependency/update risk. Inspect CI/check status, existing reviews, and
unresolved review feedback when the connected GitHub app exposes them. Apply
KISS, DRY, and YAGNI to concrete maintainability issues and avoid style-only
findings.

Classify actionable findings as:

- `blocking`: likely correctness, security, data-loss, compatibility, or
  merge-safety issue that should block merge until addressed;
- `should-fix`: concrete defect or maintainability issue worth addressing
  before merge;
- `optional`: non-blocking improvement with clear value.

Do not manufacture a finding merely to populate a category. For PRs without
actionable findings, state that explicitly. If required context cannot be
obtained, classify the PR as blocked/incomplete rather than guessing.

The consolidated report must include:

- authenticated owner login and effective PR limit;
- eligible, reviewed, omitted, stale, and blocked counts when establishable;
- for every reviewed PR: `OWNER/REPO#NUMBER`, title, exact reviewed head SHA,
  and freshness state;
- actionable findings grouped by PR and classification, with relevant file or
  path references when available;
- PRs with no actionable findings;
- blocked/incomplete or stale PRs and the reason;
- a concise cross-PR summary, without publishing anything to GitHub.

## Run

Check availability with `which oracle` and verify `oracle --version` reports
0.18.0 or newer; fail closed if the local version is older or cannot be
established. Run `oracle bridge doctor` once using Oracle's resolved
configuration. If it reports `Remote service: configured`, require the doctor
command to succeed and its authenticated `/health` result to report
`oracle VERSION` at 0.18.0 or newer; fail closed if the remote version is
missing, older, unparseable, or health cannot be verified. Do not resolve,
inject, or override remote host/token settings in the skill. If no remote
service is configured, the local version gate is sufficient.

Set `MAX_COUNT` to 20 unless the caller supplied a validated integer from 1
through 50. Then invoke exactly one browser run:

```bash
oracle \
  --wait \
  --heartbeat 15 \
  --engine browser \
  --model gpt-5.6-sol \
  --browser-thinking-time high \
  -p '# Account PR sweep
@GitHub Determine the authenticated GitHub user from the connected GitHub app. Find open, non-draft pull requests in non-archived repositories whose owner login exactly matches that authenticated user. Order eligible PRs by most recently updated first and review at most MAX_COUNT. Do not include organization-owned or collaborator repositories.

Review the selected PRs as one batch. For each PR, inspect the current diff and enough repository context to evaluate correctness, regressions, maintainability, security implications, and dependency/update risk. Inspect CI/check status, existing reviews, and unresolved review feedback when available. Apply KISS, DRY, and YAGNI to concrete maintainability issues and avoid style-only findings.

Bind each result to the exact PR head SHA you reviewed. Before finalizing the report, re-read every reviewed PR head; if a head changed, mark that PR STALE and do not present its findings as current.

Classify actionable findings as blocking, should-fix, or optional. Do not invent findings. Explicitly identify PRs with no actionable findings and PRs that are blocked or incomplete because required context is unavailable.

Return one consolidated report containing the authenticated owner login, effective limit, eligible/reviewed/omitted/stale/blocked counts when establishable, each reviewed OWNER/REPO#NUMBER with title and exact head SHA, findings grouped by PR with relevant file/path references when available, PRs with no actionable findings, stale or blocked PRs with reasons, and a concise cross-PR summary. If more eligible PRs exist than the limit, clearly state that the sweep was truncated and report the omitted count when establishable.

Do not modify repositories, pull requests, reviews, comments, threads, labels, checks, branches, commits, or any other GitHub state.'
```

Substitute only the validated decimal `MAX_COUNT`. Preserve `--wait` and
`--heartbeat 15` unchanged on every invocation so the caller stays attached
while Oracle collects the final browser result and the remote stream receives
regular progress traffic during long reasoning periods.

## Retry and result contract

Capture stdout and stderr separately in private temporary files outside the
repository and reuse those paths for the retry sequence. Record Oracle's exit
code immediately in an ordinary variable such as `exit_code`; never assign to
zsh's read-only `status` parameter. Heartbeat/progress lines may be present in
either capture, but they are not result records: stdout error classification
uses the last nonblank `ERROR:` record, while stderr classification still
requires the expected result to be the actual last nonblank line. Never
discard later stderr text to manufacture a retryable or terminal match.

Retry only when Oracle exits non-zero, capture is complete with no evidence
execution was accepted or started, and the captured busy record is exact:
either stderr's last nonblank line is `✖ busy`, or stdout's last nonblank
`ERROR:` line is exactly `ERROR: busy`. Allow ten retries after the initial
attempt, using nominal delays `1, 2, 4, 8, 16, 30, 30, 30, 30, 30` seconds
with 0.750-1.000 jitter. Do not infer busy from substrings, arbitrary stdout
text, HTTP prose, or other messages.

Treat exact final stderr `✖ read ETIMEDOUT` or stdout's last nonblank
`ERROR:` line exactly equal to `ERROR: read ETIMEDOUT` as terminal because
the remote run may already have been accepted. Do not replay either form. The
prompt's read-only instruction is not a capability boundary and therefore does
not make an accepted timed-out run safe to replay. `--wait` and the heartbeat
are preventive transport controls, not evidence that replay is safe. All other
failures are fail-fast.

Always surface captured output for the final success or failure and remove only
the temporary files created by this run with
`rm -f -- "$out_file" "$err_file"`.

Return Oracle's consolidated report without rewriting its findings only when
Oracle exits zero and the response demonstrates connected GitHub access,
identifies the authenticated owner scope, and binds reviewed PRs to exact head
SHAs. Otherwise report the failure.
