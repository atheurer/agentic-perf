#!/usr/bin/env bash
# Setup script for Jumpstarter-enabled environments.
#
# Installs all required dependencies and configures the
# environment for embedded board provisioning via Jumpstarter.
#
# Key feature: discovers and installs ALL available
# jumpstarter-driver-* packages from PyPI so we never hit
# missing driver errors when the lab adds new hardware.
#
# Usage:
#   ./scripts/setup-jumpstarter.sh
#
# Prerequisites:
#   - Jumpstarter client config at ~/.config/jumpstarter/clients/<name>.yaml
#     (created via: jmp login + jmp config client create <name>)
#   - GCP credentials for Vertex AI (gcloud auth application-default login)
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Setting up Jumpstarter environment ==="

# 0. System packages required by boot-time harness
echo "Checking system dependencies..."
for cmd in sshpass ssh-keygen curl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "  Installing $cmd..."
        if command -v dnf &>/dev/null; then
            dnf install -y "$cmd" --quiet 2>&1 | tail -1
        elif command -v apt-get &>/dev/null; then
            apt-get install -y "$cmd" 2>&1 | tail -1
        else
            echo "  WARNING: Cannot install $cmd — no package manager found"
        fi
    fi
done
echo "  System dependencies OK"

# 1. Install project with all optional deps
echo "Installing project dependencies..."
pip install -e "${PROJECT_DIR}[dev,vertex,telemetry]" --quiet 2>&1 | tail -3

# 2. Install Jumpstarter from GitHub latest release.
#    PyPI may lag behind — install from the latest GitHub
#    release (including pre-releases) to match exporter
#    versions in the lab.
echo "Installing Jumpstarter from latest GitHub release..."
# install.sh lives under python/ in the repo
JMP_INSTALL_URL="https://raw.githubusercontent.com/jumpstarter-dev/jumpstarter/BRANCH/python/install.sh"

# Determine the latest release tag (including pre-releases)
# Pin to a known-working commit on release-0.9.
# The branch tip has a MCP crash (jumpstarter-dev/jumpstarter#896).
# Update this when a fixed release is available.
JMP_COMMIT="cc2706f5fd"
JMP_BRANCH="release-0.9"

echo "  Target: $JMP_BRANCH @ $JMP_COMMIT"

if false; then
    : # placeholder for future PyPI fallback
else
    echo "  Installing from branch: $JMP_BRANCH"

    JMP_VENV="/root/.local/jumpstarter/venv"
    if [ -f "$JMP_VENV/bin/jmp" ]; then
        EXISTING_VER=$($JMP_VENV/bin/jmp version 2>/dev/null | grep -o 'v[0-9].*' | head -1 || echo "unknown")
        echo "  Existing installation: $EXISTING_VER"
    fi

    INSTALL_URL=$(echo "$JMP_INSTALL_URL" | sed "s|BRANCH|$JMP_BRANCH|")
    curl -sSL "$INSTALL_URL" | bash -s -- 2>&1 | tail -5

    # Symlink jmp to PATH if not already there
    if [ -f "$JMP_VENV/bin/jmp" ] && [ ! -f /usr/local/bin/jmp ]; then
        ln -sf "$JMP_VENV/bin/jmp" /usr/local/bin/jmp
        echo "  Symlinked jmp to /usr/local/bin/jmp"
    fi

    # Make jumpstarter packages importable by system Python
    SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])")
    PTH_FILE="$SITE_PACKAGES/jumpstarter.pth"
    if [ ! -f "$PTH_FILE" ]; then
        echo "$JMP_VENV/lib64/python3.13/site-packages" > "$PTH_FILE"
        echo "$JMP_VENV/lib/python3.13/site-packages" >> "$PTH_FILE"
        echo "  Created .pth file for system Python imports"
    fi

    echo "  Jumpstarter installed: $($JMP_VENV/bin/jmp version 2>/dev/null | head -1)"
fi

# The install.sh may install from a pinned snapshot.
# Upgrade core packages and install jumpstarter-mcp
# from the branch tip to ensure jmp mcp serve works.
echo "Upgrading packages from pinned commit..."
JMP_SRC="/tmp/jmp-src-$$"
git clone --branch "$JMP_BRANCH" \
    https://github.com/jumpstarter-dev/jumpstarter.git \
    "$JMP_SRC" 2>/dev/null
if [ -d "$JMP_SRC" ] && [ -n "$JMP_COMMIT" ]; then
    cd "$JMP_SRC" && git checkout "$JMP_COMMIT" 2>/dev/null
    cd - > /dev/null
fi
if [ -d "$JMP_SRC/python/packages" ]; then
    for pkg_dir in "$JMP_SRC"/python/packages/jumpstarter*; do
        if [ -d "$pkg_dir" ]; then
            "$JMP_VENV/bin/pip" install \
                "$pkg_dir/" \
                --no-deps --quiet 2>/dev/null
        fi
    done
    echo "  All packages upgraded from $JMP_BRANCH"
    rm -rf "$JMP_SRC"
fi

