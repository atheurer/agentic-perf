#!/bin/bash
# Start agentic-perf: state store + orchestrator
# Config is read from ~/.agentic-perf/config.json
# Env vars override config file values.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

AP_HOME="${AGENTIC_PERF_HOME:-$HOME/.agentic-perf}"
CONFIG="$AP_HOME/config.json"
if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config file not found: $CONFIG"
    echo "Create it with LLM backend, project_id, crucible_home, etc."
    exit 1
fi

# Read port from config (default 8090)
STORE_PORT=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG'))
    print(cfg.get('state_store', {}).get('port', 8090))
except Exception:
    print(8090)
")

STORE_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$STORE_PID" ] && kill -0 "$STORE_PID" 2>/dev/null; then
        kill "$STORE_PID" 2>/dev/null
        wait "$STORE_PID" 2>/dev/null
        echo "State store stopped."
    fi
    echo "Done."
}
trap cleanup EXIT INT TERM

# Start state store in background
echo "Starting state store on port $STORE_PORT..."
STORE_PORT="$STORE_PORT" python3 -m uvicorn state_store.main:app --host 0.0.0.0 --port "$STORE_PORT" --log-level warning &
STORE_PID=$!

# Wait for it to be ready
for i in $(seq 1 10); do
    if curl -s "http://localhost:$STORE_PORT/api/v1/health" >/dev/null 2>&1; then
        echo "State store ready (PID $STORE_PID)."
        echo "  Dashboard: http://localhost:$STORE_PORT/"
        break
    fi
    if [ "$i" -eq 10 ]; then
        echo "ERROR: State store failed to start."
        exit 1
    fi
    sleep 0.5
done

# Start orchestrator in foreground
echo "Starting orchestrator..."
echo "  Config: $CONFIG"
echo "  Ctrl+C to stop both."
echo ""
python3 -m orchestrator.main
