#!/bin/bash
# Run automated tests and linting before pushing commits to github
#
# Usage:
#   ./test.sh             # run all tests gpu included by defualt
#   ./test.sh --no-gpu  # run no gpu tests

set -e
cd "$(dirname "$0")"

echo "==================================="
echo "Starting Test + Lint + Format Checks"
echo "==================================="
echo ""
if [[ " $* " == *" --no-gpu "* ]]; then
    # Setup
    uv sync --group dev --extra web 

    # Tests
    uv run pytest -m "not gpu"         # skip GPU tests (CI default)

    # Lint & format
    uv run ruff check                  # lint
    uv run ruff format --check         # format check
else
  # Setup
    uv sync --group dev --extra web --extra cuda

    # Tests
    uv run pytest                      # all tests
    uv run pytest -m "not gpu"         # skip GPU tests (CI default)

    # Lint & format
    uv run ruff check                  # lint
    uv run ruff format --check         # format check
fi

echo ""
echo "Testing Complete"
