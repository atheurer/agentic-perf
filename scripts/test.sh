#!/usr/bin/env bash
# Run tests with coverage.
# This is the source of truth — CI and git hooks call this script.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# CI sets COVERAGE_DIR to produce artifacts; locally it's just terminal output.
COVERAGE_DIR="${COVERAGE_DIR:-}"

cov_reports="--cov-report=term-missing"
if [[ -n "$COVERAGE_DIR" ]]; then
    mkdir -p "$COVERAGE_DIR"
    cov_reports+=" --cov-report=html:${COVERAGE_DIR}"
    cov_reports+=" --cov-report=json:${COVERAGE_DIR}/coverage.json"
fi

echo "=== Running tests with coverage ==="
python3 -m pytest tests/ -v --tb=short \
    --cov=agents --cov=orchestrator --cov=providers --cov=state_store \
    $cov_reports \
    "$@"
