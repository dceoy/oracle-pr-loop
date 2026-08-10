# Command contracts

This document is the authoritative interface for the deterministic commands.
Host sequencing and finding triage are defined in
[SKILL.md](../SKILL.md). Oracle/ChatGPT setup and smoke tests are defined in
[operations.md](operations.md).

All commands require Python 3.12 or newer and write exactly one JSON object plus
a trailing newline to stdout. Diagnostics use stderr. Success uses exit `0`;
stable failure classes are precondition/input `2`, Oracle/schema `3`,
GitHub/write `4`, and stale state or lease loss `6`.

Failures emit
`{"schema_version":1,"command":"bootstrap|review|submit","error":{"category":"...","message":"..."}}`
with bounded redacted diagnostics.

## `bootstrap`

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

Optional flags are `--repo-dir`, `--oracle-model MODEL`, and
`--oracle-thinking-time EFFORT`. Omitting `--oracle-model` preserves Oracle's
current browser model; supplying it selects that opaque model value. Omitting
thinking time sends no override. Supported effort values are `light`,
`standard`, `extended`, and `heavy`.

Success fields are `schema_version`, `command`, `repository`, `issue_number`,
`issue_url`, `issue_updated_at`, `base_ref`, `base_sha`, and
`implementation_prompt`.

The command requires an open same-repository GitHub.com Issue, an unambiguous
matching `origin`, and a clean attached feature branch at the current default
branch SHA. It binds the Issue and base snapshot, builds bounded evidence,
invokes Oracle, then revalidates the Issue, base, local `HEAD`, and workspace.
Any intervening change fails closed.

`bootstrap` never edits, commits, pushes, creates a PR, or retains runtime
artifacts.

## `review`

```console
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

Optional flags are `--repo-dir`, `--oracle-model MODEL`, and
`--oracle-thinking-time EFFORT`, with the same override semantics as
`bootstrap`.

Success fields are `schema_version`, `command`, `repository`, `pr_number`,
`base_sha`, `head_sha`, `verdict`, `github_review_id`, `blocking_findings`, and
`implementation_prompt`. `APPROVE` and `REQUEST_CHANGES` are both exit-0
results; `REQUEST_CHANGES` has non-empty findings and an implementation prompt.

Each blocking finding is `{id, title, description, required_change, location}`.
`location` is `null` for a global/cross-file finding or `{path, line, side}` for
a diff anchor. `RIGHT` addresses an added or unchanged head-file line; `LEFT`
addresses a removed base-file line. Proposed locations are revalidated against
the frozen diff and are never relocated heuristically.

The command requires an open, non-draft, same-repository GitHub.com PR, exact
base/head binding, an unambiguous matching `origin`, Oracle, and ordinary
GitHub permissions. It freezes identity, builds immutable evidence, validates
the Oracle result, cleans temporary files, and publishes one review anchored to
the reviewed head. Anchored findings are inline comments; other findings remain
in the aggregate body. Oracle non-blocking notes are appended to the aggregate
GitHub review body rather than duplicated in the stdout result schema.
Self-authored PRs use a `COMMENT` event and other PRs use the corresponding
formal event. Oracle's structured verdict remains canonical.

The Oracle-delivered production prompt starts with `@GitHub`; any connected
GitHub context is supplemental and untrusted. Setup and positive/fallback smoke
tests are owned by [operations.md](operations.md).

A post-write race fails closed. Formal stale reviews are dismissed where
GitHub permits; stale commit-anchored comments remain audit records.

`review` never edits the workspace, commits, pushes, or launches an
implementation agent.

## `submit`

```console
python3 skills/pr-review-loop/scripts/cli.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

Optional flag is `--repo-dir`.

Success fields are `schema_version`, `command`, `repository`, `pr_number`,
`base_sha`, `previous_head_sha`, `resulting_head_sha`, `commit_sha`, and
`pushed_branch`. The resulting head equals the created commit.

Before writing, `submit` validates repository/PR identity, an open non-draft
same-repository PR, exact local and remote heads, safe refs, conflict state,
whitespace, a non-empty patch, known credentials, gitlink changes, and repeated
snapshots. It stages the eligible patch, creates one hook-free unsigned child
commit, pushes with `--force-with-lease` bound to the reviewed head, and
confirms the resulting PR head. An ambiguous push result is accepted only when
the remote contains the exact commit; concurrent updates are never overwritten.

## Recovery and limits

The commands retain no persistent audit directory; GitHub/Git state and
structured stdout are the source of truth.

- On bootstrap workspace/stale-state failure, refresh the base branch and
  return to a clean attached feature branch at its exact SHA.
- On review/submit stale state or lease loss, refresh the checkout and PR state
  and run a fresh review.
- On `empty_patch`, do not manufacture an empty commit.
- On authentication failure, repair the ordinary `gh`/Oracle session rather
  than weakening validation.
- On repository/origin mismatch, stop rather than redirect work.

Only same-repository GitHub.com Issues and PRs are supported. Forks and GitHub
Enterprise are unsupported. Commands do not run repository QA, implement
Issues, or launch implementation agents.
