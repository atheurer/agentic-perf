#!/usr/bin/env bash
# Create and operate an isolated agentic-perf development instance.
#
# The base ref is intentionally mandatory for prepare. This prevents a dirty
# primary worktree from becoming the accidental starting point for a test.
#
# Examples:
#   ./scripts/dev-instance.sh prepare --issue 123 --base origin/main
#   ./scripts/dev-instance.sh start --name issue-123
#   ./scripts/dev-instance.sh shell --name issue-123
#   ./scripts/dev-instance.sh list --include-default
#   ./scripts/dev-instance.sh test --name issue-123
#   ./scripts/dev-instance.sh stop --name issue-123
#   ./scripts/dev-instance.sh cleanup --name issue-123
#   ./scripts/dev-instance.sh commit --name issue-123 -m "fix: ..."

set -euo pipefail

script_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dev_root="${AGENTIC_PERF_DEV_ROOT:-$(dirname "$script_repo")}"
home_root="${AGENTIC_PERF_INSTANCE_ROOT:-$HOME/.agentic-perf-instances}"

usage() {
    sed -n '1,23p' "${BASH_SOURCE[0]}"
    cat <<'EOF'

Commands:
  prepare --issue NUMBER --base REF [options]
  list [--include-default]
  start --name NAME
  shell --name NAME
  status --name NAME
  stop --name NAME
  test --name NAME [-- pytest arguments]
  validate --name NAME
  commit --name NAME -m MESSAGE
  cleanup --name NAME [--delete-state] [--delete-branch] [--force]

Prepare options:
  --issue NUMBER       GitHub issue to read with gh
  --base REF           Required git starting ref, e.g. origin/main
  --name NAME          Instance/branch name (default: issue-NUMBER-slug)
  --port PORT          State-store port (default: automatically selected)
  --worktree PATH      Explicit worktree path
  --instance-home PATH Explicit AGENTIC_PERF_HOME path
  --source-config PATH Config to copy (default: ~/.agentic-perf/config.json)
  --provider NAME      LLM provider for the generated config
  --model NAME         LLM model for the generated config
  --clone              Clone instead of using git worktree
  --delete-state       Delete the isolated runtime directory during cleanup
  --delete-branch      Delete the local branch during cleanup
  --force              Allow removal of dirty worktrees or active state
  --yes                Do not prompt before deleting state
  -h, --help           Show this help
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

name=""
issue=""
base=""
port=""
worktree=""
instance_home=""
source_config="$HOME/.agentic-perf/config.json"
provider=""
model=""
clone_mode=0
message=""
delete_state=0
delete_branch=0
force=0
yes=0
include_default=0

instance_paths() {
    [ -n "$name" ] || die "--name is required"
    worktree="${worktree:-$dev_root/agentic-perf-$name}"
    instance_home="${instance_home:-$home_root/$name}"
    config="$instance_home/config.json"
    store_url="http://localhost:${port:-0}"
}

read_issue() {
    require_cmd gh
    issue_json="$(gh issue view "$issue" --json number,title,body,url)" \
        || die "could not read GitHub issue #$issue"
    issue_title="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["title"])' <<<"$issue_json")"
    issue_url="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<<"$issue_json")"
    issue_slug="$(python3 -c '
import re, sys
title = sys.stdin.read().strip().lower()
slug = re.sub(r"[^a-z0-9]+", "-", title).strip("-")
print(slug[: fifty] if (fifty := 50) else slug)
' <<<"$issue_title")"
}

choose_port() {
    if [ -n "$port" ]; then
        [[ "$port" =~ ^[0-9]+$ ]] || die "--port must be numeric"
        [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] \
            || die "--port must be between 1024 and 65535"
        return
    fi
    for candidate in $(seq 18090 18190); do
        if ! ss -H -ltn "sport = :$candidate" 2>/dev/null | grep -q .; then
            port="$candidate"
            return
        fi
    done
    die "could not find an unused port in 18090-18190; pass --port"
}

write_config() {
    [ -f "$source_config" ] || die "config not found: $source_config"
    mkdir -p "$instance_home"
    SOURCE_CONFIG="$source_config" DEST_CONFIG="$config" \
        CONFIG_PROVIDER="$provider" CONFIG_MODEL="$model" \
        CONFIG_PORT="$port" CONFIG_URL="$store_url" CONFIG_NAME="$name" \
        python3 - <<'PY'
import json
import os
from pathlib import Path

source = json.loads(Path(os.environ["SOURCE_CONFIG"]).read_text())
for key in ("api_key", "anthropic_api_key", "gemini_api_key", "openai_api_key"):
    source.pop(key, None)

llm = dict(source.get("llm", {}))
if os.environ.get("CONFIG_PROVIDER"):
    llm["provider"] = os.environ["CONFIG_PROVIDER"]
if os.environ.get("CONFIG_MODEL"):
    llm["model"] = os.environ["CONFIG_MODEL"]
source["llm"] = llm
source["instance_name"] = os.environ["CONFIG_NAME"]
source["state_store"] = {
    "url": os.environ["CONFIG_URL"],
    "port": int(os.environ["CONFIG_PORT"]),
}

Path(os.environ["DEST_CONFIG"]).write_text(json.dumps(source, indent=4) + "\n")
PY
}

prepare() {
    [ -n "$issue" ] || die "prepare requires --issue"
    [ -n "$base" ] || die "prepare requires --base (for example: origin/main)"
    require_cmd git
    require_cmd python3
    require_cmd ss
    read_issue

    if [ -z "$name" ]; then
        name="issue-$issue-$issue_slug"
    fi
    [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] \
        || die "invalid name: $name"
    choose_port
    instance_paths

    git -C "$script_repo" rev-parse --verify "$base^{commit}" >/dev/null \
        || die "base ref does not resolve: $base"
    [ ! -e "$worktree" ] || die "worktree path already exists: $worktree"
    [ ! -e "$instance_home" ] || die "instance home already exists: $instance_home"

    mkdir -p "$(dirname "$worktree")"
    if [ "$clone_mode" -eq 1 ]; then
        git clone "$script_repo" "$worktree"
        git -C "$worktree" fetch origin
        git -C "$worktree" switch --detach "$base"
        git -C "$worktree" switch -c "$name"
    else
        git -C "$script_repo" worktree add -b "$name" "$worktree" "$base"
    fi
    write_config
    printf '%s\n' "$worktree" > "$instance_home/worktree.path"

    cat <<EOF
Prepared isolated instance.
  Issue:       #$issue — $issue_title
  URL:         $issue_url
  Base:        $base
  Branch:      $name
  Worktree:    $worktree
  AP home:     $instance_home
  Store URL:   $store_url
  Config:      $config

API keys are not copied. Export the desired provider key before 'start'.
EOF
}

process_matches() {
    local pid="$1"
    local pattern="$2"
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -Fq -- "$pattern"
}

pid_from_file() {
    local pid_file="$1"
    local pattern="$2"
    local expected_cwd="$3"
    [ -f "$pid_file" ] || return 1
    local pid
    pid="$(tr -d '[:space:]' < "$pid_file")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    process_matches "$pid" "$pattern" || return 1
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" = "$expected_cwd" ] \
        || return 1
    printf '%s' "$pid"
}

list_one_instance() {
    local label="$1"
    local ap_home="$2"
    local config_path="$ap_home/config.json"
    local configured_port="-"
    local orch_pid="-"
    local store_pid="-"
    local status="stopped"
    local instance_worktree="-"

    if [ -f "$config_path" ]; then
        configured_port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state_store", {}).get("port", "-"))' "$config_path" 2>/dev/null || printf '%s' '?')"
    fi
    if [ -f "$ap_home/worktree.path" ]; then
        instance_worktree="$(tr -d '\n' < "$ap_home/worktree.path")"
    elif [ "$label" = "default" ]; then
        instance_worktree="$script_repo"
    else
        instance_worktree="$dev_root/agentic-perf-$label"
    fi

    orch_pid="$(pid_from_file "$ap_home/orchestrator.pid" "orchestrator.main" "$instance_worktree" || printf '%s' '-')"
    store_pid="$(pid_from_file "$ap_home/logs/state-store.pid" "state_store.main:app" "$instance_worktree" || printf '%s' '-')"
    if [ "$orch_pid" != "-" ] || [ "$store_pid" != "-" ]; then
        status="running"
    fi
    printf 'Instance:        %s\n' "$label"
    printf '  Status:        %s\n' "$status"
    printf '  Store port:    %s\n' "$configured_port"
    printf '  Orchestrator:  %s\n' "$orch_pid"
    printf '  State store:   %s\n' "$store_pid"
    printf '  Worktree:      %s\n' "$instance_worktree"
    printf '  Runtime home:  %s\n\n' "$ap_home"
}

list_instances() {
    local found=0
    if [ "$include_default" -eq 1 ] && [ -d "$HOME/.agentic-perf" ]; then
        list_one_instance "default" "$HOME/.agentic-perf"
        found=1
    fi
    if [ -d "$home_root" ]; then
        local ap_home
        for ap_home in "$home_root"/*; do
            [ -d "$ap_home" ] || continue
            list_one_instance "$(basename "$ap_home")" "$ap_home"
            found=1
        done
    fi
    [ "$found" -eq 1 ] || echo "No managed instances found."
}

run_instance_command() {
    instance_paths
    [ -d "$worktree" ] || die "worktree not found: $worktree"
    [ -f "$config" ] || die "instance config not found: $config"
    instance_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_store"]["url"])' "$config")"
    AGENTIC_PERF_HOME="$instance_home" STATE_STORE_URL="$instance_url" \
        "$worktree/scripts/start-bg.sh" "$@"
}

load_instance() {
    instance_paths
    [ -d "$worktree" ] || die "worktree not found: $worktree"
    [ -f "$config" ] || die "instance config not found: $config"
    instance_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_store"]["url"])' "$config")"
}

instance_services_running() {
    local store_pid_file="$instance_home/logs/state-store.pid"
    local orch_pid_file="$instance_home/orchestrator.pid"
    if [ -f "$store_pid_file" ]; then
        local store_pid
        store_pid="$(pid_from_file "$store_pid_file" "state_store.main:app" "$worktree" || true)"
        if [ -n "$store_pid" ]; then
            return 0
        fi
    fi
    if [ -f "$orch_pid_file" ]; then
        if python3 - "$orch_pid_file" <<'PY'
import fcntl
import sys

try:
    with open(sys.argv[1]) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (BlockingIOError, OSError):
    sys.exit(0)
sys.exit(1)
PY
        then
            return 0
        fi
    fi
    return 1
}

instance_has_active_tickets() {
    INSTANCE_TICKETS="$instance_home/tickets" python3 - <<'PY'
import json
import os
from pathlib import Path

tickets = Path(os.environ["INSTANCE_TICKETS"])
for path in tickets.glob("PERF-*.json"):
    try:
        if json.loads(path.read_text()).get("status") != "closed":
            raise SystemExit(0)
    except (OSError, json.JSONDecodeError):
        raise SystemExit(0)
raise SystemExit(1)
PY
}

open_shell() {
    load_instance
    local prompt_name="$name"
    if [[ "$name" =~ ^issue-([0-9]+) ]]; then
        prompt_name="issue-${BASH_REMATCH[1]}"
    else
        prompt_name="${prompt_name:0:24}"
    fi
    export AGENTIC_PERF_HOME="$instance_home"
    export STATE_STORE_URL="$instance_url"
    export AP_INSTANCE_NAME="$name"
    export PS1="[ap:$prompt_name] $ "
    cd "$worktree"
    exec "${SHELL:-/bin/bash}" -i
}

cleanup() {
    instance_paths
    [ -f "$config" ] || die "instance config not found: $config"
    instance_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_store"]["url"])' "$config")"
    if instance_services_running; then
        die "services are still running; stop instance '$name' first"
    fi
    if instance_has_active_tickets && [ "$force" -ne 1 ]; then
        die "instance has active tickets; use --force only if deletion is intentional"
    fi

    if [ "$delete_state" -eq 1 ]; then
        [ "$yes" -eq 1 ] || {
            read -r -p "Delete all state under '$instance_home'? [y/N] " answer
            [[ "$answer" =~ ^[Yy]$ ]] || die "state deletion cancelled"
        }
    fi

    if git -C "$script_repo" worktree list --porcelain \
        | grep -Fqx "worktree $worktree"; then
        if [ "$force" -eq 1 ]; then
            git -C "$script_repo" worktree remove --force "$worktree"
        else
            git -C "$script_repo" worktree remove "$worktree"
        fi
    elif [ ! -e "$worktree" ]; then
        echo "Worktree already absent: $worktree"
    elif [ "$force" -eq 1 ]; then
        rm -rf -- "$worktree"
    else
        die "worktree is not registered; use --force to remove '$worktree'"
    fi

    if [ "$delete_branch" -eq 1 ]; then
        if [ "$force" -eq 1 ]; then
            git -C "$script_repo" branch -D "$name"
        else
            git -C "$script_repo" branch -d "$name"
        fi
    fi

    if [ "$delete_state" -eq 1 ]; then
        [ "$instance_home" != "/" ] && [ "$instance_home" != "$HOME" ] \
            || die "refusing to delete unsafe instance home: $instance_home"
        rm -rf -- "$instance_home"
    fi
    if [ "$delete_state" -eq 1 ]; then
        echo "Cleaned instance '$name' and deleted state"
    else
        echo "Cleaned instance '$name' (state preserved)"
    fi
}

parse_common() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --name) name="${2:?missing value for --name}"; shift 2 ;;
            --instance-home) instance_home="${2:?missing value for --instance-home}"; shift 2 ;;
            --worktree) worktree="${2:?missing value for --worktree}"; shift 2 ;;
            --port) port="${2:?missing value for --port}"; shift 2 ;;
            --provider) provider="${2:?missing value for --provider}"; shift 2 ;;
            --model) model="${2:?missing value for --model}"; shift 2 ;;
            --source-config) source_config="${2:?missing value for --source-config}"; shift 2 ;;
            --clone) clone_mode=1; shift ;;
            --delete-state) delete_state=1; shift ;;
            --delete-branch) delete_branch=1; shift ;;
            --force) force=1; shift ;;
            --yes) yes=1; shift ;;
            --include-default) include_default=1; shift ;;
            --issue) issue="${2:?missing value for --issue}"; shift 2 ;;
            --base) base="${2:?missing value for --base}"; shift 2 ;;
            -m|--message) message="${2:?missing value for --message}"; shift 2 ;;
            --) shift; break ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    extra_args=("$@")
}

[ "$#" -gt 0 ] || { usage; exit 1; }
case "$1" in
    -h|--help) usage; exit 0 ;;
esac
command_name="$1"
shift
extra_args=()
parse_common "$@"

case "$command_name" in
    prepare) prepare ;;
    list) list_instances ;;
    start|status|stop) run_instance_command "$command_name" ;;
    shell) open_shell ;;
    test)
        instance_paths
        [ -d "$worktree" ] || die "worktree not found: $worktree"
        AGENTIC_PERF_HOME="$instance_home" STATE_STORE_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_store"]["url"])' "$config")" \
            "$worktree/scripts/test.sh" "${extra_args[@]}"
        ;;
    validate)
        instance_paths
        [ -d "$worktree" ] || die "worktree not found: $worktree"
        AGENTIC_PERF_HOME="$instance_home" STATE_STORE_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_store"]["url"])' "$config")" \
            "$worktree/scripts/validate.sh"
        ;;
    commit)
        instance_paths
        [ -n "$message" ] || die "commit requires -m MESSAGE"
        git -C "$worktree" add -A
        git -C "$worktree" commit -m "$message"
        ;;
    cleanup) cleanup ;;
    *) die "unknown command: $command_name" ;;
esac
