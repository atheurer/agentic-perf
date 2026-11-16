#!/usr/bin/env bash
# Start, stop, and inspect the agentic-perf background services.
#
# The state-store persistence lock and orchestrator lock are authoritative.
# PID files are repairable metadata and are never sufficient on their own to
# identify a process that may be signalled.
#
# Usage:
#   ./scripts/start-bg.sh          # start both services
#   ./scripts/start-bg.sh restart  # stop, then start both services
#   ./scripts/start-bg.sh stop     # stop both services
#   ./scripts/start-bg.sh status   # inspect ownership and readiness
#
# START_BG_STORE_TIMEOUT, START_BG_ORCH_TIMEOUT, START_BG_ORCH_LEASE_TIMEOUT,
# and START_BG_STOP_TIMEOUT override readiness and shutdown waits.
# START_BG_LOG_BACKUPS, START_BG_LOG_MAX_BYTES, and the START_BG_RESTART_*
# settings control log retention and supervisor backoff.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
ORCH_SUPERVISOR_LOCK="$AP_HOME/orchestrator-supervisor.lock"
ORCH_SUPERVISOR_PID_FILE="$LOG_DIR/orchestrator-supervisor.pid"
ORCH_SUPERVISOR_LOG="$LOG_DIR/orchestrator-supervisor.log"
ORCH_SUPERVISOR_STATUS_FILE="$AP_HOME/orchestrator-supervisor.status"
LAUNCH_LOCK="$AP_HOME/state-store-launch.lock"
STORE_STATUS_FILE="$AP_HOME/state-store.status"
ORCH_STATUS_FILE="$AP_HOME/orchestrator.status"
STORE_START_TIMEOUT="${START_BG_STORE_TIMEOUT:-20}"
ORCH_START_TIMEOUT="${START_BG_ORCH_TIMEOUT:-45}"
STOP_TIMEOUT="${START_BG_STOP_TIMEOUT:-10}"
INACTIVITY_TIMEOUT="${START_BG_INACTIVITY_TIMEOUT:-20}"
MAX_START_TIMEOUT="${START_BG_MAX_START_TIMEOUT:-180}"
LOG_BACKUPS="${START_BG_LOG_BACKUPS:-5}"
LOG_MAX_BYTES="${START_BG_LOG_MAX_BYTES:-1048576}"
RESTART_BACKOFF="${START_BG_RESTART_BACKOFF:-2}"
RESTART_BACKOFF_MAX="${START_BG_RESTART_BACKOFF_MAX:-60}"
RESTART_STABLE_SECONDS="${START_BG_RESTART_STABLE_SECONDS:-60}"
CRASH_LOOP_THRESHOLD="${START_BG_CRASH_LOOP_THRESHOLD:-3}"

mkdir -p "$LOG_DIR"

validate_launch_settings() {
    [[ "$LOG_MAX_BYTES" =~ ^[1-9][0-9]*$ ]] || {
        echo "ERROR: START_BG_LOG_MAX_BYTES must be a positive integer" >&2
        return 2
    }
    for setting in \
        "START_BG_LOG_BACKUPS=$LOG_BACKUPS" \
        "START_BG_LOG_MAX_BYTES=$LOG_MAX_BYTES" \
        "START_BG_RESTART_BACKOFF=$RESTART_BACKOFF" \
        "START_BG_RESTART_BACKOFF_MAX=$RESTART_BACKOFF_MAX" \
        "START_BG_RESTART_STABLE_SECONDS=$RESTART_STABLE_SECONDS" \
        "START_BG_CRASH_LOOP_THRESHOLD=$CRASH_LOOP_THRESHOLD" \
        "START_BG_ORCH_LEASE_TIMEOUT=$ORCH_LEASE_WAIT_TIMEOUT"; do
        local name="${setting%%=*}" value="${setting#*=}"
        [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
            echo "ERROR: ${setting%%=*} must be a positive integer" >&2
            return 2
        }
        local max_value
        case "$name" in
            START_BG_RESTART_BACKOFF | START_BG_RESTART_BACKOFF_MAX | START_BG_ORCH_LEASE_TIMEOUT)
                max_value=86400
                ;;
            START_BG_LOG_MAX_BYTES)
                max_value=1073741824
                ;;
            START_BG_LOG_BACKUPS)
                max_value=100
                ;;
            START_BG_RESTART_STABLE_SECONDS)
                max_value=31536000
                ;;
            START_BG_CRASH_LOOP_THRESHOLD)
                max_value=1000000
                ;;
        esac
        if [ "${#value}" -gt "${#max_value}" ] || [ "$value" -gt "$max_value" ]; then
            echo "ERROR: $name must be <= $max_value" >&2
            return 2
        fi
    done
}

rotate_log() {
    local path="$1" index
    [ -s "$path" ] || return 0
    for ((index = LOG_BACKUPS; index > 1; index--)); do
        if [ -f "$path.$((index - 1))" ]; then
            mv -f "$path.$((index - 1))" "$path.$index"
        fi
    done
    mv -f "$path" "$path.1"
}

rotate_log_if_needed() {
    local path="$1" size
    [ -f "$path" ] || return 0
    size="$(stat -c %s "$path" 2>/dev/null || echo 0)"
    [ "$size" -ge "$LOG_MAX_BYTES" ] || return 0
    rotate_log "$path"
}

append_supervisor_log() {
    rotate_log_if_needed "$ORCH_SUPERVISOR_LOG"
    printf '%s %s\n' "$(date -Is)" "$*" >>"$ORCH_SUPERVISOR_LOG"
}

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

