# Command contracts

All three commands write exactly one JSON object plus a trailing newline to stdout. Diagnostics use stderr. Success uses exit `0`; stable failure classes are precondition/input `2`, Oracle/schema `3`, GitHub/write `4`, and stale state or lease loss `6`.

## `bootstrap`

```console
python3 skills/pr-review-loop/scripts/cli.py bootstrap --issue <NUMBER_OR_URL>
```

Optional flags are `--repo-dir`, `--artifacts-dir`, and `--oracle-thinking-time`.

Success fields are `schema_version`, `command`, `repository`, `issue_number`, `issue_url`, `issue_updated_at`, `base_ref`, `base_sha`, `implementation_prompt`, and `artifacts_dir`.

The command resolves the target Issue, requires it to be open and same-repository, reads the repository's current default branch and its exact commit SHA, and requires the local checkout to already be clean and at that exact commit (`workspace` precondition otherwise, naming the fetch/switch remedy) before building bounded repository evidence (the Issue snapshot plus any `AGENTS.md`/`CONTRIBUTING.md` instruction files) at that immutable base commit and requesting one Oracle-generated implementation prompt bound to the Issue and base SHA. It then re-reads the Issue, the base branch, and local `HEAD`; if the Issue's `updatedAt`, title, body, or bounded comments, the base branch's name or SHA, or local `HEAD` changed while Oracle was working, the command fails with stale-state semantics instead of returning a prompt for state that no longer exists. If the Issue was closed instead, the re-read fails its own open-state precondition rather than stale-state.

Bootstrap artifacts include the initial Issue snapshot, bundle manifest, instruction-file attachments, Oracle prompt/raw output, validated bootstrap result, and final result. `bootstrap` never edits, commits, pushes, or creates a pull request; implementation, repository QA, and PR creation remain the invoking host agent's responsibility.

## `review`

```console
python3 skills/pr-review-loop/scripts/cli.py review --pr <NUMBER_OR_URL>
```

Optional flags are `--repo-dir`, `--artifacts-dir`, and `--oracle-thinking-time`.

Success fields are `schema_version`, `command`, `repository`, `pr_number`, `base_sha`, `head_sha`, `verdict`, `github_review_id`, `blocking_findings`, `implementation_prompt`, and `artifacts_dir`. `APPROVE` and `REQUEST_CHANGES` are both exit-0 results. A changes-request result has non-empty blocking findings and an implementation prompt for the host agent.

The command freezes repository/PR/base/head identity, builds evidence from immutable Git objects, validates Oracle output strictly, posts one aggregate review anchored with the reviewed `commit_id`, then revalidates the PR and posted review. A detected post-write race is dismissed where GitHub permits and returns stale-state failure.

Review artifacts include the frozen snapshot, changed paths, patch, evidence manifest/content, Oracle input/output, validated review, GitHub review metadata, and final result.

## `submit`

```console
python3 skills/pr-review-loop/scripts/cli.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

Optional flags are `--repo-dir` and `--artifacts-dir`.

Success fields are `schema_version`, `command`, `repository`, `pr_number`, `base_sha`, `previous_head_sha`, `resulting_head_sha`, `commit_sha`, `pushed_branch`, and `artifacts_dir`. The resulting head equals the created commit.

Before writing, `submit` validates one origin push URL, repository identity, same-repository open/non-draft state, exact expected local/remote head, safe refs, conflicts, whitespace, non-empty staged content, known credentials, gitlinks, and repeated base/head snapshots. It creates exactly one hook-free unsigned child commit and pushes only with an explicit `--force-with-lease` bound to the reviewed head. A concurrent branch update is never overwritten; an ambiguous push error is accepted only when the remote already contains the exact created commit.

Submit artifacts include the initial snapshot, staged patch, commit metadata, push metadata, and final result.

## Error envelope and limits

Failures emit `{"schema_version":1,"command":"bootstrap|review|submit","error":{"category":"...","message":"..."}}` with bounded redacted diagnostics.

Runtime dependencies are Python standard library plus the required external commands. GitHub.com same-repository Issues and PRs only; forks and GitHub Enterprise are unsupported. The skill does not run repository QA, implement an Issue, create a pull request, or launch an implementation agent.
