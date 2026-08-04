#!/usr/bin/env bash

set -euox pipefail
cd "$(git rev-parse --show-toplevel)"

COOLDOWN_DAYS=7
export UV_EXCLUDE_NEWER="${COOLDOWN_DAYS} days"
export NPM_CONFIG_MIN_RELEASE_AGE="${COOLDOWN_DAYS}"

# Python
uv sync
uv run ruff format .
uv run ruff check --fix .
uv run pyright .
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q review_loop.py tests

# Markdown
npx -y prettier --write './**/*.md'

# GitHub Actions
zizmor --fix=safe .github/workflows
git ls-files -z -- '.github/workflows/*.yml' | xargs -0 -t actionlint
git ls-files -z -- '.github/workflows/*.yml' | xargs -0 -t yamllint -d '{"extends": "relaxed", "rules": {"line-length": "disable"}}'
checkov --framework=all --output=github_failed_only --directory=.