# 3. Discover and install ALL jumpstarter-driver packages.
#    Lab hardware changes over time — new exporters may reference
#    drivers we don't have. Install from the same venv's pip
#    to keep versions consistent.
echo "Discovering jumpstarter-driver packages..."

# Use the jumpstarter venv's pip if available, else system pip
if [ -f "$JMP_VENV/bin/pip" ] 2>/dev/null; then
    JMP_PIP="$JMP_VENV/bin/pip"
else
    JMP_PIP="pip"
fi

DRIVERS=$($JMP_PIP list 2>/dev/null | grep 'jumpstarter-driver' | awk '{print $1}' | sort -u)
if [ -z "$DRIVERS" ]; then
    # Discover from PyPI as fallback
    DRIVERS=$(curl -s https://pypi.org/simple/ \
        | grep -o 'jumpstarter-driver-[a-z0-9-]*' \
        | sort -u)
fi
DRIVER_COUNT=$(echo "$DRIVERS" | wc -l)
echo "  Found $DRIVER_COUNT driver packages"

INSTALLED=0
FAILED=0
FAILED_LIST=""
for driver in $DRIVERS; do
    if $JMP_PIP install "$driver" --quiet 2>/dev/null; then
        INSTALLED=$((INSTALLED + 1))
    else
        FAILED=$((FAILED + 1))
        FAILED_LIST="$FAILED_LIST $driver"
    fi
done
echo "  Installed: $INSTALLED, Failed: $FAILED"
if [ "$FAILED" -gt 0 ]; then
    echo "  Failed packages:$FAILED_LIST"
    echo "  (Non-critical — these may have platform-specific deps)"
fi

# 3. Verify critical driver imports
echo "Verifying critical Jumpstarter driver imports..."
python3 -c "
import importlib
# These are the drivers used by R-Car S4, SA8775P, and S32G boards
critical = [
    'jumpstarter_driver_flashers',
    'jumpstarter_driver_power',
    'jumpstarter_driver_pyserial',
    'jumpstarter_driver_ssh',
    'jumpstarter_driver_network',
    'jumpstarter_driver_composite',
    'jumpstarter_driver_tmt',
    'jumpstarter_driver_vnc',
]
failed = []
for d in critical:
    try:
        importlib.import_module(d)
    except ImportError:
        failed.append(d)
if failed:
    print(f'CRITICAL MISSING: {failed}')
    exit(1)

# Count all installed drivers
import pkgutil
all_drivers = [
    m.name for m in pkgutil.iter_modules()
    if m.name.startswith('jumpstarter_driver_')
]
print(f'All {len(all_drivers)} installed drivers OK '
      f'(critical: {len(critical)}/{len(critical)})')
"

# 4. Verify Jumpstarter client config
echo "Checking Jumpstarter client config..."
if jmp config client list 2>/dev/null | grep -q .; then
    echo "  Client configs found:"
    jmp config client list 2>&1 | head -5
else
    echo "  WARNING: No Jumpstarter client configs found."
    echo "  Run: jmp login --endpoint <ENDPOINT> --token <TOKEN>"
    echo "  Then: jmp config client create <NAME> --namespace <NS>"
fi

# 5. Create config directory structure
echo "Setting up config directories..."
mkdir -p ~/.agentic-perf/secrets/jumpstarter
mkdir -p ~/.agentic-perf/logs
mkdir -p ~/.agentic-perf/skill-cache

# 6. Check for required config
if [ ! -f ~/.agentic-perf/config.json ]; then
    echo "  WARNING: ~/.agentic-perf/config.json not found."
    echo "  Create it with at minimum:"
    echo '  {"llm": {"provider": "claude", "model": "claude-sonnet-4-6"}}'
fi

if [ ! -f ~/.agentic-perf/secrets/jumpstarter/config.json ]; then
    echo "  WARNING: ~/.agentic-perf/secrets/jumpstarter/config.json not found."
    echo "  Create it with: {\"client_name\": \"<your-client-name>\"}"
fi

# 7. Check Vertex AI env vars
echo "Checking Vertex AI environment..."
if [ -n "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ] || [ -n "${CLAUDE_CODE_USE_VERTEX:-}" ]; then
    echo "  Vertex AI configured (project=${ANTHROPIC_VERTEX_PROJECT_ID:-unset})"
else
    echo "  WARNING: Vertex AI env vars not set."
    echo "  Export: CLAUDE_CODE_USE_VERTEX=1"
    echo "  Export: CLOUD_ML_REGION=global"
    echo "  Export: ANTHROPIC_VERTEX_PROJECT_ID=<project-id>"
fi

# 8. Update skill cache with boot-time scripts if repo available
BOOT_TIME_REPO="/git/gitlab/perfscale/boot-time-analysis-scripts"
if [ -d "$BOOT_TIME_REPO" ]; then
    echo "Updating boot-time skill cache..."
    cp -r "$BOOT_TIME_REPO" ~/.agentic-perf/skill-cache/boot-time-analysis-scripts
    echo "  Updated from $BOOT_TIME_REPO"
fi

echo ""
echo "=== Setup complete ==="
echo "Start services with: ./scripts/start-bg.sh"
