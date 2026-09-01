# oracle-pr-loop

`oracle-pr-loop` is a self-contained agent-skill workflow for taking GitHub
work through independent Oracle/ChatGPT review, starting from an open Issue
or an existing pull request. It is composed from four small, single-purpose
local skills instead of a custom Python review/submit engine.

[![CI/CD](https://github.com/dceoy/oracle-pr-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/dceoy/oracle-pr-loop/actions/workflows/ci.yml)

## Skills

- [`oracle-pr-loop`](skills/oracle-pr-loop/SKILL.md) — orchestrates the other
  three skills and the main agent's own implementation, QA, and Git/GitHub
  actions. This is the entry point.
- [`oracle-issue-plan`](skills/oracle-issue-plan/SKILL.md) — turns one or more
  same-repository GitHub Issues into one advisory implementation plan via
  Oracle browser mode and ChatGPT's connected GitHub app.
- [`oracle-pr-review`](skills/oracle-pr-review/SKILL.md) — reviews one exact
  pull-request head the same way, prioritizing inline review comments.
- [`oracle-pr-feedback-plan`](skills/oracle-pr-feedback-plan/SKILL.md) —
  reads that review's existing GitHub feedback the same way and returns
  advisory dispositions and decision-complete fix plans; it makes no
  repository or GitHub mutation.

## Workflow

**Issue-started:**

1. `oracle-issue-plan` produces an advisory implementation plan for one or
   more same-repository Issues, intended to resolve them in one pull
   request.
2. The main agent validates that plan against the Issue set's combined
   scope, implements the change, runs repository QA, and opens the pull
   request.
3. Enter the PR workflow below on the resulting PR.

**Existing PR** — enter directly at:

1. `oracle-pr-review` reviews the exact current PR head.
2. `oracle-pr-feedback-plan` triages that review's existing GitHub feedback
   and returns advisory dispositions and decision-complete fix plans; it
   makes no repository or GitHub mutation itself.
3. The main agent validates that advice, implements accepted fixes, runs QA,
   publishes the fix, and replies to/resolves review threads.
4. If a fix was published, the PR head changed — run `oracle-pr-review` again
   on the new head and repeat.
5. Finish when a review/triage cycle leaves no actionable feedback with the
   head unchanged. An unchanged head is never re-reviewed.

Stop and report — rather than continuing or fabricating progress — when
triage needs clarification, records a deliberate defer/won't-fix, has an
unpublished or unverified fix, hits an authentication/permission failure, or
hits another explicit blocker.

## Discovery

- Codex CLI and Cursor CLI discover the skills under `.agents/skills/`.
- Claude Code discovers the skills under `.claude/skills/`.

Both discovery roots are local symlinks to the canonical directories under
`skills/`; no discovery path points outside this repository.

## Oracle retry and timeout behavior

Every Oracle leaf explicitly passes `--wait` so the original CLI remains
attached until the browser session completes instead of relying on ambient
Oracle defaults. Each leaf also passes `--heartbeat 15`; browser heartbeats
provide regular progress traffic while ChatGPT is reasoning, which keeps the
remote `/runs` stream active while the final result is being collected. These
controls are preventive only: they do not prove that a timed-out run was not
accepted. This transport contract requires Oracle CLI 0.18.0 or newer on the
local client and, when Oracle's resolved browser routing uses a remote service,
on that `oracle serve` endpoint as well. Each leaf verifies the local version
and uses `oracle bridge doctor` to require an authenticated `/health` response
reporting remote Oracle 0.18.0 or newer before starting a remote browser run.
Older, missing, or unparseable endpoint versions fail closed; the skills never
resolve or inject remote host/token settings themselves.

All three Oracle leaf skills keep one busy-retry counter across the complete
leaf run. An exact pre-acceptance busy failure may trigger at most ten
additional invocations, using nominal delays `1, 2, 4, 8, 16, 30, 30, 30, 30,
30` seconds with 0.750–1.000 jitter. Each busy retry requires complete capture
with no evidence that browser execution was accepted or started. Stdout and
stderr are captured separately; every terminal nonzero exit is reported, and
the retry policy only decides whether a failed invocation is replayed. For
`oracle-pr-review`, which has no timeout replay, this bounds Oracle execution to
at most eleven invocations.

Heartbeat/progress lines may be present in those captures, but they are never
result records and do not relax exact matching. Stdout error classification
uses only the last nonblank `ERROR:` record; stderr busy/timeout classification
still requires the expected text to be the actual last nonblank stderr line.
If later output makes a terminal state ambiguous, the leaf fails closed rather
than discarding progress text to manufacture a retryable or recoverable match.
For `oracle-pr-review`, normal publication success likewise requires
`ORACLE_PR_REVIEW_PUBLISHED` to remain stdout's actual final nonblank line.

Exact final `✖ read ETIMEDOUT` on stderr or `ERROR: read ETIMEDOUT` as stdout's
last nonblank `ERROR:` line remains terminal for `oracle-pr-review`: it is
never replayed because publication may already have happened. The review leaf
uses its exact per-run GitHub correlation marker to recover an already-persisted
`COMMENTED` review instead.

The two explicitly read-only leaves, `oracle-issue-plan` and
`oracle-pr-feedback-plan`, additionally keep `timeout_recovery_used=false`
alongside `busy_retries_used=0`. Only a nonzero Oracle invocation whose exact
terminal error is `read ETIMEDOUT` may set the timeout flag and trigger one
replay of the identical validated Oracle request; a zero exit remains success
even if its output quotes that text. The replay is allowed even if all ten
busy-triggered retries were already consumed. Exact busy responses on the
recovery path still use only the remaining busy budget. A second exact read
timeout is terminal. Because the two budgets are independent, a read-only leaf
can invoke Oracle at most twelve times: the first invocation, up to ten
invocations triggered by busy failures, and one invocation triggered by the
first read timeout. This exception is restricted to prompts that explicitly
prohibit repository and GitHub mutation; it must not be applied to the review
leaf.

`oracle-pr-review` carries a unique hidden correlation marker in its top-level
GitHub review body. After an exact read timeout, the leaf delegates marker
polling to its bundled script instead of having the agent drive each
`gh`/`sleep` step. The 15-minute window is split into at most two bounded
foreground invocations: an initial phase checks immediately and then eight more
times at 60-second intervals (480 seconds), and a continuation phase, used only
after the initial phase returns its exact `CONTINUE` result, performs seven
more reads after seven additional one-minute waits (420 seconds). Together
they preserve 16 total reads over 15 one-minute intervals while keeping each
command below Claude Code's 600-second foreground Bash limit; Claude Code runs
each phase with a 600000 ms tool timeout. The continuation is invoked at most
once, so recovery needs at most one additional model decision rather than
fifteen polling turns.

Publication is recovered only when exactly one persisted `COMMENTED` review
contains that exact per-run marker. The marker is positive proof for that
invocation; review counts, timestamps, partial matches, stdout, or marker
absence are never used to infer publication or non-publication. A GitHub read
failure, multiple exact matches, an unexpected recovery result, or exhaustion
of the bounded recovery window leaves publication indeterminate and blocks the
loop.

For exact busy records, the current Oracle remote server returns HTTP 409
`{"error":"busy"}` before `/runs` acceptance when its single-flight guard is
occupied, while the remote client collapses that status to the error message
before the CLI renders it. The classifier is therefore a pragmatic
best-effort rule rather than protocol-level proof of pre-acceptance. Generic
or embedded `busy` text is never retried, and a post-acceptance error whose
final message is exactly `busy` is a narrow residual collision risk. If
Oracle later exposes a stable pre-acceptance discriminator or durable accepted
run retrieval, prefer that protocol-level contract over replay-based recovery.

## Requirements

- Git and, where the host/triage flow needs it, an authenticated GitHub CLI
  (`gh`) session or equivalent GitHub access;
- Oracle CLI 0.18.0 or newer on the local client and on any configured remote
  `oracle serve` endpoint used by browser routing, with an authenticated
  ChatGPT browser session and the ChatGPT GitHub app authorized for the target
  repository;
- `GPT-5.6 Sol` available to Oracle browser mode.

## Usage

Ask a compatible host to implement an open Issue and carry its pull request
through review, or to review and improve an existing pull request. See
[`skills/oracle-pr-loop/SKILL.md`](skills/oracle-pr-loop/SKILL.md) for the
normative sequencing and stop conditions.
