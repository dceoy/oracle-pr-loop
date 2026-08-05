---
name: loopr
description: Review and improve a pull request through independent Oracle/ChatGPT review while the invoking host agent owns implementation work.
---

# loopr

Use this skill to review and improve one pull request without embedding or launching a particular implementation agent. The canonical skill is compatible with Codex CLI, Claude Code, Cursor CLI, and other clients that support agent skills.

## Workflow

1. Identify the pull request and review its exact current head through Oracle/ChatGPT.
2. Consume the structured review result.
3. When the result is `REQUEST_CHANGES`, let the invoking Host agent plan and edit the repository.
4. Let the Host agent run the repository's applicable validation.
5. Use deterministic Skill scripts to validate and submit the patch.
6. Obtain a fresh Oracle/ChatGPT review for the resulting head.
7. Repeat until `APPROVE`, an explicit stop condition, or the iteration limit.

## Responsibilities

- **Host agent:** Plan changes, edit the repository, run local validation, and decide how to address review findings.
- **Oracle/ChatGPT:** Independently review the exact pull-request head and generate a structured verdict.
- **Skill scripts:** Deterministically inspect pull-request state, construct evidence, transport reviews, validate patches, and return machine-readable results.
- **GitHub/Git:** Provide pull-request identity, immutable commit state, reviews, and remote branch updates.

Production skill code must not launch, select, or detect Codex CLI, Claude Code, Cursor CLI, or another host agent.

## Implemented command

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` validates an open, non-draft, same-repository GitHub.com pull request; binds the operation to exact base and head commits; builds deterministic evidence from immutable Git objects; strictly validates Oracle output; posts one aggregate review anchored to the reviewed head; and emits exactly one machine-readable JSON object on stdout.

Both `APPROVE` and `REQUEST_CHANGES` are successful exit-zero domain results. Operational, schema, GitHub, and stale-state failures use a non-zero exit status and a structured error object. Diagnostics are written only to stderr.

## Prerequisites and limits

Python 3, Git, GitHub CLI, Oracle, Chrome/Chromium, ordinary GitHub read authentication, an authenticated Oracle browser profile, and `GH_REVIEW_TOKEN` for a dedicated reviewer account are required. Numeric PRs require a matching local `origin`; the local checkout supplies immutable Git object reads. The reviewer token is supplied only to reviewer identity and review-write calls and is never supplied to Oracle.

Artifacts default to `.pr-loopr/runs/`. CI status is not an approval condition. Fork PRs, GitHub Enterprise, inline comments, repository edits, commit creation, pushing, implementation-agent invocation, and automatic iteration are out of scope for `review`.

## Planned command

`submit` remains planned and is owned by issue #17:

```console
python3 skills/loopr/scripts/loopr.py submit --pr <NUMBER_OR_URL> --expected-head <SHA>
```

Until #17 and #18 are complete, the complete vendor-neutral review/fix loop is not implemented. See `references/command-contracts.md` for the public schemas.
