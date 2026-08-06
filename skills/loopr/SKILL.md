---
name: loopr
description: Review and improve a pull request through independent Oracle/ChatGPT review while the invoking host agent owns implementation work.
---

# loopr

Use this skill to review and improve one pull request without embedding or launching a particular implementation agent. The canonical skill is compatible with Codex CLI, Claude Code, Cursor CLI, and other clients that support agent skills.

## Workflow

1. Identify the pull request and review its exact current head through Oracle/ChatGPT.
2. Consume the structured review result.
3. When the result is `REQUEST_CHANGES`, let the invoking host agent plan and edit the repository.
4. Let the host agent run the repository's applicable validation.
5. Submit the complete workspace patch against the reviewed head.
6. Obtain a fresh Oracle/ChatGPT review for the resulting head.
7. Repeat until `APPROVE`, an explicit stop condition, or the iteration limit.

## Responsibilities

- **Host agent:** Plan changes, edit the repository, run local validation, and decide how to address review findings.
- **Oracle/ChatGPT:** Independently review the exact pull-request head and generate a structured verdict.
- **Skill scripts:** Deterministically inspect pull-request state, construct evidence, transport reviews, validate patches, create one commit, and perform a lease-protected push.
- **GitHub/Git:** Provide pull-request identity, immutable commit state, reviews, and remote branch updates.

Production skill code must not launch, select, or detect Codex CLI, Claude Code, Cursor CLI, or another host agent.

## Commands

Review one exact PR head:

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` validates an open, non-draft, same-repository GitHub.com pull request; binds the operation to exact base and head commits; builds deterministic evidence from immutable Git objects; strictly validates Oracle output; posts one aggregate review anchored to the reviewed head; and emits exactly one machine-readable JSON object on stdout.

Submit the host agent's complete workspace patch:

```console
python3 skills/loopr/scripts/loopr.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

`submit` requires local `HEAD` and the remote PR head to equal `--expected-head`. It rejects repository mismatches, forks, drafts, conflicts, whitespace failures, empty patches, unsafe refs, known credential values, and base/head races. It stages the complete patch, creates one hook-free unsigned commit, pushes the PR branch with an explicit force-with-lease bound to the expected head, confirms GitHub exposes the resulting SHA, and emits one machine-readable JSON object.

Operational and stale-state failures use non-zero exit statuses and structured error objects. Diagnostics are written only to stderr.

## Prerequisites and limits

Both commands require Python 3, Git, GitHub CLI, ordinary GitHub read authentication, a matching local `origin`, and an open non-draft same-repository GitHub.com PR. `submit` additionally requires push access and configured Git commit identity. `review` additionally requires Oracle, Chrome/Chromium, an authenticated Oracle browser profile, and `GH_REVIEW_TOKEN` for a dedicated reviewer account.

Artifacts default to `.pr-loopr/runs/`. CI status is not an approval condition. Fork PRs and GitHub Enterprise are unsupported. `review` does not edit, commit, push, or launch an implementation agent. `submit` does not plan changes, edit files, run repository QA, interpret review findings, or invoke an implementation agent. Automatic iteration remains outside these commands until issue #18 replaces the legacy root orchestrator. See `references/command-contracts.md` for the public schemas.
