#!/usr/bin/env bash
# Run full validation: lint + tests.
# Full serial local validation. The pre-commit hook runs scoped checks, and
# CI runs scripts/lint.sh plus scripts/test-parallel.sh.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${repo_root}/scripts/lint.sh"

# Guard: mcp_server.py files must never reappear in agents/ (issue #517)
if find "${repo_root}/agents" -name "mcp_server.py" | grep -q .; then
    echo "ERROR: mcp_server.py files found in agents/ — use server.py instead"
    find "${repo_root}/agents" -name "mcp_server.py"
    exit 1
fi

echo ""
"${repo_root}/scripts/test.sh" -q
