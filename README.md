# loopr

The current root `loopr.py` executable runs a synchronous
Oracle-ChatGPT-Codex review-and-fix loop for one GitHub pull request:

1. Snapshot the exact PR head.
2. Ask ChatGPT through Oracle for an independent review.
3. Post the review with a dedicated GitHub reviewer account.
4. Let Codex fix validated blockers in a disposable worktree.
5. Validate, commit, and lease-protect the push before reviewing again.

The legacy orchestrator owns GitHub access and Git operations. Codex receives
neither GitHub credentials nor push authority.

## Agent skill transition

`skills/loopr/` is the canonical location for the vendor-neutral agent skill.
Compatible hosts discover the same directory through:

- `.agents/skills/loopr` for Codex CLI, Cursor CLI, and compatible clients;
- `.claude/skills/loopr` for Claude Code.

The host agent plans, edits, and runs repository validation. Oracle/ChatGPT
independently reviews the exact pull-request head and returns a structured
verdict. Deterministic skill scripts inspect the pull request, transport
reviews, validate the complete workspace patch, create one commit, and push the
PR branch with an explicit lease. GitHub and Git remain the sources of
pull-request identity, commit state, reviews, and remote branch updates.

The vendor-neutral commands are:

```console
python3 skills/loopr/scripts/loopr.py review --pr <NUMBER_OR_URL>
python3 skills/loopr/scripts/loopr.py submit \
  --pr <NUMBER_OR_URL> \
  --expected-head <SHA>
```

The root executable remains unchanged until issue #18 removes the legacy
orchestrator and wires the complete host-driven iteration around these command
contracts.

## Requirements

- Linux
- Python 3.10 or newer
- Node.js 24 or newer
- `git` and an authenticated, push-capable `origin`
- [GitHub CLI](https://cli.github.com/) authenticated for read access
- [Oracle](https://github.com/steipete/oracle) with Chrome or Chromium
- [Codex CLI](https://github.com/openai/codex) with `codex login` completed
- A dedicated reviewer account, different from the PR author, with repository
  administrator access
- A reviewer token in `GH_REVIEW_TOKEN` with pull-request review write
  permission

Only open, non-draft, same-repository GitHub.com pull requests are supported.
The local user must be able to push the PR head branch.

## Oracle login

Initialize Oracle's persistent browser profile and sign in to ChatGPT:

```console
oracle --engine browser --browser-manual-login --browser-keep-browser \
  --browser-input-timeout 120000 --prompt "Reply with ready"
```

The default profile is `~/.oracle/browser-profile`. Use
`ORACLE_BROWSER_PROFILE_DIR` to select another location.

## Usage

```console
export GH_REVIEW_TOKEN='...'
python3 loopr.py --pr 123
```

A canonical PR URL is also accepted:

```console
python3 loopr.py --pr https://github.com/OWNER/REPO/pull/123
```

Validate the environment without invoking models or changing GitHub:

```console
python3 loopr.py --pr 123 --dry-run
```

### Options

- `--repo-dir DIR`: local checkout; defaults to the current directory.
- `--max-iterations N`: maximum Oracle reviews; defaults to `5`.
- `--oracle-thinking-time LEVEL`: `light`, `standard`, `extended`, or `heavy`;
  defaults to `heavy`.
- `--artifacts-dir DIR`: audit directory; defaults to `.pr-loopr`.
- `--dry-run`: run preflight validation only.

## Safety model

`loopr` is intended for trusted internal repositories. Pull-request content and
model output are treated as untrusted data, but Codex's workspace sandbox does
not prevent reads outside the worktree. Run it only where the PR may safely read
files available to the operator account.

The loop fails closed on malformed reviews, credential collisions, unsafe
patches, context limits, stale approvals, concurrent head changes, or failed
pushes. It does not automatically merge, support forks, remediate CI, resolve
human review threads, or run as a daemon.

CI status is not an approval gate; failing or missing checks do not prevent
approval.

Each run writes permission-restricted audit artifacts under `.pr-loopr/runs/`,
including the PR snapshot, review inputs and outputs, state transitions,
resulting patch, and pushed commit SHA.

## Tests

```console
python3 -m unittest discover -s tests -v
```
