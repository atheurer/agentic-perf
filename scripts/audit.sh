#!/usr/bin/env bash
# Run the complete suite of audit policy and inventory checks.
# Ensures 100% audit logging of actions and decisions across tools and primitives.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Running agentic-perf Audit & Trace Verification Suite ==="
pytest -q \
    "${repo_root}/tests/test_tool_audit_policy.py" \
    "${repo_root}/tests/test_filesystem_inventory.py" \
    "${repo_root}/tests/test_subprocess_inventory.py" \
    "${repo_root}/tests/test_httpx_inventory.py" \
    "${repo_root}/tests/test_ssh_audit.py" \
    "${repo_root}/tests/test_store_audit.py" \
    "${repo_root}/tests/test_audited_filesystem.py" \
    "${repo_root}/tests/test_audited_subprocess.py" \
    "${repo_root}/tests/test_trace_operations.py" \
    "${repo_root}/tests/test_trace_operation_api.py" \
    "${repo_root}/tests/test_trace_state_mutations.py" \
    "${repo_root}/tests/test_state_store_trace_context.py" \
    "$@"

echo "✓ All audit policy and inventory checks passed!"