dump_process_diagnostics() {
    local log_path="$1" service_name="$2" pid="$3"
    echo "================================================================================" >&2
    echo "DIAGNOSTICS: $service_name (PID ${pid:-unknown}) startup failure" >&2
    echo "Log file: $log_path" >&2
    if [ -f "$log_path" ] && [ -s "$log_path" ]; then
        echo "--- Last 25 lines of $log_path ---" >&2
        tail -n 25 "$log_path" >&2
        echo "--------------------------------------------------------------------------------" >&2
    else
        echo "Log file is empty or does not exist." >&2
    fi
    echo "================================================================================" >&2
}

read_status_info() {
    local status_file="$1" expected_pid="$2"
    [ -f "$status_file" ] || return 1
    python3 - "$status_file" "$expected_pid" <<'PY'
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    expected_pid = int(sys.argv[2])
    if int(data.get("pid", -1)) != expected_pid:
        sys.exit(1)
    phase = data.get("phase", "")
    detail = data.get("detail", "")
    updated_at = float(data.get("updated_at", 0.0))
    print(f"{phase}\t{detail}\t{updated_at}")
except Exception:
    sys.exit(1)
PY
}

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

read_orchestrator_lease_wait_timeout() {
    python3 - "$CONFIG" <<'PY'
import json
import math
import os
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.load(handle)
except (OSError, json.JSONDecodeError, TypeError):
    config = {}
lease = config.get("orchestrator_lease", {}) if isinstance(config, dict) else {}
if not isinstance(lease, dict):
    lease = {}
retry_interval = 5.0
request_timeout = 10.0
backoff_max = 30.0
try:
    ttl = float(os.environ.get("ORCHESTRATOR_LEASE_TTL_SECONDS", lease.get("ttl_seconds", 30.0)))
    recovery = float(
        os.environ.get(
            "ORCHESTRATOR_LEASE_RECOVERY_DELAY_SECONDS",
            lease.get("recovery_delay_seconds", 5.0),
        )
    )
    max_attempts = int(
        os.environ.get(
            "ORCHESTRATOR_LEASE_MAX_ATTEMPTS",
            lease.get("max_attempts", 12),
        )
    )
    if not math.isfinite(ttl) or ttl <= 0 or ttl > 3600:
        raise ValueError
    if not math.isfinite(recovery) or recovery < 0:
        raise ValueError
except (TypeError, ValueError, OverflowError):
    ttl, recovery = 30.0, 5.0
    max_attempts = 12
# More attempts cannot increase the externally capped wait, so clamp before
# converting the retry count to a float.
max_attempts = min(10_000, max(1, max_attempts))
retry_count = max_attempts - 1
if retry_count <= 3:
    retry_delays = retry_interval * (2**retry_count - 1)
else:
    retry_delays = 35.0 + 30.0 * (retry_count - 3)
retry_budget = max_attempts * request_timeout + retry_delays
wait = max(0.0, ttl) + max(0.0, recovery) + retry_budget
print(min(86_400, max(1, math.ceil(wait))))
PY
}

initialize_launch_settings() {
    ORCH_LEASE_WAIT_TIMEOUT="${START_BG_ORCH_LEASE_TIMEOUT:-$(read_orchestrator_lease_wait_timeout)}"
}

validate_application_config() {
    python3 <<'PY'
import sys

from orchestrator.config import ConfigFileError, OrchestratorConfig, _load_config_file
from state_store.config import validate_runtime_config

try:
    config = _load_config_file(strict=True)
    validate_runtime_config(config)
    # Constructing the read-only config object applies the same lease and
    # state-store parsing that the orchestrator uses at startup.
    OrchestratorConfig(raw_config=config)
except ConfigFileError as exc:
    print(f"ERROR: invalid application configuration: {exc}", file=sys.stderr)
    raise SystemExit(1)
except Exception:
    print("ERROR: invalid application configuration", file=sys.stderr)
    raise SystemExit(1)
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

clear_lock_metadata() {
    local path="$1"
    python3 - "$path" <<'PY'
import fcntl
import os
import sys

path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
except FileNotFoundError:
    raise SystemExit(0)
except OSError:
    raise SystemExit(2)
try:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(3)
    try:
        os.ftruncate(fd, 0)
        os.fsync(fd)
    except OSError:
        raise SystemExit(4)
finally:
    os.close(fd)
PY
}

wait_for_lock_release() {
    local path="$1" label="$2" deadline=$((SECONDS + STORE_START_TIMEOUT))
    echo "$label lock is held by an unverifiable process; waiting up to ${STORE_START_TIMEOUT}s for it to release..."
    while [ "$SECONDS" -lt "$deadline" ] && lock_is_held "$path"; do
        sleep 0.2
    done
    ! lock_is_held "$path"
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

supervisor_pid() {
    tr -d '[:space:]' < "$ORCH_SUPERVISOR_PID_FILE" 2>/dev/null || true
}

supervisor_lock_pid() {
    tr -d '[:space:]' < "$ORCH_SUPERVISOR_LOCK" 2>/dev/null || true
}

supervisor_owner_valid() {
    local pid="$1" cmd
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    process_alive "$pid" || return 1
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" = "$REPO_DIR" ] || return 1
    cmd="$(process_cmdline "$pid")"
    [[ "$cmd" == *"start-bg.sh __supervise-orchestrator"* ]]
}

active_supervisor_pid() {
    local pid=""
    lock_is_held "$ORCH_SUPERVISOR_LOCK" || return 1
    pid="$(supervisor_lock_pid || true)"
    supervisor_owner_valid "$pid" || return 1
    printf '%s\n' "$pid"
}

write_supervisor_status() {
    local phase="$1" detail="$2" failures="$3" next_restart="$4" temporary
    temporary="${ORCH_SUPERVISOR_STATUS_FILE}.tmp.$$"
    python3 - "$temporary" "$$" "$phase" "$detail" "$failures" \
        "$next_restart" <<'PY'
import json
import os
import sys
import time

path, pid, phase, detail, failures, next_restart = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "pid": int(pid),
            "phase": phase,
            "detail": detail,
            "consecutive_failures": int(failures),
            "next_restart_epoch": int(next_restart),
            "updated_at": time.time(),
        },
        handle,
        separators=(",", ":"),
    )
os.replace(path, path.removesuffix(f".tmp.{pid}"))
PY
}

