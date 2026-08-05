---
name: loopr
description: Iterate on a pull request through independent Oracle/ChatGPT review while the invoking host agent owns implementation work.
---

# loopr

Use this skill to review and improve one pull request without embedding or
launching a particular implementation agent. The canonical skill is compatible
with Codex CLI, Claude Code, Cursor CLI, and other clients that support agent
skills.

## Workflow

1. Identify the pull request and review its exact current head through
   Oracle/ChatGPT.
2. Consume the structured review result.
3. When the result requests changes, let the invoking host agent plan and edit
   the repository.
4. Let the host agent run the repository's applicable validation.
5. Use deterministic skill scripts to validate and submit the patch.
6. Obtain a fresh Oracle/ChatGPT review for the resulting head.
7. Repeat until approval, an explicit stop condition, or the iteration limit.

## Responsibilities

- **Host agent:** Plan changes, edit the repository, run local validation, and
  decide how to address review findings.
- **Oracle/ChatGPT:** Independently review the exact pull-request head and
  generate a structured verdict.
- **Skill scripts:** Deterministically inspect pull-request state, transport
  reviews, validate patches, create commits, push updates, and return
  machine-readable results.
- **GitHub/Git:** Provide pull-request identity, commit state, reviews, and
  remote branch updates.

Production skill code must not launch, select, or detect Codex CLI, Claude Code,
Cursor CLI, or another host agent.

## Planned command boundary

The stable command boundary is documented now and implemented by follow-up
issues:

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
python3 skills/loopr/scripts/loopr.py submit --pr <NUMBER_OR_URL> --expected-head <SHA>
```

Both commands return machine-readable JSON. `review` owns the exact-head
Oracle/ChatGPT review path. The host agent owns implementation changes.
`submit` owns deterministic validation, commit creation, and lease-protected
push.

Valid `APPROVE` and `REQUEST_CHANGES` review results are successful command
results. Operational failures and contract violations use a non-zero exit
status.

See `references/command-contracts.md` for the documentation-only command
contracts. Issue #16 will implement `review`; issue #17 will implement `submit`.
