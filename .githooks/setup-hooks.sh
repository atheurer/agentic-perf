#!/usr/bin/env bash
# Configure git to use the shared hooks directory.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

git -C "${repo_root}" config core.hooksPath .githooks
echo "  ✓ Git hooks installed (.githooks/)"