read_supervisor_status() {
    python3 - "$ORCH_SUPERVISOR_STATUS_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        status = json.load(handle)
    print(
        status.get("phase", ""),
        status.get("detail", ""),
        status.get("consecutive_failures", 0),
        status.get("next_restart_epoch", 0),
        sep="\t",
    )
except (OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
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
    local pid="$1"
    local start_time="$SECONDS"
    local max_deadline=$((start_time + MAX_START_TIMEOUT))
    local inactivity_deadline=$((start_time + STORE_START_TIMEOUT))
    local last_phase="" last_updated=0 status_raw phase detail updated_at

    while [ "$SECONDS" -lt "$inactivity_deadline" ] && [ "$SECONDS" -lt "$max_deadline" ]; do
        # If the launched process died, return immediately without waiting.
        if ! process_alive "$pid"; then
            return 2
        fi

        # Authoritative readiness remains strictly endpoint identity and persistence lock.
        if store_owner_valid "$pid" "$(store_lock_start_identity 2>/dev/null || true)" \
            && [ "$(endpoint_identity || true)" = "this-store" ] \
            && [ "$(store_lock_pid || true)" = "$pid" ]; then
            return 0
        fi

        # Advisory status monitoring & watchdog extension
        if status_raw="$(read_status_info "$STORE_STATUS_FILE" "$pid" 2>/dev/null)" && [ -n "$status_raw" ]; then
            IFS=$'\t' read -r phase detail updated_at <<< "$status_raw"
            local cur_sec="${updated_at%.*}"
            local prev_sec="${last_updated%.*}"
            # If heartbeat timestamp or phase advanced, reset inactivity deadline
            if [ "$phase" != "$last_phase" ] || [ "${cur_sec:-0}" -gt "${prev_sec:-0}" ]; then
                inactivity_deadline=$((SECONDS + INACTIVITY_TIMEOUT))
                if [ -n "$phase" ] && [ "$phase" != "$last_phase" ]; then
                    if [ -n "$detail" ]; then
                        echo "  [state-store] $phase: $detail"
                    else
                        echo "  [state-store] $phase"
                    fi
                fi
                last_phase="$phase"
                last_updated="$updated_at"
            fi
        fi

        sleep 0.2
    done

    return 1
}

wait_for_orchestrator() {
    local pid="$1"
    if [ -z "${ORCH_LEASE_WAIT_TIMEOUT:-}" ]; then
        initialize_launch_settings
    fi
    local start_time="$SECONDS"
    local ready_timeout=$((ORCH_START_TIMEOUT + ORCH_LEASE_WAIT_TIMEOUT))
    [ "$ready_timeout" -ge "$MAX_START_TIMEOUT" ] || ready_timeout="$MAX_START_TIMEOUT"
    local max_deadline=$((start_time + ready_timeout))
    local inactivity_deadline=$((start_time + ORCH_START_TIMEOUT))
    local lease_deadline=0
    local last_phase="" last_updated=0 status_raw phase detail updated_at

    while [ "$SECONDS" -lt "$inactivity_deadline" ] && [ "$SECONDS" -lt "$max_deadline" ]; do
        # Authoritative readiness remains control-plane lease and lock ownership.
        if orchestrator_owner_valid "$pid" && lock_is_held "$ORCH_LOCK" \
            && orchestrator_lease_owner_valid "$pid"; then
            return 0
        fi

        # If the process died, return immediately.
        if ! process_alive "$pid"; then
            return 1
        fi

        # Advisory status monitoring & watchdog extension
        if status_raw="$(read_status_info "$ORCH_STATUS_FILE" "$pid" 2>/dev/null)" && [ -n "$status_raw" ]; then
            IFS=$'\t' read -r phase detail updated_at <<< "$status_raw"
            local cur_sec="${updated_at%.*}"
            local prev_sec="${last_updated%.*}"
            if [ "$phase" != "$last_phase" ] || [ "${cur_sec:-0}" -gt "${prev_sec:-0}" ]; then
                if [ "$phase" = "acquiring_lease" ]; then
                    if [ "$last_phase" != "acquiring_lease" ]; then
                        lease_deadline=$((SECONDS + ORCH_LEASE_WAIT_TIMEOUT))
                    fi
                    inactivity_deadline=$lease_deadline
                else
                    lease_deadline=0
                    inactivity_deadline=$((SECONDS + INACTIVITY_TIMEOUT))
                fi
                if [ -n "$phase" ] && [ "$phase" != "$last_phase" ]; then
                    if [ -n "$detail" ]; then
                        echo "  [orchestrator] $phase: $detail"
                    else
                        echo "  [orchestrator] $phase"
                    fi
                fi
                last_phase="$phase"
                last_updated="$updated_at"
            fi
        fi

        sleep 0.2
    done

    return 1
}

orchestrator_process_start_id() {
    local pid="$1" boot_id start_identity
    boot_id="$(tr -d '[:space:]' < /proc/sys/kernel/random/boot_id 2>/dev/null)" || return 1
    [ -n "$boot_id" ] || return 1
    start_identity="$(process_start_identity "$pid")"
    [[ "$start_identity" == "$pid:"* ]] || return 1
    printf '%s:%s' "$boot_id" "${start_identity#*:}"
}

orchestrator_lease_owner_valid() {
    local pid="$1" token expected_start_id
    expected_start_id="$(orchestrator_process_start_id "$pid")" || return 1
    token="$(read_api_token)"
    [[ "$token" != *$'\n'* && "$token" != *$'\r'* ]] || return 1
    token="$(printf '%s' "$token" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
    printf 'header = "Authorization: Bearer %s"\n' "$token" \
        | curl -fsS --max-time 1 --config - \
            "http://localhost:$STORE_PORT/api/v1/control/orchestrator-lease" 2>/dev/null \
        | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    lease = payload.get("lease") if isinstance(payload, dict) else None
    matches = (
        isinstance(lease, dict)
        and type(lease.get("pid")) is int
        and lease["pid"] == int(sys.argv[1])
        and lease.get("process_start_id") == sys.argv[2]
    )
except (TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)
' "$pid" "$expected_start_id"
}

terminate_verified_process() {
    local pid="$1" expected_identity="$2"
    python3 - "$pid" "$expected_identity" "$STOP_TIMEOUT" <<'PY'
import os
import select
import signal
import sys
from pathlib import Path

pid = int(sys.argv[1])
expected_identity = sys.argv[2]
timeout_ms = int(float(sys.argv[3]) * 1000)

if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
    raise SystemExit("pidfd signaling is unavailable; refusing unsafe PID signaling")

def start_identity():
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return f"{pid}:{fields[19]}"
    except (IndexError, OSError):
        return ""

try:
    pidfd = os.pidfd_open(pid, 0)
except OSError as exc:
    raise SystemExit(f"could not open pidfd for PID {pid}: {exc}") from exc

try:
    if start_identity() != expected_identity:
        raise SystemExit(f"PID {pid} changed identity; refusing to signal it")
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if poller.poll(0):
        raise SystemExit(0)
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    except ProcessLookupError:
        raise SystemExit(0) from None
    if poller.poll(timeout_ms):
        raise SystemExit(0)
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        raise SystemExit(0) from None
    if not poller.poll(5000):
        raise SystemExit(f"PID {pid} did not exit after SIGKILL")
finally:
    os.close(pidfd)
PY
}

cleanup_failed_orchestrator_start() {
    local pid="$1" expected_identity="$2" current_identity
    if process_alive "$pid"; then
        if ! orchestrator_owner_valid "$pid"; then
            error "failed orchestrator startup PID $pid is still alive but unverifiable"
            return 1
        fi
        echo "Cleaning up failed orchestrator startup (PID $pid)..."
        current_identity="$(process_start_identity "$pid")"
        if [ "$current_identity" != "$expected_identity" ] || [ -z "$expected_identity" ]; then
            error "failed orchestrator startup PID $pid changed identity; refusing to signal it"
            return 1
        fi
        if ! terminate_verified_process "$pid" "$expected_identity" \
            && process_alive "$pid"; then
            error "could not confirm failed orchestrator startup PID $pid stopped"
            return 1
        fi
    fi
    if lock_is_held "$ORCH_LOCK"; then
        error "orchestrator lock is still held after failed startup PID $pid"
        return 1
    fi
    clear_lock_metadata "$ORCH_LOCK" || {
        error "orchestrator lock changed ownership while the previous owner exited"
        return 1
    }
}

terminate_process() {
    local pid="$1" kind="$2" deadline lock_path="" label="state store" port_detail="port $STORE_PORT"
    local forced=0 lock_wait_deadline owner=""
    [ "$kind" = store ] && lock_path="$STORE_LOCK"
    if [ "$kind" = orchestrator ]; then
        lock_path="$ORCH_LOCK"
        label="orchestrator"
        port_detail="state-store port $STORE_PORT"
    fi
    echo "$label received SIGTERM (PID $pid; $port_detail); waiting up to ${STOP_TIMEOUT}s for graceful shutdown..."
    kill "$pid" 2>/dev/null || true
    deadline=$((SECONDS + STOP_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ] && process_alive "$pid"; do sleep 0.2; done
    if process_alive "$pid"; then
        echo "$label did not stop after ${STOP_TIMEOUT}s; sending SIGKILL (PID $pid; $port_detail)."
        kill -KILL "$pid" 2>/dev/null || true
        forced=1
    fi
    deadline=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$deadline" ] && process_alive "$pid"; do sleep 0.1; done
    if process_alive "$pid"; then
        error "$label PID $pid did not exit after SIGKILL; refusing to continue while it may hold $lock_path"
        return 1
    fi
    if [ -n "$lock_path" ] && lock_is_held "$lock_path"; then
        lock_wait_deadline=$((SECONDS + 5))
        while [ "$SECONDS" -lt "$lock_wait_deadline" ] && lock_is_held "$lock_path"; do
            sleep 0.1
        done
        if lock_is_held "$lock_path"; then
            if [ "$kind" = store ]; then
                owner="$(store_lock_pid || true)"
            else
                owner="$(orchestrator_pid || true)"
            fi
            error "$label PID $pid exited but $lock_path is still held by live PID ${owner:-unknown}; refusing unsafe removal"
            return 1
        fi
    fi
    if [ -n "$lock_path" ] && [ -f "$lock_path" ]; then
        if ! clear_lock_metadata "$lock_path"; then
            error "$label lock $lock_path changed ownership; refusing unsafe removal"
            return 1
        fi
        if [ "$forced" -eq 1 ]; then
            echo "Cleared stale $label lock metadata after confirmed process exit."
        fi
    fi
    echo "$label stopped (PID $pid; $port_detail)."
}

stop_orchestrator() {
    local pid=""
    stop_orchestrator_supervisor || return 1
    if lock_is_held "$ORCH_LOCK"; then
        pid="$(orchestrator_pid || true)"
        orchestrator_owner_valid "$pid" || {
            error "orchestrator lock is held by an unverifiable process (PID ${pid:-unknown}); refusing to signal it"
            return 1
        }
        echo "Stopping orchestrator (PID $pid; state-store port $STORE_PORT)..."
        terminate_process "$pid" orchestrator || { error "orchestrator did not release its lock"; return 1; }
    else
        if [ -f "$ORCH_LOCK" ]; then
            clear_lock_metadata "$ORCH_LOCK" || {
                error "orchestrator lock changed ownership while stopping"
                return 1
            }
            echo "Orchestrator already stopped (removed stale lock metadata)."
        else
            echo "Orchestrator already stopped."
        fi
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
        echo "Stopping state store (PID $pid; port $STORE_PORT)..."
        terminate_process "$pid" store || { error "state store did not release its persistence lock"; return 1; }
    elif [ -f "$STORE_LOCK" ]; then
        rm -f "$STORE_LOCK"
        echo "State store already stopped (removed stale lock metadata)."
    else
        echo "State store already stopped."
    fi
    rm -f "$STORE_PID_FILE"
}

start_store() {
    local pid="" expected=""
    if lock_is_held "$STORE_LOCK"; then
        pid="$(store_lock_pid || true)"
        expected="$(store_lock_start_identity || true)"
        if ! store_owner_valid "$pid" "$expected"; then
            if wait_for_lock_release "$STORE_LOCK" "State store"; then
                echo "Previous state-store lock owner released the lock; starting a fresh state store."
                rm -f "$STORE_PID_FILE"
            else
                error "state-store lock is held by an unverifiable process (PID ${pid:-unknown}); see $STORE_LOG"
                return 1
            fi
        else
            write_pid_file "$STORE_PID_FILE" "$pid"
            echo "State store already owns the persistence lock (PID $pid); waiting up to ${STORE_START_TIMEOUT}s for readiness..."
            wait_for_store "$pid" || {
                if ! lock_is_held "$STORE_LOCK"; then
                    echo "Previous state store PID $pid exited and released the lock; starting a fresh state store."
                    rm -f "$STORE_PID_FILE"
                else
                    error "state-store lock owner PID $pid is not responding as this instance; see $STORE_LOG"
                    return 1
                fi
            }
            if lock_is_held "$STORE_LOCK" && [ "$(store_lock_pid || true)" = "$pid" ] \
                && [ "$(endpoint_identity || true)" = "this-store" ]; then
                echo "State store already running (PID $pid; port $STORE_PORT); did not start a new service."
                echo "  Repaired PID metadata: $STORE_PID_FILE"
                echo "  The existing process was left untouched; its loaded revision is not changed by this checkout."
                return 2
            fi
        fi
    fi
    case "$(endpoint_identity || true)" in
        different-store) error "port $STORE_PORT is occupied by a different state store"; return 1 ;;
        this-store) error "state-store endpoint is reachable without this instance's lock"; return 1 ;;
    esac
    echo "Starting state store on port $STORE_PORT..."
    rm -f "$STORE_STATUS_FILE"
    rotate_log "$STORE_LOG"
    STORE_PORT="$STORE_PORT" nohup python3 -m uvicorn state_store.main:app \
        --host 0.0.0.0 --port "$STORE_PORT" --log-level warning > "$STORE_LOG" 2>&1 &
    pid=$!
    write_pid_file "$STORE_PID_FILE" "$pid"
    if wait_for_store "$pid"; then
        echo "State store started (PID $pid; port $STORE_PORT)."
        return 0
    fi
    error "state store failed to become ready; see $STORE_LOG"
    dump_process_diagnostics "$STORE_LOG" "State store" "$pid"
    if store_owner_valid "$pid" "$(store_lock_start_identity 2>/dev/null || true)"; then
        echo "Cleaning up failed state-store startup (PID $pid)..."
        terminate_process "$pid" store || true
    fi
    rm -f "$STORE_PID_FILE" "$STORE_STATUS_FILE"
    return 1
}

