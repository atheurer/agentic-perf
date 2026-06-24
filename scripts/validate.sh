#!/usr/bin/env bash
# Run full validation: lint + tests.
# Called by git hooks and CI. Also useful for manual pre-commit checks.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${repo_root}/scripts/lint.sh"
echo ""
"${repo_root}/scripts/test.sh" -q
