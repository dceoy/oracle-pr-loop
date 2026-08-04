# Planned command contracts

These interfaces define the stable boundary for follow-up issues. They are
documentation only in issue #15; no executable stub is provided.

## `review`

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` resolves the pull request, binds the operation to the exact current
base and head commits, obtains an independent Oracle/ChatGPT review, validates
the structured verdict, and returns machine-readable JSON.

A valid `APPROVE` or `REQUEST_CHANGES` verdict is a successful command result.
The host agent consumes `REQUEST_CHANGES`, decides how to address the findings,
edits the repository, and runs applicable local validation. The command does
not launch an implementation agent.

Issue #16 owns the implementation and detailed result schema.

## `submit`

```console
python3 skills/loopr/scripts/loopr.py submit --pr <NUMBER_OR_URL> --expected-head <SHA>
```

`submit` verifies pull-request and repository identity, confirms the remote head
still matches `--expected-head`, performs deterministic patch validation,
creates the commit, and pushes with an explicit lease. It returns
machine-readable JSON describing the submitted head and commit.

The host agent owns planning, editing, and repository-specific validation.
`submit` does not decide how to fix review findings.

Issue #17 owns the implementation and detailed result schema.

## Process status

Valid review verdicts are domain results, not runtime failures. Operational
errors, stale state, malformed structured data, and command-contract violations
use a non-zero exit status.
