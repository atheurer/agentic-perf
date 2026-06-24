#!/usr/bin/env bash
# Install git hooks and verify development environment.
# Run this once after cloning the repository.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== agentic-perf Developer Setup ==="
echo ""

# Check required tools
echo "Step 1: Checking required tools..."
missing_tools=()

if ! command -v python3 &>/dev/null; then
    missing_tools+=("python3")
fi

if ! command -v git &>/dev/null; then
    missing_tools+=("git")
fi

if [ ${#missing_tools[@]} -gt 0 ]; then
    echo "ERROR: Missing required tools: ${missing_tools[*]}"
    echo ""
    echo "Required:"
    echo "  - Python 3.12+"
    echo "  - Git"
    exit 1
fi

# Check Python version
python_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python: ${python_version}"

if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
    echo "  ✓ Python version OK"
else
    echo "  ✗ Python 3.12+ required (found ${python_version})"
    exit 1
fi

echo ""
echo "Step 2: Installing git hooks..."
"${repo_root}/.githooks/setup-hooks.sh"

echo ""
echo "Step 3: Installing dev dependencies..."
pip install -e ".[dev]" --quiet

echo ""
echo "✓ Developer setup complete!"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/validate.sh to verify everything works"
echo "  2. Read AGENTS.md for AI-assisted development guidelines"
echo "  3. Read CONTRIBUTING.md for contribution workflow"
