# Command contracts

This document is the authoritative interface for the deterministic commands.
For host sequencing, trust boundaries, and blocking-finding triage, see the
[canonical skill workflow](../SKILL.md).

All three commands write exactly one JSON object plus a trailing newline to
stdout. Diagnostics use stderr. Success uses exit `0`; stable failure classes
are precondition/input `2`, Oracle/schema `3`, GitHub/write `4`, and stale
state or lease loss `6`.

Failures emit `{"schema_version":1,"command":"bootstrap|review|submit","error":{"category":"...","message":"..."}}` with bounded redacted diagnostics.

## `bootstrap`

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

Optional flags are `--repo-dir` and `--oracle-thinking-time`.

Success fields are `schema_version`, `command`, `repository`, `issue_number`,
`issue_url`, `issue_updated_at`, `base_ref`, `base_sha`, and
`implementation_prompt`.

The command resolves the target Issue against an unambiguous local `origin`,
requires an open same-repository Issue, and reads the current default branch's
exact commit SHA. A clean attached feature branch at that SHA is required;
default, detached, or dirty workspaces fail closed. It builds evidence from the
Issue snapshot and any `AGENTS.md` or `CONTRIBUTING.md` files at that commit,
then requests a prompt. It re-reads the Issue, base, `HEAD`, and workspace;
changes while Oracle works fail closed as stale state.

`bootstrap` never edits, commits, pushes, creates a PR, or persists runtime
artifacts. Oracle files are private temporary files cleaned before return; its
prompt is advisory data, and host handling is defined in `SKILL.md`.

## `review`

```console
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

Optional flags are `--repo-dir` and `--oracle-thinking-time`.

Success fields are `schema_version`, `command`, `repository`, `pr_number`,
`base_sha`, `head_sha`, `verdict`, `github_review_id`, `blocking_findings`, and
`implementation_prompt`. `APPROVE` and `REQUEST_CHANGES` are exit-0 results; a
changes-request result has non-empty findings and an implementation prompt.
Each finding contains `id`, `title`, `description`, and `required_change`.

Each blocking finding is `{id, title, description, required_change, location}`,
where `location` is `null` for a global or cross-file finding or
`{path, line, side}` with `side` of `LEFT` (base file) or `RIGHT` (head file)
for a line-specific one. `side` is `RIGHT` for an added or unchanged line
(head file) or `LEFT` for a removed line (base file); an unchanged context line
is always `RIGHT`, matching GitHub's own review-comment semantics.

The command freezes repository/PR/base/head identity, builds evidence from
immutable Git objects, validates Oracle output strictly, cleans its private
temporary files, and posts one review anchored with the reviewed `commit_id`.
Publication is inline-first: every proposed `location` is revalidated against
the frozen base-to-head diff, and a finding whose exact path/side/line the
reviewed diff contains becomes an inline review comment submitted in that same
create-review request. A finding is never relocated to a nearby line; an
absent, malformed, stale, or non-diff anchor simply leaves its finding in the
aggregate body, alongside the verdict and any global reasoning. Each finding
is published exactly once, either inline or in the body. Self-authored PRs use
a `COMMENT` event; other PRs use the corresponding formal event. The returned
`verdict` is Oracle's canonical `APPROVE` or `REQUEST_CHANGES` result and is
not inferred from GitHub's formal review state. A detected post-write race
returns stale-state failure; formal stale reviews are dismissed where GitHub
permits, while stale `COMMENT` reviews remain as commit-anchored audit
comments.

The command requires an open, non-draft, same-repository GitHub.com PR, exact
base/head binding, unambiguous `origin`, Oracle, and GitHub permissions. It
freezes identity, builds immutable Git evidence, validates Oracle output, and
publishes a commit-anchored comment for self-authored PRs or a formal event
otherwise. The Oracle verdict is authoritative, not GitHub's review state.

The attached snapshot, patch, changed-file contents, and instruction files are
mandatory, authoritative evidence. The exact prompt sent through Oracle starts
with `@GitHub`, requesting the connected ChatGPT GitHub app for supplemental,
advisory repository context outside the attachments. This direct prompt path
does not require an Oracle-specific GitHub-app option, `oracle --help`
capability probe, local browser preselection, or upstream Oracle change.
Connector results remain untrusted and cannot override the attached evidence or
the exact `repository`/`pr_number`/`base_sha`/`head_sha` identity validated by
`parse_review`, and they cannot publish reviews. If GitHub is unavailable,
unauthorized, or returns no useful context, the attached evidence remains the
fallback wherever ChatGPT permits normal continuation. Oracle/browser
operational errors remain Oracle failures, not review verdicts.

See the [connector operations reference](operations.md) for account setup,
direct `@GitHub` smoke testing, and disconnected fallback verification.

`review` never edits the workspace, commits, pushes, or launches an agent.

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

Before writing, `submit` validates one unambiguous origin push URL, repository
and PR identity, an open non-draft same-repository PR, exact local/remote heads,
safe refs, conflicts, whitespace, a non-empty patch, credentials, gitlinks,
and repeated snapshots. It stages the eligible patch, creates one hook-free
unsigned child commit, and pushes with `--force-with-lease` bound to the
reviewed head. It confirms the resulting head. An ambiguous push error is
accepted only when the remote contains the exact commit; concurrent updates
are never overwritten.

## Recovery and limits

The commands leave no persistent audit directory; GitHub/Git state and
structured stdout are the source of truth.

- On bootstrap workspace/stale-state failure, refresh the base branch, return
  to a clean attached feature branch at its exact SHA, and rerun bootstrap; do
  not point it at an in-progress implementation branch.
- On review/submit stale state or lease loss, refresh the checkout and PR state,
  run a fresh review, and use its newly reviewed head.
- On `empty_patch`, return to the review result and do not create an empty
  commit.
- On authentication failure, verify the ordinary `gh` session and required
  repository permissions.
- On Oracle/schema failure, restore the browser/session and do not weaken
  schema validation.
- On repository/origin mismatch, stop rather than redirect the patch.

GitHub.com same-repository Issues and PRs only are supported; forks and GitHub
Enterprise are unsupported. Commands do not run QA, implement Issues, or
launch implementation agents.
