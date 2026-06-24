#!/usr/bin/env bash
# Run linting and format checks.
# This is the source of truth — CI and git hooks call this script.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "=== Ruff lint check ==="
ruff check .

echo "=== Ruff format check ==="
ruff format --check .

echo "✓ All lint checks passed"
