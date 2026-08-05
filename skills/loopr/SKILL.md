---
name: loopr
description: Review an exact GitHub pull-request head through Oracle/ChatGPT while the invoking host agent owns implementation work.
---

# loopr

Use this skill to obtain an independent Oracle/ChatGPT review of one exact GitHub pull-request snapshot. The skill does not launch Codex CLI, Claude Code, Cursor CLI, or another implementation agent.

## Implemented command

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` validates an open, non-draft, same-repository GitHub.com pull request; binds the operation to exact base and head commits; builds deterministic evidence from immutable Git objects; strictly validates Oracle output; posts one aggregate review anchored to the reviewed head; and emits exactly one JSON object on stdout.

Both `APPROVE` and `REQUEST_CHANGES` exit `0`. Operational, schema, GitHub, and stale-state failures exit non-zero with a structured error object. Diagnostics are written only to stderr.

## Responsibilities

- **Host agent:** Plan and edit changes, run repository-specific validation, and consume `blocking_findings` and `implementation_prompt`.
- **Oracle/ChatGPT:** Independently review the exact supplied snapshot.
- **Skill scripts:** Resolve identity, construct evidence, validate the verdict, post and revalidate the review, neutralize stale reviews, and write private artifacts.
- **GitHub/Git:** Provide pull-request state and immutable commit objects.

## Prerequisites and limits

Python 3, Git, GitHub CLI, Oracle, Chrome/Chromium, ordinary GitHub read authentication, an authenticated Oracle browser profile, and `GH_REVIEW_TOKEN` for a dedicated reviewer account are required. Numeric PRs require a matching local `origin`; the local checkout supplies immutable Git object reads. The token is scoped only to reviewer identity and review-write calls and is never supplied to Oracle.

Artifacts default to `.pr-loopr/runs/`. CI status is not an approval condition. Fork PRs, GitHub Enterprise, inline comments, repository edits, commit creation, pushing, implementation-agent invocation, and automatic iteration are out of scope.

## Planned command

`submit` remains planned and is owned by issue #17:

```console
python3 skills/loopr/scripts/loopr.py submit --pr <NUMBER_OR_URL> --expected-head <SHA>
```

Until #17 and #18 are complete, the complete vendor-neutral review/fix loop is not implemented. See `references/command-contracts.md` for the public schemas.
