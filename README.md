# loopr

`loopr` is a vendor-neutral agent skill for improving one GitHub pull request
through independent Oracle/ChatGPT review. The host agent owns planning, editing,
and repository QA; deterministic skill commands own review transport, patch
validation, commit creation, and lease-protected pushes.

## Supported clients

The canonical skill lives at `skills/loopr/`. Supported hosts discover that same
directory through symlinks:

- Codex CLI: `.agents/skills/loopr`
- Claude Code: `.claude/skills/loopr`
- Cursor CLI: `.agents/skills/loopr`

There is no client-specific copy, runtime fork, or repository-root compatibility
CLI. Client-specific discovery instructions, when needed, point to the canonical
skill rather than changing production code.

## Quick start

### 1. Prepare the checkout and reviewer

Requirements:

- macOS or Linux;
- Python 3.10 or newer;
- Git and authenticated GitHub CLI;
- an `origin` matching the target GitHub.com repository;
- an open, non-draft, same-repository pull request;
- push access and configured Git commit identity for `submit`;
- Oracle with Chrome or Chromium;
- a persistent Oracle browser profile authenticated to ChatGPT;
- `GH_REVIEW_TOKEN` for a dedicated reviewer account that differs from the PR
  author.

Initialize Oracle's browser profile once:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The default profile is `~/.oracle/browser-profile`. Set
`ORACLE_BROWSER_PROFILE_DIR` to use another persistent profile.

### 2. Review the exact PR head

```console
export GH_REVIEW_TOKEN='...'
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` emits exactly one JSON object on stdout. Both valid verdicts are
successful process results:

- `APPROVE`: finish successfully.
- `REQUEST_CHANGES`: the host agent implements only the returned
  `blocking_findings`, using `implementation_prompt` as reviewer guidance.

Operational, schema, GitHub, and stale-state failures return non-zero status and
must be resolved before continuing.

### 3. Let the host edit and validate

After `REQUEST_CHANGES`, the invoking Codex CLI, Claude Code, Cursor CLI, or
other compatible host edits the current checkout and runs the repository's
normal QA. `loopr` does not launch an implementation agent and does not select or
run repository QA.

### 4. Submit against the reviewed head

```console
python3 skills/loopr/scripts/loopr.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <REVIEWED_HEAD_SHA>
```

`submit` verifies repository identity, local and remote head binding, conflicts,
whitespace, patch content, known credential values, and repeated base/head
snapshots. It stages the complete intended patch, creates one hook-free unsigned
commit, pushes with an explicit force-with-lease bound to the reviewed head, and
confirms the resulting GitHub PR head.

### 5. Re-review and stop deterministically

Run a fresh `review` against the resulting head. Repeat the host edit/QA and
`submit` sequence only when the fresh verdict is `REQUEST_CHANGES`.

Choose an iteration limit before starting. Finish only on a fresh `APPROVE`;
otherwise stop when the configured limit is reached. The host owns this loop and
must not manufacture approval after a limit or operational failure.

## Contracts and artifacts

The stable version-1 success and error schemas, exit classes, race behavior, and
artifact contracts are documented in
`skills/loopr/references/command-contracts.md`.

Each command writes bounded, permission-restricted audit artifacts below
`.pr-loopr/runs/` by default. Review artifacts record the frozen PR snapshot,
review evidence, validated Oracle result, GitHub review metadata, and final
result. Submit artifacts record the staged patch, commit metadata, push metadata,
and final result.

For executable cross-client smoke tests, troubleshooting, reviewer setup, and
stale-head recovery, see `skills/loopr/references/operations.md`.

## Safety and limitations

- `review` never edits, commits, pushes, or launches an implementation agent.
- `submit` never plans changes, interprets findings, runs tests, or launches a
  model process.
- Known credential values are rejected or redacted from review and audit paths.
- Reviews are anchored to the exact reviewed commit; pushes are bound to the
  expected head with an explicit lease.
- GitHub.com only; GitHub Enterprise and fork pull requests are unsupported.
- CI status is not an approval gate.
- The runtime does not sandbox or contain the host agent.

## Migration note

The legacy migration removed the 4,679-line repository-root `loopr.py`
orchestrator, reducing root production Python from 4,679 lines to 0. Surviving
production code lives only under `skills/loopr/scripts/`.

The completed implementation order is #15 → (#16 and #17) → #18 → #19.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check skills/loopr
uv run ruff format --check skills/loopr
uv run pyright
```