start_orchestrator() {
    if [ -z "${ORCH_LEASE_WAIT_TIMEOUT:-}" ]; then
        initialize_launch_settings
    fi
    local pid="" pid_start_identity="" attempt launch_timeout deadline supervisor_result
    launch_timeout=$((ORCH_START_TIMEOUT + ORCH_LEASE_WAIT_TIMEOUT))
    [ "$launch_timeout" -ge "$MAX_START_TIMEOUT" ] || launch_timeout="$MAX_START_TIMEOUT"
    deadline=$((SECONDS + launch_timeout))
    local supervisor=""
    if lock_is_held "$ORCH_LOCK"; then
        pid="$(orchestrator_pid || true)"
        if ! orchestrator_owner_valid "$pid"; then
            if wait_for_lock_release "$ORCH_LOCK" "Orchestrator"; then
                echo "Previous orchestrator lock owner released the lock; continuing with a fresh orchestrator start."
                clear_lock_metadata "$ORCH_LOCK" || {
                    error "orchestrator lock changed ownership before restart"
                    return 1
                }
            else
                error "orchestrator lock is held by an unverifiable process (PID ${pid:-unknown}); see $ORCH_LOG"
                return 1
            fi
        else
            start_orchestrator_supervisor || return 3
            echo "Orchestrator already owns its lock (PID $pid); waiting for lease readiness..."
            if ! wait_for_orchestrator "$pid"; then
                error "orchestrator PID $pid is running but has not acquired the leader lease; see $ORCH_LOG"
                return 3
            fi
            echo "Orchestrator already running (PID $pid; state-store port $STORE_PORT); did not start a new service."
            echo "  The existing process was left untouched; its loaded revision is not changed by this checkout."
            return 2
        fi
    fi
    supervisor="$(active_supervisor_pid || true)"
    if [ -z "$supervisor" ] && lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        error "orchestrator supervisor lock is held by an unverifiable process; see $ORCH_SUPERVISOR_LOG"
        return 3
    fi
    if [ -n "$supervisor" ]; then
        if [ "$(supervisor_pid)" != "$supervisor" ]; then
            write_pid_file "$ORCH_SUPERVISOR_PID_FILE" "$supervisor"
            echo "Repaired orchestrator supervisor PID metadata: $ORCH_SUPERVISOR_PID_FILE"
        fi
        echo "Orchestrator supervisor is active; waiting for it to start the service..."
        while [ "$SECONDS" -lt "$deadline" ]; do
            if lock_is_held "$ORCH_LOCK"; then
                pid="$(orchestrator_pid || true)"
                if orchestrator_owner_valid "$pid"; then
                    if wait_for_orchestrator "$pid"; then
                        echo "Orchestrator started by supervisor (PID $pid; state-store port $STORE_PORT)."
                        return 2
                    fi
                    error "supervisor-started orchestrator PID $pid has not acquired the leader lease; see $ORCH_LOG"
                    return 3
                fi
            fi
            supervisor_owner_valid "$supervisor" || break
            sleep 0.1
        done
        error "orchestrator supervisor did not start the service; see $ORCH_SUPERVISOR_LOG"
        return 3
    fi
    clear_lock_metadata "$ORCH_LOCK" || {
        error "orchestrator lock changed ownership before startup"
        return 1
    }
    while [ "$SECONDS" -lt "$deadline" ]; do
        # The orchestrator owns this file after launch.  Never replace a
        # pathname that may still be locked by a process exiting between
        # retries.
        if ! lock_is_held "$ORCH_LOCK"; then
            clear_lock_metadata "$ORCH_LOCK" || {
                error "orchestrator lock changed ownership during startup"
                return 1
            }
        fi
        echo "Starting orchestrator..."
        rm -f "$ORCH_STATUS_FILE"
        rotate_log "$ORCH_LOG"
        nohup python3 -m orchestrator.main > "$ORCH_LOG" 2>&1 &
        pid=$!
        for attempt in 1 2 3 4 5; do
            pid_start_identity="$(process_start_identity "$pid")"
            [[ "$pid_start_identity" == "$pid:"* ]] && break
            process_alive "$pid" || break
            sleep 0.02
        done
        if [[ ! "$pid_start_identity" == "$pid:"* ]]; then
            pid_start_identity=""
        fi
        if wait_for_orchestrator "$pid"; then
            supervisor_result=0
            start_orchestrator_supervisor || supervisor_result=$?
            if [ "$supervisor_result" -ne 0 ]; then
                if [ "$supervisor_result" -eq 3 ]; then
                    return 3
                fi
                if ! terminate_process "$pid" orchestrator; then
                    error "orchestrator startup cleanup is unconfirmed after supervisor failure"
                    return 3
                fi
                return 1
            fi
            echo "Orchestrator started (PID $pid; state-store port $STORE_PORT)."
            return 0
        fi
        if ! cleanup_failed_orchestrator_start "$pid" "$pid_start_identity"; then
            return 3
        fi
        if grep -q "leader lease unavailable" "$ORCH_LOG" 2>/dev/null; then
            echo "Orchestrator is waiting for the previous leader lease to expire; retrying..."
            sleep 1
            continue
        fi
        error "orchestrator failed to become ready; see $ORCH_LOG"
        dump_process_diagnostics "$ORCH_LOG" "Orchestrator" "$pid"
        rm -f "$ORCH_STATUS_FILE"
        if ! lock_is_held "$ORCH_LOCK"; then
            clear_lock_metadata "$ORCH_LOCK" || {
                error "could not safely clear the failed orchestrator lock"
                return 3
            }
        fi
        return 1
    done
    error "orchestrator did not become ready within ${launch_timeout}s; see $ORCH_LOG"
    dump_process_diagnostics "$ORCH_LOG" "Orchestrator" "${pid:-unknown}"
    rm -f "$ORCH_STATUS_FILE"
    return 1
}

