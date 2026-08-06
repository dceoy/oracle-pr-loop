# Command contracts

## `review` — implemented

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

Optional arguments are `--repo-dir`, `--artifacts-dir`, and `--oracle-thinking-time`. The command writes exactly one JSON object followed by a newline to stdout. Subprocess output and diagnostics never share stdout.

### Success

Exit status is `0` for either valid verdict:

```json
{
  "schema_version": 1,
  "command": "review",
  "repository": "owner/repository",
  "pr_number": 123,
  "base_sha": "40-character SHA",
  "head_sha": "40-character SHA",
  "verdict": "APPROVE",
  "github_review_id": 123456789,
  "blocking_findings": [],
  "implementation_prompt": null,
  "artifacts_dir": "/private/path"
}
```

For `REQUEST_CHANGES`, `blocking_findings` is a non-empty array of objects with exactly `id`, `title`, `description`, and `required_change`; `implementation_prompt` is a non-empty string for the invoking host agent. The command never launches an implementation agent.

### Error

Operational and contract failures exit non-zero and emit:

```json
{
  "schema_version": 1,
  "command": "review",
  "error": {
    "category": "stale_state",
    "message": "bounded redacted diagnostic"
  }
}
```

Detailed diagnostics go to stderr. Stable exit classes are precondition/input `2`, Oracle/schema `3`, GitHub/write `4`, and stale base/head state `6`.

### Snapshot and stale-state behavior

The review is bound to repository, PR number, base SHA, and head SHA. The command re-reads the PR immediately before posting, posts through the GitHub reviews API with `commit_id` set to the frozen head, re-reads the PR after posting, and verifies the created review ID, author, state, and commit. A post-write race is neutralized by dismissing the review where GitHub permits it, then returning a non-zero stale-state error.

### Artifacts

A private run directory contains the normalized snapshot, changed-path list, exact patch, bundle manifest, included text attachments and explicit omission records, Oracle prompt and raw bounded response, validated review, GitHub review metadata, and final result. Known credential values are rejected or redacted and reviewer credentials are not supplied to Oracle.

### Limitations

Only GitHub.com, same-repository, open non-draft PRs are supported. Runtime code uses only the Python standard library. CI status, inline comments, repository edits, commit creation, pushing, agent invocation, and automatic iteration are out of scope.

## `submit` — implemented

```console
python3 skills/loopr/scripts/loopr.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

Optional arguments are `--repo-dir` and `--artifacts-dir`. The host agent owns planning, editing, and local QA. `submit` owns only deterministic validation, one commit, and the remote branch write.

### Success

Exit status is `0` after GitHub exposes the resulting PR head:

```json
{
  "schema_version": 1,
  "command": "submit",
  "repository": "owner/repository",
  "pr_number": 123,
  "base_sha": "40-character SHA",
  "previous_head_sha": "40-character SHA",
  "resulting_head_sha": "40-character SHA",
  "commit_sha": "40-character SHA",
  "pushed_branch": "feature",
  "artifacts_dir": "/private/path"
}
```

The resulting head SHA and commit SHA are identical.

### Error

Failures emit the same bounded error envelope with `"command":"submit"`. Input, repository, workspace, patch, commit, and credential failures use exit `2`; GitHub and push failures use exit `4`; stale snapshots, stale expected heads, and lease loss use exit `6`.

### Validation and write behavior

Before commit or push, the command verifies that `origin` has exactly one configured push URL and that its fetch and push URLs identify the target GitHub.com repository; the PR is open, non-draft, and same-repository; the remote PR head and local `HEAD` equal `--expected-head`; the workspace contains changes and no unresolved conflicts; tracked and staged patches pass `git diff --check`; the staged patch is non-empty and does not contain a known credential value; and the PR base/head remain unchanged across repeated snapshots.

The command stages with `git add --all`, creates one commit with hooks and signing disabled, verifies that the commit is a direct child of the expected head, revalidates the remote branch, and pushes with:

```console
git push --no-verify \
  --force-with-lease=refs/heads/<branch>:<expected-head> \
  origin <commit>:refs/heads/<branch>
```

A concurrent branch update causes the explicit lease to fail and is never overwritten. If the push process reports an error after the remote already accepted the exact created commit, the command recognizes that state and continues GitHub confirmation; a third SHA remains a lease-loss failure. The command then polls GitHub until the PR head equals the created commit.

### Artifacts and limits

A private run directory records the initial PR snapshot, bounded staged patch, commit metadata, push metadata, and final result. Runtime dependencies remain in the Python standard library. The command does not create worktrees, manage a control repository, inspect unrelated worktrees, run tests, invoke model processes, or attempt to contain the host agent.
