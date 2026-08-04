#!/usr/bin/env bash

set -euox pipefail
cd "$(git rev-parse --show-toplevel)"

# Python
uv run ruff format .
uv run ruff check --fix .
uv run pyright .
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q review_loop.py tests