start_orchestrator_supervisor() {
    local pid="" deadline
    pid="$(active_supervisor_pid || true)"
    if [ -n "$pid" ]; then
        if [ "$(supervisor_pid)" != "$pid" ]; then
            write_pid_file "$ORCH_SUPERVISOR_PID_FILE" "$pid"
            echo "Repaired orchestrator supervisor PID metadata: $ORCH_SUPERVISOR_PID_FILE"
        fi
        return 0
    fi
    if lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        error "orchestrator supervisor lock is held by an unverifiable process; see $ORCH_SUPERVISOR_LOG"
        return 3
    fi
    rm -f "$ORCH_SUPERVISOR_PID_FILE" "$ORCH_SUPERVISOR_STATUS_FILE"
    rotate_log "$ORCH_SUPERVISOR_LOG"
    START_BG_RESTART_BACKOFF="$RESTART_BACKOFF" \
    START_BG_RESTART_BACKOFF_MAX="$RESTART_BACKOFF_MAX" \
    START_BG_RESTART_STABLE_SECONDS="$RESTART_STABLE_SECONDS" \
    START_BG_CRASH_LOOP_THRESHOLD="$CRASH_LOOP_THRESHOLD" \
        nohup bash "$SCRIPT_DIR/start-bg.sh" __supervise-orchestrator \
        >> "$ORCH_SUPERVISOR_LOG" 2>&1 &
    pid=$!
    write_pid_file "$ORCH_SUPERVISOR_PID_FILE" "$pid"
    deadline=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$deadline" ]; do
        supervisor_owner_valid "$pid" && return 0
        process_alive "$pid" || break
        sleep 0.05
    done
    pid="$(active_supervisor_pid || true)"
    if [ -n "$pid" ]; then
        write_pid_file "$ORCH_SUPERVISOR_PID_FILE" "$pid"
        echo "Repaired orchestrator supervisor PID metadata: $ORCH_SUPERVISOR_PID_FILE"
        return 0
    fi
    if lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        error "orchestrator supervisor lock is held by an unverifiable process; see $ORCH_SUPERVISOR_LOG"
        return 3
    fi
    error "orchestrator supervisor failed to start; see $ORCH_SUPERVISOR_LOG"
    rm -f "$ORCH_SUPERVISOR_PID_FILE"
    return 1
}

