# loopr

`loopr` is a vendor-neutral agent skill for improving one GitHub pull request
through independent Oracle/ChatGPT review. The invoking host agent owns planning,
editing, and repository validation; deterministic skill commands own review
transport, patch validation, commit creation, and lease-protected pushes.

The canonical implementation lives under `skills/loopr/`. Compatible hosts
discover it through:

- `.agents/skills/loopr` for Codex CLI, Cursor CLI, and compatible clients;
- `.claude/skills/loopr` for Claude Code.

No implementation agent is launched by the runtime code, and no compatibility
CLI is provided at the repository root.

## Workflow

1. Review the exact current PR head.
2. When the verdict is `REQUEST_CHANGES`, let the host agent implement the
   returned blocking findings and run the repository's QA workflow.
3. Submit the complete workspace patch against the reviewed head.
4. Review the resulting head and repeat until `APPROVE` or an explicit stop
   condition.

## Requirements

- macOS or Linux
- Python 3.10 or newer
- Git and GitHub CLI with ordinary read authentication
- an `origin` matching the target repository
- Oracle with Chrome or Chromium and an authenticated browser profile
- a dedicated reviewer account, different from the PR author
- `GH_REVIEW_TOKEN` with pull-request review write permission
- push access and configured Git commit identity when using `submit`

Only open, non-draft, same-repository GitHub.com pull requests are supported.
Fork pull requests and GitHub Enterprise are not supported.

Initialize Oracle's persistent browser profile before the first review:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The default profile is `~/.oracle/browser-profile`. Set
`ORACLE_BROWSER_PROFILE_DIR` to select another location.

## Commands

Review one exact PR head:

```console
export GH_REVIEW_TOKEN='...'
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
```

`review` emits one JSON object. `APPROVE` and `REQUEST_CHANGES` are successful
domain results; operational, schema, GitHub, and stale-state failures use a
non-zero exit status.

Submit the host agent's complete workspace patch:

```console
python3 skills/loopr/scripts/loopr.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

`submit` verifies repository identity, the expected local and remote head,
conflicts, whitespace, patch content, known credential values, and repeated
base/head snapshots. It stages the complete patch, creates one hook-free
unsigned commit, pushes with an explicit force-with-lease bound to the expected
head, and confirms the resulting GitHub PR head.

See `skills/loopr/references/command-contracts.md` for the complete JSON schemas,
exit classes, race behavior, and artifact contracts.

## Safety and artifacts

The host agent owns editing and local QA. `review` never edits or pushes, and
`submit` never plans changes, interprets findings, runs tests, or launches a
model process. CI status is not an approval gate.

Each command writes bounded, permission-restricted audit artifacts under
`.pr-loopr/runs/`. Known credential values are rejected or redacted, review
writes are anchored to the reviewed commit, and remote updates use explicit
head binding and lease protection.

## Codebase reduction

The legacy migration removed the 4,679-line root `loopr.py` orchestrator, so
root production code decreased from 4,679 lines to 0. The only production
Python now lives in `skills/loopr/scripts/`.

`github.py` and `submit_core.py` remain the largest modules because they contain
the shared GitHub snapshot/race checks and deterministic commit/push state
machine respectively. They are focused command infrastructure rather than an
agent orchestrator or containment framework.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check skills/loopr
uv run ruff format --check skills/loopr
uv run pyright
```
