# Command contracts

## `review` — implemented

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

Optional arguments are `--repo-dir`, `--artifacts-dir`, and `--oracle-thinking-time`. The command writes exactly one JSON object followed by a newline to stdout. Subprocess output and diagnostics never share stdout.

### Success

Exit status is `0` for either valid verdict:

```json
{"schema_version":1,"command":"review","repository":"owner/repository","pr_number":123,"base_sha":"40-character SHA","head_sha":"40-character SHA","verdict":"APPROVE","github_review_id":123456789,"blocking_findings":[],"implementation_prompt":null,"artifacts_dir":"/private/path"}
```

For `REQUEST_CHANGES`, `blocking_findings` is a non-empty array of objects with exactly `id`, `title`, `description`, and `required_change`; `implementation_prompt` is a non-empty string for the invoking host agent. The command never launches an implementation agent.

### Error

Operational and contract failures exit non-zero and emit:

```json
{"schema_version":1,"command":"review","error":{"category":"stale_state","message":"bounded redacted diagnostic"}}
```

Detailed diagnostics go to stderr. Stable exit classes are precondition/input `2`, Oracle/schema `3`, GitHub/write `4`, and stale base/head state `6`.

### Snapshot and stale-state behavior

The review is bound to repository, PR number, base SHA, and head SHA. The command re-reads the PR immediately before posting, posts through the GitHub reviews API with `commit_id` set to the frozen head, re-reads the PR after posting, and verifies the created review ID, author, state, and commit. A post-write race is neutralized by dismissing the review where GitHub permits it, then returning a non-zero stale-state error.

### Artifacts

A private run directory contains the normalized snapshot, changed-path list, exact patch, bundle manifest, included text attachments and explicit omission records, Oracle prompt and raw bounded response, validated review, GitHub review metadata, and final result. Known credential values are rejected or redacted and reviewer credentials are not supplied to Oracle.

### Limitations

Only GitHub.com, same-repository, open non-draft PRs are supported. Runtime code uses only the Python standard library. CI status, inline comments, repository edits, commit creation, pushing, agent invocation, and automatic iteration are out of scope.

## `submit` — planned

```console
python3 skills/loopr/scripts/loopr.py submit --pr <NUMBER_OR_URL> --expected-head <SHA>
```

Issue #17 owns implementation of validation, commit creation, and lease-protected push. The host agent owns planning, editing, and local QA.