stop_orchestrator_supervisor() {
    local pid="" identity="" deadline
    if ! lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        rm -f "$ORCH_SUPERVISOR_PID_FILE" "$ORCH_SUPERVISOR_STATUS_FILE"
        return 0
    fi
    pid="$(supervisor_lock_pid || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || {
        error "orchestrator supervisor lock is held by an unverifiable process (PID ${pid:-unknown}); refusing to stop"
        return 1
    }
    identity="$(process_start_identity "$pid")"
    if [ -z "$identity" ]; then
        error "orchestrator supervisor lock is held by an unverifiable process (PID $pid); refusing to stop"
        return 1
    fi
    if ! supervisor_owner_valid "$pid"; then
        error "orchestrator supervisor lock is held by an unverifiable process (PID ${pid:-unknown}); refusing to stop"
        return 1
    fi
    echo "Stopping orchestrator supervisor (PID $pid)..."
    if ! terminate_verified_process "$pid" "$identity" && process_alive "$pid"; then
        error "orchestrator supervisor PID $pid did not exit"
        return 1
    fi
    deadline=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$deadline" ] && lock_is_held "$ORCH_SUPERVISOR_LOCK"; do
        sleep 0.1
    done
    if lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        error "orchestrator supervisor PID $pid exited but its lock is still held"
        return 1
    fi
    clear_lock_metadata "$ORCH_SUPERVISOR_LOCK" || {
        error "orchestrator supervisor lock changed ownership while stopping"
        return 1
    }
    rm -f "$ORCH_SUPERVISOR_PID_FILE" "$ORCH_SUPERVISOR_STATUS_FILE"
}

