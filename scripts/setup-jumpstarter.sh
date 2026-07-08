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
pip install -e "${PROJECT_DIR}[dev,vertex,jumpstarter,telemetry]" --quiet 2>&1 | tail -3

# 2. Discover and install ALL jumpstarter-driver packages from PyPI.
#    Lab hardware changes over time — new exporters may reference
#    drivers we don't have. The composite driver's j CLI crashes
#    on StubDriverClient.cli() for any missing child driver, so
#    we install everything available to prevent runtime failures.
echo "Discovering all jumpstarter-driver packages from PyPI..."
DRIVERS=$(curl -s https://pypi.org/simple/ \
    | grep -o 'jumpstarter-driver-[a-z0-9-]*' \
    | sort -u)
DRIVER_COUNT=$(echo "$DRIVERS" | wc -l)
echo "  Found $DRIVER_COUNT driver packages on PyPI"

# Install all discovered drivers. pip handles already-installed
# packages gracefully (no-op). Failures on individual packages
# are logged but don't abort the script — some drivers may have
# platform-specific deps that can't be satisfied everywhere.
INSTALLED=0
FAILED=0
FAILED_LIST=""
for driver in $DRIVERS; do
    if pip install "$driver" --quiet 2>/dev/null; then
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
