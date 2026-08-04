# loopr

`loopr` is a synchronous, local Oracle–ChatGPT–Codex review loop for a single
GitHub pull request. It snapshots an exact PR head, asks ChatGPT web for an
independent aggregate review through Oracle, posts that review with a dedicated
reviewer identity, and lets Codex address validated blockers in a disposable Git
worktree. The orchestrator owns validation, commits, and lease-protected pushes;
Codex never receives GitHub authority.

This v1 is intentionally for trusted internal repositories. Pull-request text,
code, tests, and model output are handled as untrusted data, but the tool is not
a safe way to execute arbitrary hostile build systems. Review repository-local
instructions and test commands before using it on code you do not trust.

## Prerequisites

- Python 3.10 or newer (the orchestrator uses only the standard library)
- Node.js 24 or newer
- `git` and an authenticated, push-capable `origin` for the PR repository
- [GitHub CLI](https://cli.github.com/) authenticated for read operations
- [Oracle](https://github.com/steipete/oracle) and Google Chrome/Chromium
- [Codex CLI](https://github.com/openai/codex) with `codex login` completed
- A dedicated GitHub reviewer account with repository access and a token in
  `GH_REVIEW_TOKEN`; it must be different from the PR author
- A Linux host; other platforms are rejected before any subprocess starts
  because they cannot provide the required descendant containment guarantee

Only same-repository, non-draft, open GitHub.com PRs are supported. The local
user must be able to push the PR head branch. The reviewer account must have
repository administrator access so it can dismiss a stale review after a
base/head race, and the reviewer token must be authorized to create and dismiss
pull request reviews: for a fine-grained personal access token, grant
repository permission "Pull requests: Read and write"; classic tokens need the
`repo` scope. A token scoped to read-only access
passes the collaborator-permission precheck but is rejected by GitHub with a
403 the first time it tries to post a review.

## One-time Oracle browser login

Initialize Oracle's persistent manual-login profile and sign in to ChatGPT in
the Chrome window:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The default profile is `~/.oracle/browser-profile`. Set
`ORACLE_BROWSER_PROFILE_DIR` or Oracle's `browser.manualLoginProfileDir` config
when using another location.

## Usage

Export the dedicated reviewer's token without writing it to a file, then run:

```console
export GH_REVIEW_TOKEN='...'
python3 loopr.py --pr 123
```

`--pr` accepts either a number inferable from local `origin` or a canonical URL:

```console
python3 loopr.py --pr https://github.com/OWNER/REPO/pull/123
```

Options:

- `--repo-dir DIR`: local checkout, defaulting to the current directory.
- `--max-iterations N`: maximum number of fresh Oracle reviews, default `5`.
  If the last allowed review requests changes, the tool stops without creating
  an unreviewable final patch.
- `--oracle-thinking-time {light,standard,extended,heavy}`: default `heavy`.
- `--artifacts-dir DIR`: repository-relative audit root, default
  `.pr-loopr`.
- `--dry-run`: check dependencies, authentication, PR identity, permissions,
  push authentication and lease freshness, and locking without invoking
  ChatGPT/Codex or changing a remote or worktree; see
  [Limitations](#limitations) for what the pushability check does not cover.

Each run writes deterministic, permission-restricted audit material below
`.pr-loopr/runs/`, including exact PR metadata and patch, a changed-file
manifest, complete changed text, explicit binary-file entries, Oracle input and
output, the posted review, Codex events/final response when applicable, the
resulting binary patch, pushed SHA, versions, and state transitions. Diagnostics
are redacted, but any known credential collision in code-bearing review content
fails closed instead of changing what Oracle or GitHub receives.

## Exit codes

| Code | Meaning                                                             |
| ---: | ------------------------------------------------------------------- |
|  `0` | GitHub confirms `APPROVED` for the exact reviewed head SHA.         |
|  `2` | Invalid input, dependency/context limit, or failed precondition.    |
|  `3` | Oracle failed or returned malformed, ambiguous, or stale output.    |
|  `4` | GitHub review/write/verification failed, including policy blockage. |
|  `5` | Codex failed or its patch failed safety validation.                 |
|  `6` | The remote PR head changed during a protected operation.            |
|  `7` | Maximum iterations were reached or implementation stalled.          |

All error paths fail closed. There is no automatic merge, issue-comment
fallback, retry/repair of model JSON, inline comment posting, CI remediation,
fork support, daemon mode, or human-thread resolution.

## Security boundaries

- `GH_REVIEW_TOKEN` is required, removed from general subprocess environments,
  mapped to `GH_TOKEN` only for individual reviewer `gh` calls, and never stored.
- Oracle and Codex receive small allowlisted environments without GitHub,
  cloud, package-registry, SSH-agent, database URL, or arbitrary host
  variables. The allowlist still passes through the real `HOME` and `PATH`;
  see [Limitations](#limitations).
- Oracle uses a fresh one-shot browser conversation for every head SHA, current
  ChatGPT model selection, configured thinking time, and automatic successful
  one-shot archiving.
- Oracle output must match one strict JSON object. Approval cannot contain
  blockers; requested changes must contain concrete blockers and an
  implementation prompt.
- Codex runs with `--sandbox workspace-write --ephemeral`, ignores user config
  and execution-policy rules (preventing configured MCP/plugin authority), has
  no network authority, and receives fixed guardrails before the untrusted
  reviewer task. `workspace-write` confines writes to the worktree; it does not
  confine reads. See [Limitations](#limitations).
- The primary checkout is not reset, cleaned, staged, or committed. Authenticated
  Git fetch, race checks, dry-run pushes, and real pushes run from a private
  bare control repository with an empty trusted working directory; the primary
  checkout is only inspected for identity, ignore state, and outside-worktree
  mutations. Codex edits only `.pr-loopr/worktrees/pr-N/`.
- The orchestrator rejects dirty/conflicting loop worktrees, whitespace errors,
  new nested Git repositories, submodule URL changes, no-op patches, and changed
  outside-worktree state. A final `--force-with-lease` prevents overwriting a
  concurrently updated PR head.
- Context fails closed above 100 changed files, 2 MiB of patch text, or 20 MiB
  of attached text. Files are never silently truncated.

## Limitations

- Codex's `--sandbox workspace-write` restricts writes to the worktree but
  does not confine filesystem reads. Codex also inherits the operator's real
  `HOME` and `PATH`. A prompt-injected task can therefore read files outside
  the worktree or invoke locally reachable credential helpers. Review content
  containing values already present in the orchestrator's captured environment
  (`CommandRunner._secrets`) is rejected rather than redacted; values discovered
  by Codex itself are not knowable to this check.
  Only run this tool against pull requests you would trust with read access
  to the operator's account.
- The Linux containment path is exercised by the repository CI workflow, but
  CI status is not itself an approval gate: GitHub approval is
  verified for the exact reviewed head SHA, while a red or absent check suite
  does not block approval.
- The pushability precheck only proves push authentication and
  force-with-lease staleness detection against the current head SHA.
  `git push --dry-run` never reaches the server's hook/branch-protection
  phase, even for a push that would land a genuinely new commit, so it
  cannot predict whether the real push after Oracle/Codex will be accepted.
  A policy rejection there surfaces as a failure at that push, not here.
- On Linux, every subprocess runs under a dedicated supervisor that enables
  `PR_SET_CHILD_SUBREAPER` before the payload starts. The supervisor is the
  payload's sole parent, adopts descendants that double-fork or call
  `setsid()`, and kills and reaps the complete child tree before reporting a
  result on success, failure, timeout, overflow, or interruption. Descendants
  are terminated through stable Linux pidfds rather than reused numeric PIDs.
  Missing `prctl`, `/proc` child enumeration, or pidfd support, and any cleanup
  uncertainty, fail closed before the caller can use the result. Non-Linux
  platforms are rejected rather than using a weaker process-group fallback.
- The reviewer-permission precheck requires an administrator reviewer account
  for stale-review dismissal. The reviewer token must also have the pull-request
  review write grant; GitHub may still reject an under-scoped token at the API,
  which surfaces as exit code `4`.

The single-file implementation is longer than the original 250–400-line sizing
target because the issue's mandatory safety boundaries require explicit bounded
subprocess handling, token-scoped environments, path and schema validation,
worktree conflict detection, deterministic attachment construction, audit state,
and race-safe Git operations. Keeping those checks visible in `loopr.py`
is preferable to hiding core behavior in undeclared dependencies or services.

## Tests

Run the standard-library suite with:

```console
python3 -m unittest discover -s tests -v
```