cmd_supervise_orchestrator() {
    local child="" stopping=0 failures=0 exit_status=0 delay=0
    local started_at=0 now=0 elapsed=0 next_restart=0

    supervisor_pause() {
        local seconds="$1"
        local deadline=$((SECONDS + seconds))
        while [ "$stopping" -eq 0 ] && [ "$SECONDS" -lt "$deadline" ]; do
            sleep 0.2 {supervisor_fd}>&- || true
        done
    }

    initialize_launch_settings
    validate_launch_settings || exit $?
    exec {supervisor_fd}<>"$ORCH_SUPERVISOR_LOCK"
    flock -n "$supervisor_fd" || exit 0
    printf '%s\n' "$$" > "$ORCH_SUPERVISOR_LOCK"
    trap 'stopping=1; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; fi' TERM INT
    write_pid_file "$ORCH_SUPERVISOR_PID_FILE" "$$"
    write_supervisor_status "starting" "waiting for an orchestrator process" 0 0
    append_supervisor_log "orchestrator supervisor started"
    while [ "$stopping" -eq 0 ]; do
        if lock_is_held "$ORCH_LOCK"; then
            write_supervisor_status \
                "monitoring" "orchestrator PID $(orchestrator_pid || echo unknown)" \
                "$failures" 0
            supervisor_pause 2
            continue
        fi
        [ "$stopping" -eq 0 ] || break
        if ! clear_lock_metadata "$ORCH_LOCK"; then
            append_supervisor_log \
                "orchestrator lock changed ownership before launch; retrying"
            supervisor_pause 2
            continue
        fi
        rm -f "$ORCH_STATUS_FILE"
        rotate_log "$ORCH_LOG"
        started_at="$(date +%s)"
        append_supervisor_log \
            "starting orchestrator (restart failure count: $failures)"
        # The child must not inherit the supervisor lock; otherwise a crashed
        # supervisor cannot be replaced while that child remains alive.
        nohup python3 -m orchestrator.main > "$ORCH_LOG" 2>&1 {supervisor_fd}>&- &
        child=$!
        if [ "$stopping" -ne 0 ]; then
            kill -TERM "$child" 2>/dev/null || true
        fi
        write_supervisor_status "running" "orchestrator PID $child" "$failures" 0
        if wait "$child"; then
            exit_status=0
        else
            exit_status=$?
        fi
        child=""
        [ "$stopping" -eq 0 ] || break
        now="$(date +%s)"
        elapsed=$((now - started_at))
        if [ "$elapsed" -ge "$RESTART_STABLE_SECONDS" ]; then
            failures=0
        else
            failures=$((failures + 1))
        fi
        delay="$RESTART_BACKOFF"
        local exponent=$((failures > 1 ? failures - 1 : 0))
        local step=0
        while [ "$step" -lt "$exponent" ] && [ "$delay" -lt "$RESTART_BACKOFF_MAX" ]; do
            delay=$((delay * 2))
            step=$((step + 1))
        done
        [ "$delay" -le "$RESTART_BACKOFF_MAX" ] || delay="$RESTART_BACKOFF_MAX"
        next_restart=$((now + delay))
        if [ "$failures" -ge "$CRASH_LOOP_THRESHOLD" ]; then
            append_supervisor_log \
                "CRASH LOOP: orchestrator exited with status $exit_status; restarting in ${delay}s (failures=$failures)"
            write_supervisor_status \
                "crash_looping" \
                "orchestrator exited with status $exit_status; retrying in ${delay}s" \
                "$failures" "$next_restart"
        else
            append_supervisor_log \
                "orchestrator exited with status $exit_status; restarting in ${delay}s (failures=$failures)"
            write_supervisor_status \
                "restarting" \
                "orchestrator exited with status $exit_status; retrying in ${delay}s" \
                "$failures" "$next_restart"
        fi
        supervisor_pause "$delay"
    done
    rm -f "$ORCH_SUPERVISOR_PID_FILE" "$ORCH_SUPERVISOR_STATUS_FILE"
    append_supervisor_log "orchestrator supervisor stopped"
}

