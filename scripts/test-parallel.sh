#!/usr/bin/env bash
# Run the full test suite with isolated pytest-xdist workers and coverage.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

workers="${PYTEST_WORKERS:-4}"
exec "${repo_root}/scripts/test.sh" -n "$workers" --dist loadfile "$@"
