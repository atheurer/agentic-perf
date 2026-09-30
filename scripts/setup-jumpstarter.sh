#!/usr/bin/env bash
# Setup script for Jumpstarter-enabled environments.
#
# Installs all required dependencies and configures the
# environment for embedded board provisioning via Jumpstarter.
#
# All Jumpstarter drivers are declared in pyproject.toml under
# the [jumpstarter] extra (jumpstarter-all meta-package).
#
# Usage:
#   ./scripts/setup-jumpstarter.sh
#
# Prerequisites:
#   - Jumpstarter client config at ~/.config/jumpstarter/clients/<name>.yaml
#     (created via: jmp login + jmp config client create <name>)

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

# 1. Install project with jumpstarter extras
echo "Installing project + jumpstarter dependencies..."
pip install -e "${PROJECT_DIR}[dev,vertex,jumpstarter]" --quiet 2>&1 | tail -3

# Telemetry is best-effort — must not block Jumpstarter install.
pip install -e "${PROJECT_DIR}[telemetry]" --quiet 2>&1 | tail -3 || \
    echo "  WARNING: telemetry extras failed (non-fatal)"

# 2. Verify critical driver imports
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
    'jumpstarter_driver_gpiod',
    'jumpstarter_driver_snmp',
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

# 3. Verify CLIs are available
echo "Checking CLI availability..."
if command -v jmp &>/dev/null; then
    echo "  jmp: $(jmp version 2>/dev/null | head -1)"
else
    echo "  WARNING: jmp CLI not on PATH"
fi
if command -v j &>/dev/null; then
    echo "  j: available"
else
    echo "  WARNING: j CLI not on PATH"
fi

# 4. Check Jumpstarter client config
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

echo ""
echo "=== Setup complete ==="