acquire_lifecycle_lock() {
    exec {launch_fd}>"$LAUNCH_LOCK"
    flock -n "$launch_fd" || {
        exec {launch_fd}>&-
        error "another start-bg.sh invocation is starting or stopping $AP_HOME"
        return 1
    }
}

release_lifecycle_lock() {
    flock -u "$launch_fd" || true
    exec {launch_fd}>&-
}

prepare_start() {
    initialize_launch_settings
    validate_launch_settings || return $?
    [ -f "$CONFIG" ] || { error "config file not found: $CONFIG"; return 1; }
    validate_application_config || return $?
}

cmd_start_locked() {
    echo "Launcher checkout revision: $(revision)"
    local store_result=0 orch_result=0 store_started=0
    if start_store; then store_started=1; else store_result=$?; [ "$store_result" -eq 2 ] || return 1; fi
    if start_orchestrator; then
        orch_result=0
    else
        orch_result=$?
        if [ "$orch_result" -eq 2 ]; then
            orch_result=0
        elif [ "$orch_result" -eq 3 ]; then
            error "orchestrator startup did not reach a safe stopped state; leaving the state store running for safety"
            return 1
        elif [ "$store_started" -eq 1 ]; then
            error "orchestrator startup failed; rolling back the state store started by this invocation"
            stop_store || true
            return 1
        else
            return 1
        fi
    fi
    echo "Services running."
}

cmd_start() {
    prepare_start || return $?
    acquire_lifecycle_lock || return 1
    local status=0
    cmd_start_locked || status=$?
    release_lifecycle_lock
    return "$status"
}

cmd_stop_locked() {
    local -a failed_services=()
    echo "Launcher checkout revision: $(revision)"
    echo "Stopping services..."
    if ! stop_orchestrator; then
        failed_services+=(orchestrator)
        error "orchestrator shutdown is unconfirmed; leaving the state store running"
    elif ! stop_store; then
        failed_services+=(state-store)
    fi
    if [ "${#failed_services[@]}" -ne 0 ]; then
        error "could not stop service(s): ${failed_services[*]}"
        return 1
    fi
    echo "Services stopped."
}

cmd_stop() {
    acquire_lifecycle_lock || return 1
    local status=0
    cmd_stop_locked || status=$?
    release_lifecycle_lock
    return "$status"
}

cmd_restart() {
    acquire_lifecycle_lock || return 1
    echo "Restarting services..."
    local status=0
    prepare_start || status=$?
    if [ "$status" -eq 0 ]; then
        cmd_stop_locked || status=$?
    fi
    if [ "$status" -eq 0 ]; then
        cmd_start_locked || status=$?
    fi
    release_lifecycle_lock
    return "$status"
}

cmd_status() {
    local store_pid="" orch_pid="" supervisor_status="" supervisor_phase="" supervisor_detail=""
    local supervisor_failures="" supervisor_next=""
    echo "Launcher checkout revision: $(revision)"
    if lock_is_held "$STORE_LOCK"; then
        store_pid="$(store_lock_pid || true)"
        if store_owner_valid "$store_pid" "$(store_lock_start_identity 2>/dev/null || true)"; then
            echo "State store:  RUNNING (PID $store_pid; port $STORE_PORT; endpoint $(endpoint_identity || echo unavailable))"
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
            echo "Orchestrator: RUNNING (PID $orch_pid; state-store port $STORE_PORT)"
        else
            echo "Orchestrator: LOCK HELD BY UNKNOWN OWNER (PID ${orch_pid:-unknown})"
        fi
    else
        echo "Orchestrator: STOPPED"
    fi
    orch_pid="$(supervisor_lock_pid || true)"
    if lock_is_held "$ORCH_SUPERVISOR_LOCK" && supervisor_owner_valid "$orch_pid"; then
        supervisor_status="$(read_supervisor_status 2>/dev/null || true)"
        IFS=$'\t' read -r supervisor_phase supervisor_detail supervisor_failures \
            supervisor_next <<< "$supervisor_status" || true
        echo "Orchestrator supervisor: RUNNING (PID $orch_pid; phase ${supervisor_phase:-unknown}; failures ${supervisor_failures:-0})"
        if [ -n "$supervisor_detail" ]; then
            echo "  $supervisor_detail"
        fi
    elif lock_is_held "$ORCH_SUPERVISOR_LOCK"; then
        echo "Orchestrator supervisor: LOCK HELD BY UNKNOWN OWNER (PID ${orch_pid:-unknown})"
    elif [ -f "$ORCH_SUPERVISOR_STATUS_FILE" ]; then
        supervisor_status="$(read_supervisor_status 2>/dev/null || true)"
        IFS=$'\t' read -r supervisor_phase supervisor_detail supervisor_failures \
            supervisor_next <<< "$supervisor_status" || true
        echo "Orchestrator supervisor: STOPPED (last phase ${supervisor_phase:-unknown})"
    else
        echo "Orchestrator supervisor: STOPPED"
    fi
}

case "${1:-start}" in
    start) cmd_start ;;
    restart) cmd_restart ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    __supervise-orchestrator) cmd_supervise_orchestrator ;;
    *) error "usage: $0 {start|restart|stop|status}"; exit 2 ;;
esac
