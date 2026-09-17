#!/usr/bin/env bash
# Start, stop, and inspect the agentic-perf background services.
#
# The state-store persistence lock and orchestrator lock are authoritative.
# PID files are repairable metadata and are never sufficient on their own to
# identify a process that may be signalled.
#
# Usage:
#   ./scripts/start-bg.sh          # start both services
#   ./scripts/start-bg.sh stop     # stop both services
#   ./scripts/start-bg.sh status   # inspect ownership and readiness
#
# START_BG_STORE_TIMEOUT, START_BG_ORCH_TIMEOUT, and START_BG_STOP_TIMEOUT
# can override the readiness and shutdown timeouts for diagnostics.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

AP_HOME="${AGENTIC_PERF_HOME:-$HOME/.agentic-perf}"
export AGENTIC_PERF_HOME="$AP_HOME"
CONFIG="$AP_HOME/config.json"
LOG_DIR="$AP_HOME/logs"
STORE_LOCK="$AP_HOME/state-store.lock"
STORE_PID_FILE="$LOG_DIR/state-store.pid"
STORE_LOG="$LOG_DIR/state-store.log"
ORCH_LOCK="$AP_HOME/orchestrator.pid"
ORCH_LOG="$LOG_DIR/orchestrator.log"
LAUNCH_LOCK="$AP_HOME/state-store-launch.lock"
STORE_START_TIMEOUT="${START_BG_STORE_TIMEOUT:-20}"
ORCH_START_TIMEOUT="${START_BG_ORCH_TIMEOUT:-45}"
STOP_TIMEOUT="${START_BG_STOP_TIMEOUT:-10}"

mkdir -p "$LOG_DIR"

revision() {
    local sha dirty
    sha="$(git -C "$REPO_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
    dirty="$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)"
    if [ -n "$dirty" ]; then
        printf '%s (dirty)\n' "$sha"
    else
        printf '%s\n' "$sha"
    fi
}

error() { echo "ERROR: $*" >&2; }

read_port() {
    python3 - "$CONFIG" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get("state_store", {}).get("port", 8090))
except (OSError, json.JSONDecodeError, TypeError):
    print(8090)
PY
}

STORE_PORT="${STORE_PORT:-$(read_port)}"

process_cmdline() {
    tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | sed 's/[[:space:]]*$//' || true
}

process_start_identity() {
    python3 - "$1" <<'PY'
from pathlib import Path
import sys

try:
    fields = Path(f"/proc/{sys.argv[1]}/stat").read_text().rsplit(") ", 1)[1].split()
    print(f"{sys.argv[1]}:{fields[19]}")
except (IndexError, OSError):
    print("")
PY
}

process_alive() {
    local pid="$1" state
    kill -0 "$pid" 2>/dev/null || return 1
    state="$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || true)"
    [ "$state" != Z ] && [ "$state" != X ]
}

process_matches() {
    local pid="$1" kind="$2" cmd
    process_alive "$pid" || return 1
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" = "$REPO_DIR" ] || return 1
    cmd="$(process_cmdline "$pid")"
    case "$kind" in
        store) [[ "$cmd" == *"-m uvicorn state_store.main:app"* ]] ;;
        orchestrator) [[ "$cmd" == *"-m orchestrator.main"* ]] ;;
        *) return 1 ;;
    esac
}

lock_is_held() {
    local path="$1"
    [ -e "$path" ] || return 1
    python3 - "$path" <<'PY'
import fcntl
import os
import sys

try:
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
except BlockingIOError:
    raise SystemExit(0)
raise SystemExit(1)
PY
}

json_field() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get(sys.argv[2], "")
    print(value if value is not None else "")
except (OSError, json.JSONDecodeError, TypeError):
    print("")
PY
}

store_lock_pid() { json_field "$STORE_LOCK" pid; }
store_lock_start_identity() { json_field "$STORE_LOCK" process_start_identity; }

store_owner_valid() {
    local pid="$1" expected="${2:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    process_matches "$pid" store || return 1
    [ -z "$expected" ] || [ "$(process_start_identity "$pid")" = "$expected" ]
}

orchestrator_pid() { tr -d '[:space:]' < "$ORCH_LOCK"; }

orchestrator_owner_valid() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    process_matches "$pid" orchestrator
}

write_pid_file() {
    local path="$1" pid="$2" temporary="${1}.tmp.$$"
    printf '%s\n' "$pid" > "$temporary"
    mv -f "$temporary" "$path"
}

