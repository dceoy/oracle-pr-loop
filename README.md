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

All three Oracle leaf skills allow six retry opportunities (seven total
invocations) only for invocations that exited unsuccessfully with an exact
busy record: either stderr's last nonblank line is `✖ busy`, or stdout's last
nonblank `ERROR:` line is exactly `ERROR: busy`. Each retry additionally
requires that capture to be complete and to contain no evidence that browser
execution was accepted or started. Stdout and stderr are captured separately;
every terminal nonzero exit is reported, and the retry policy only decides
whether a failed invocation is replayed.

Exact final `✖ read ETIMEDOUT` on stderr or `ERROR: read ETIMEDOUT` as stdout's
last nonblank `ERROR:` line is terminal in every leaf and is never replayed. A
read timeout can occur after the remote `/runs` request was accepted and after
ChatGPT received the prompt while the server-side browser run continues. Even
the read-only planning and triage leaves therefore fail closed instead of
starting a second run that could duplicate ChatGPT work or immediately collide
with the still-active run as `busy`. For `oracle-pr-review`, the timeout also
leaves GitHub publication state indeterminate, so no captured-stdout marker is
accepted as a trusted publication receipt.

For exact busy records, the current Oracle remote server returns HTTP 409
`{"error":"busy"}` before `/runs` acceptance when its single-flight guard is
occupied, while the remote client collapses that status to the error message
before the CLI renders it. The classifier is therefore a pragmatic
best-effort rule rather than protocol-level proof of pre-acceptance. Generic
or embedded `busy` text is never retried, and a post-acceptance error whose
final message is exactly `busy` is a narrow residual collision risk. If
Oracle later exposes a stable pre-acceptance discriminator, prefer it over
the CLI-text classifier.

## Requirements

- Git and, where the host/triage flow needs it, an authenticated GitHub CLI
  (`gh`) session or equivalent GitHub access;
- the `oracle` CLI, with an authenticated ChatGPT browser session and the
  ChatGPT GitHub app authorized for the target repository;
- `GPT-5.6 Sol` available to Oracle browser mode.

## Usage

Ask a compatible host to implement an open Issue and carry its pull request
through review, or to review and improve an existing pull request. See
[`skills/oracle-pr-loop/SKILL.md`](skills/oracle-pr-loop/SKILL.md) for the
normative sequencing and stop conditions.
