#!/bin/bash
# Start agentic-perf services in the background.
#
# Unlike start.sh (which runs the orchestrator in the foreground),
# this script starts both the state store and orchestrator as
# background processes with proper signal isolation.
#
# Usage:
#   ./scripts/start-bg.sh          # start both services
#   ./scripts/start-bg.sh stop     # stop both services
#   ./scripts/start-bg.sh status   # check if services are running
#
# Logs:
#   State store:  ~/.agentic-perf/logs/state-store.log
#   Orchestrator: ~/.agentic-perf/logs/orchestrator.log
#
# Why nohup? Background processes started from scripts or AI agent
# tool calls inherit the parent's stdout/stderr. When the parent
# exits, writes to those file descriptors fail with SIGPIPE. Even
# though the orchestrator ignores SIGPIPE (fixed in #106), the
# process can still die if the parent's process group is killed.
# nohup + explicit log redirection prevents this entirely.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

AP_HOME="${AGENTIC_PERF_HOME:-$HOME/.agentic-perf}"
export AGENTIC_PERF_HOME="$AP_HOME"
CONFIG="$AP_HOME/config.json"
LOG_DIR="$AP_HOME/logs"
STORE_PID_FILE="$LOG_DIR/state-store.pid"
STORE_LOG="$LOG_DIR/state-store.log"
ORCH_LOG="$LOG_DIR/orchestrator.log"

mkdir -p "$LOG_DIR"

# Read port from config (default 8090)
_read_port() {
    python3 -c "
import json
try:
    cfg = json.load(open('$CONFIG'))
    print(cfg.get('state_store', {}).get('port', 8090))
except Exception:
    print(8090)
" 2>/dev/null
}

STORE_PORT=$(_read_port)

_is_store_running() {
    if [ -f "$STORE_PID_FILE" ]; then
        local pid
        pid=$(cat "$STORE_PID_FILE")
        kill -0 "$pid" 2>/dev/null && return 0
    fi
    return 1
}

_is_orch_running() {
    local pid_file="$AP_HOME/orchestrator.pid"
    if [ -f "$pid_file" ]; then
        # The orchestrator uses fcntl.flock — if we can't lock
        # the file, the orchestrator is running.
        python3 -c "
import fcntl, sys
try:
    fd = open('$pid_file')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(fd, fcntl.LOCK_UN)
    sys.exit(1)  # got the lock = not running
except (BlockingIOError, OSError):
    sys.exit(0)  # can't lock = running
" 2>/dev/null && return 0
    fi
    return 1
}

cmd_start() {
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: Config file not found: $CONFIG"
        echo "Create it with LLM backend, project_id, etc."
        exit 1
    fi

    # Start state store
    if _is_store_running; then
        echo "State store already running (PID $(cat "$STORE_PID_FILE"))."
    else
        echo "Starting state store on port $STORE_PORT..."
        STORE_PORT="$STORE_PORT" nohup python3 -m uvicorn state_store.main:app \
            --host 0.0.0.0 --port "$STORE_PORT" \
            --log-level warning \
            > "$STORE_LOG" 2>&1 &
        echo $! > "$STORE_PID_FILE"

        # Wait for ready
        for i in $(seq 1 20); do
            if curl -s "http://localhost:$STORE_PORT/api/v1/health" >/dev/null 2>&1; then
                echo "State store ready (PID $!)."
                echo "  Dashboard: http://localhost:$STORE_PORT/"
                echo "  Log: $STORE_LOG"
                break
            fi
            if [ "$i" -eq 20 ]; then
                echo "ERROR: State store failed to start. Check $STORE_LOG"
                exit 1
            fi
            sleep 0.5
        done
    fi

    # Start orchestrator
    if _is_orch_running; then
        echo "Orchestrator already running."
    else
        echo "Starting orchestrator..."
        nohup python3 -m orchestrator.main \
            > "$ORCH_LOG" 2>&1 &

        # Wait for it to acquire the lock
        sleep 3
        if _is_orch_running; then
            echo "Orchestrator ready."
            echo "  Log: $ORCH_LOG"
        else
            echo "ERROR: Orchestrator failed to start. Check $ORCH_LOG"
            exit 1
        fi
    fi

    echo ""
    echo "Services running. Submit tickets with:"
    echo "  python3 cli.py submit \"<summary>\" -d \"<description>\""
    echo ""
    echo "Stop with:"
    echo "  $0 stop"
}

cmd_stop() {
    echo "Stopping services..."

    # Stop orchestrator first
    local orch_pid_file="$AP_HOME/orchestrator.pid"
    if [ -f "$orch_pid_file" ]; then
        local pid
        pid=$(cat "$orch_pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            # Wait up to 5s for graceful shutdown
            for i in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$pid" 2>/dev/null || true
            echo "Orchestrator stopped."
        fi
    fi

    # Stop state store
    if [ -f "$STORE_PID_FILE" ]; then
        local pid
        pid=$(cat "$STORE_PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            for i in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$pid" 2>/dev/null || true
            echo "State store stopped."
        fi
        rm -f "$STORE_PID_FILE"
    fi

    echo "Done."
}

cmd_status() {
    echo "=== agentic-perf services ==="
    if _is_store_running; then
        echo "State store:  RUNNING (PID $(cat "$STORE_PID_FILE"))"
        echo "  Dashboard:  http://localhost:$STORE_PORT/"
    else
        echo "State store:  STOPPED"
    fi

    if _is_orch_running; then
        echo "Orchestrator: RUNNING"
    else
        echo "Orchestrator: STOPPED"
    fi
}

case "${1:-start}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