read_api_token() {
    local token="${AGENTIC_PERF_API_TOKEN:-}"
    [ -f "$AP_HOME/secrets/api-token" ] && token="$(tr -d '\n' < "$AP_HOME/secrets/api-token")"
    printf '%s' "$token"
}

endpoint_identity() {
    local response token local_id remote_id
    token="$(read_api_token)"
    response="$(curl -fsS --max-time 1 -H "Authorization: Bearer $token" \
        "http://localhost:$STORE_PORT/api/v1/diagnostics" 2>/dev/null)" || return 1
    local_id="$(tr -d '\n' < "$AP_HOME/state-store.id" 2>/dev/null || true)"
    remote_id="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("store_id", ""))' "$response" 2>/dev/null || true)"
    if [ -n "$remote_id" ] && [ "$remote_id" = "$local_id" ]; then
        printf '%s' this-store
    elif [ -n "$remote_id" ]; then
        printf '%s' different-store
    fi
}

wait_for_store() {
    local pid="$1" deadline=$((SECONDS + STORE_START_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if store_owner_valid "$pid" "$(store_lock_start_identity 2>/dev/null || true)" \
            && [ "$(endpoint_identity || true)" = "this-store" ] \
            && [ "$(store_lock_pid || true)" = "$pid" ]; then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

wait_for_orchestrator() {
    local pid="$1" deadline=$((SECONDS + ORCH_START_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if orchestrator_owner_valid "$pid" && lock_is_held "$ORCH_LOCK"; then
            return 0
        fi
        process_alive "$pid" || return 1
        sleep 0.2
    done
    return 1
}

terminate_process() {
    local pid="$1" kind="$2" deadline lock_path=""
    [ "$kind" = store ] && lock_path="$STORE_LOCK"
    [ "$kind" = orchestrator ] && lock_path="$ORCH_LOCK"
    kill "$pid" 2>/dev/null || true
    deadline=$((SECONDS + STOP_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ] && process_alive "$pid"; do sleep 0.2; done
    process_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
    sleep 0.2
    [ -z "$lock_path" ] || ! lock_is_held "$lock_path"
}

stop_orchestrator() {
    local pid=""
    if lock_is_held "$ORCH_LOCK"; then
        pid="$(orchestrator_pid || true)"
        orchestrator_owner_valid "$pid" || {
            error "orchestrator lock is held by an unverifiable process (PID ${pid:-unknown}); refusing to signal it"
            return 1
        }
        echo "Stopping orchestrator (PID $pid)..."
        terminate_process "$pid" orchestrator || { error "orchestrator did not release its lock"; return 1; }
    else
        [ -f "$ORCH_LOCK" ] && rm -f "$ORCH_LOCK"
    fi
}

stop_store() {
    local pid="" expected=""
    if lock_is_held "$STORE_LOCK"; then
        pid="$(store_lock_pid || true)"
        expected="$(store_lock_start_identity || true)"
        store_owner_valid "$pid" "$expected" || {
            error "state-store lock is held by an unverifiable process (PID ${pid:-unknown}); refusing to signal it"
            return 1
        }
        echo "Stopping state store (PID $pid)..."
        terminate_process "$pid" store || { error "state store did not release its persistence lock"; return 1; }
    fi
    rm -f "$STORE_PID_FILE"
}

start_store() {
    local pid="" expected=""
    if lock_is_held "$STORE_LOCK"; then
        pid="$(store_lock_pid || true)"
        expected="$(store_lock_start_identity || true)"
        store_owner_valid "$pid" "$expected" || {
            error "state-store lock is held by an unverifiable process (PID ${pid:-unknown})"
            return 1
        }
        write_pid_file "$STORE_PID_FILE" "$pid"
        wait_for_store "$pid" || { error "state-store lock owner PID $pid is not responding as this instance"; return 1; }
        echo "State store already running (PID $pid); did not start a new service."
        echo "  Repaired PID metadata: $STORE_PID_FILE"
        echo "  The existing process was left untouched; its loaded revision is not changed by this checkout."
        return 2
    fi
    case "$(endpoint_identity || true)" in
        different-store) error "port $STORE_PORT is occupied by a different state store"; return 1 ;;
        this-store) error "state-store endpoint is reachable without this instance's lock"; return 1 ;;
    esac
    echo "Starting state store on port $STORE_PORT..."
    STORE_PORT="$STORE_PORT" nohup python3 -m uvicorn state_store.main:app \
        --host 0.0.0.0 --port "$STORE_PORT" --log-level warning > "$STORE_LOG" 2>&1 &
    pid=$!
    write_pid_file "$STORE_PID_FILE" "$pid"
    if wait_for_store "$pid"; then
        echo "State store started (PID $pid)."
        return 0
    fi
    error "state store failed to become ready; see $STORE_LOG"
    store_owner_valid "$pid" "$(store_lock_start_identity 2>/dev/null || true)" && terminate_process "$pid" store || true
    rm -f "$STORE_PID_FILE"
    return 1
}

start_orchestrator() {
    local pid="" deadline=$((SECONDS + ORCH_START_TIMEOUT))
    if lock_is_held "$ORCH_LOCK"; then
        pid="$(orchestrator_pid || true)"
        orchestrator_owner_valid "$pid" || {
            error "orchestrator lock is held by an unverifiable process (PID ${pid:-unknown})"
            return 1
        }
        echo "Orchestrator already running (PID $pid); did not start a new service."
        echo "  The existing process was left untouched; its loaded revision is not changed by this checkout."
        return 2
    fi
    rm -f "$ORCH_LOCK"
    while [ "$SECONDS" -lt "$deadline" ]; do
        echo "Starting orchestrator..."
        nohup python3 -m orchestrator.main > "$ORCH_LOG" 2>&1 &
        pid=$!
        write_pid_file "$ORCH_LOCK" "$pid"
        if wait_for_orchestrator "$pid"; then
            echo "Orchestrator started (PID $pid)."
            return 0
        fi
        if grep -q "leader lease unavailable" "$ORCH_LOG" 2>/dev/null; then
            echo "Orchestrator is waiting for the previous leader lease to expire; retrying..."
            sleep 1
            continue
        fi
        error "orchestrator failed to become ready; see $ORCH_LOG"
        rm -f "$ORCH_LOCK"
        return 1
    done
    error "orchestrator did not become ready within ${ORCH_START_TIMEOUT}s; see $ORCH_LOG"
    rm -f "$ORCH_LOCK"
    return 1
}

cmd_start() {
    [ -f "$CONFIG" ] || { error "config file not found: $CONFIG"; return 1; }
    echo "Launcher checkout revision: $(revision)"
    exec {launch_fd}>"$LAUNCH_LOCK"
    flock -n "$launch_fd" || { error "another start-bg.sh invocation is starting or stopping $AP_HOME"; return 1; }
    local store_result=0 orch_result=0 store_started=0
    if start_store; then store_started=1; else store_result=$?; [ "$store_result" -eq 2 ] || { flock -u "$launch_fd" || true; return 1; }; fi
    if start_orchestrator; then orch_result=0; else orch_result=$?; if [ "$orch_result" -ne 2 ] && [ "$store_started" -eq 1 ]; then error "orchestrator startup failed; rolling back the state store started by this invocation"; stop_store || true; fi; flock -u "$launch_fd" || true; return 1; fi
    flock -u "$launch_fd" || true
    echo "Services running."
}

cmd_stop() {
    local failures=0
    echo "Launcher checkout revision: $(revision)"
    echo "Stopping services..."
    stop_orchestrator || failures=$((failures + 1))
    stop_store || failures=$((failures + 1))
    if [ "$failures" -ne 0 ]; then error "$failures service(s) could not be stopped safely"; return 1; fi
    echo "Services stopped."
}

cmd_status() {
    local store_pid="" orch_pid=""
    echo "Launcher checkout revision: $(revision)"
    if lock_is_held "$STORE_LOCK"; then
        store_pid="$(store_lock_pid || true)"
        if store_owner_valid "$store_pid" "$(store_lock_start_identity 2>/dev/null || true)"; then
            echo "State store:  RUNNING (PID $store_pid; endpoint $(endpoint_identity || echo unavailable))"
        else
            echo "State store:  LOCK HELD BY UNKNOWN OWNER (PID ${store_pid:-unknown})"
        fi
    elif [ -n "$(endpoint_identity || true)" ]; then
        echo "State store:  ENDPOINT OCCUPIED WITHOUT THIS STORE LOCK"
    else
        echo "State store:  STOPPED"
    fi
    if lock_is_held "$ORCH_LOCK"; then
        orch_pid="$(orchestrator_pid || true)"
        if orchestrator_owner_valid "$orch_pid"; then
            echo "Orchestrator: RUNNING (PID $orch_pid)"
        else
            echo "Orchestrator: LOCK HELD BY UNKNOWN OWNER (PID ${orch_pid:-unknown})"
        fi
    else
        echo "Orchestrator: STOPPED"
    fi
}

case "${1:-start}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    *) error "usage: $0 {start|stop|status}"; exit 2 ;;
esac
