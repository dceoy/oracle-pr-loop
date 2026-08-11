---
name: oracle-pr-feedback-plan
description: Triage the existing review feedback on one GitHub pull request through Oracle browser mode and ChatGPT's connected GitHub app, returning advisory dispositions and decision-complete fix plans without modifying the pull request.
allowed-tools: Bash(gh:*), Bash(oracle:*), Bash(which:*)
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
current agent's own triage. Do not retry with a modified prompt if ChatGPT cannot invoke `@GitHub` or access the
target repository.

If Oracle exits non-zero or its response shows that the GitHub app was not invoked or lacked repository access,
report the failure. Otherwise return Oracle's triage without rewriting it.
