"""Lifecycle tests for scripts/start-bg.sh."""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "start-bg.sh"
_FAKE_LEASE_RESPONSE = """\
lease_response() {
    local pid="$1" start_id
    start_id="$(python3 - "$pid" <<'PY'
from pathlib import Path
import sys

pid = sys.argv[1]
fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
print(f"{boot_id}:{fields[19]}")
PY
)"
    printf '{\"lease\":{\"pid\":%s,\"process_start_id\":\"%s\"}}\\n' "$pid" "$start_id"
}
"""


def _holder(path: Path, *, role: str, lock_path: Path, store_id: str) -> None:
    if role in {"store", "unverifiable-store"}:
        argv0 = (
            "python3 -m uvicorn state_store.main:app"
            if role == "store"
            else "python3 -m unrelated.service"
        )
        code = """
import fcntl, json, os, sys, time
lock_path, store_id = sys.argv[1:]
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
fields = open(f'/proc/{os.getpid()}/stat').read().rsplit(') ', 1)[1].split()
metadata = {
    'pid': os.getpid(),
    'process_start_identity': f'{os.getpid()}:{fields[19]}',
    'configured_port': 18903,
    'store_id': store_id,
}
os.ftruncate(fd, 0)
os.write(fd, json.dumps(metadata).encode())
os.fsync(fd)
time.sleep(600)
"""
    else:
        argv0 = "python3 -m orchestrator.main"
        code = """
import fcntl, os, sys, time
lock_path = sys.argv[1]
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
os.fsync(fd)
time.sleep(600)
"""
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        exec -a {shlex.quote(argv0)} /usr/bin/python3 -c {shlex.quote(code)} "$@"
        """
    ).lstrip()
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _stubborn_store_holder(path: Path, *, lock_path: Path, store_id: str) -> None:
    code = """
import fcntl, json, os, signal, sys, time
lock_path, store_id = sys.argv[1:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
fields = open(f'/proc/{os.getpid()}/stat').read().rsplit(') ', 1)[1].split()
metadata = {
    'pid': os.getpid(),
    'process_start_identity': f'{os.getpid()}:{fields[19]}',
    'configured_port': 18903,
    'store_id': store_id,
}
os.ftruncate(fd, 0)
os.write(fd, json.dumps(metadata).encode())
os.fsync(fd)
time.sleep(600)
"""
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        exec -a {shlex.quote("python3 -m uvicorn state_store.main:app")} \\
            /usr/bin/python3 -c {shlex.quote(code)} "$@"
        """
    ).lstrip()
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_start_repairs_metadata_and_stop_uses_lock_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "secrets").mkdir()
    store_id = str(uuid.uuid4())
    (home / "state-store.id").write_text(store_id + "\n")
    (home / "config.json").write_text(json.dumps({"state_store": {"port": 18903}}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\n{_FAKE_LEASE_RESPONSE}"
        'if [[ "$*" == *"/control/orchestrator-lease"* ]]; then\n'
        '  pid_file="$AGENTIC_PERF_HOME/orchestrator.pid"\n'
        '  if [ -f "$pid_file" ]; then\n'
        '    pid="$(tr -d \'[:space:]\' < "$pid_file")"\n'
        '    lease_response "$pid"\n'
        "  else\n"
        "    printf '{\"lease\":null}\\n'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'exec 9>>"$AGENTIC_PERF_HOME/state-store.lock"\n'
        "if flock -n 9; then flock -u 9; exit 7; fi\n"
        f"printf '%s\\n' '{{\"store_id\":\"{store_id}\"}}'\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    store_holder = tmp_path / "store-holder"
    orch_holder = tmp_path / "orch-holder"
    _holder(
        store_holder,
        role="store",
        lock_path=home / "state-store.lock",
        store_id=store_id,
    )
    _holder(
        orch_holder,
        role="orchestrator",
        lock_path=home / "orchestrator.pid",
        store_id=store_id,
    )
    env = os.environ.copy()
    env.update({"AGENTIC_PERF_HOME": str(home), "PATH": f"{fake_bin}:{env['PATH']}"})
    store_process = subprocess.Popen(
        [str(store_holder), str(home / "state-store.lock"), store_id],
        cwd=REPO,
        env=env,
    )
    orch_process = subprocess.Popen(
        [str(orch_holder), str(home / "orchestrator.pid")], cwd=REPO, env=env
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                ready = (home / "state-store.lock").stat().st_size > 0 and (
                    home / "orchestrator.pid"
                ).stat().st_size > 0
            except FileNotFoundError:
                ready = False
            if ready:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("fake service holders did not acquire their locks")
        store_pid_file = home / "logs" / "state-store.pid"
        store_pid_file.write_text(str(store_process.pid) + "\n")
        result = subprocess.run(
            [
                "bash",
                "-x",
                "-c",
                f"{SCRIPT} start; rm {store_pid_file}; {SCRIPT} start; {SCRIPT} stop; {SCRIPT} status",
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "State store already running" in result.stdout
        assert "waiting up to" in result.stdout
        assert "did not start a new service" in result.stdout
        assert "Orchestrator already running" in result.stdout
        assert "Services stopped" in result.stdout
        assert "State store:  STOPPED" in result.stdout
        assert "Orchestrator: STOPPED" in result.stdout
    finally:
        for process in (store_process, orch_process):
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=5)


def test_stop_reports_already_stopped_services(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    result = subprocess.run(
        [str(SCRIPT), "stop"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Orchestrator already stopped" in result.stdout
    assert "State store already stopped" in result.stdout
    assert "Services stopped" in result.stdout


def test_log_rotation_keeps_bounded_history(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_LOG_BACKUPS": "5",
        }
    )
    script = f"""
source {shlex.quote(str(SCRIPT))} status
for index in 1 2 3 4 5 6 7; do
    printf 'content-%s\\n' "$index" > "$AGENTIC_PERF_HOME/logs/orchestrator.log"
    rotate_log "$AGENTIC_PERF_HOME/logs/orchestrator.log"
done
"""
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "logs" / "orchestrator.log.1").read_text() == "content-7\n"
    assert (home / "logs" / "orchestrator.log.5").read_text() == "content-3\n"
    assert not (home / "logs" / "orchestrator.log.6").exists()


def test_stop_reclaims_lock_metadata_after_launcher_kills_process(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    store_id = str(uuid.uuid4())
    holder = tmp_path / "stubborn-store-holder"
    _stubborn_store_holder(
        holder,
        lock_path=home / "state-store.lock",
        store_id=store_id,
    )
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_STOP_TIMEOUT": "1",
        }
    )
    process = subprocess.Popen(
        [str(holder), str(home / "state-store.lock"), store_id],
        cwd=REPO,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (home / "state-store.lock").exists():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("stubborn store holder did not acquire its lock")
        lock_inode = (home / "state-store.lock").stat().st_ino

        result = subprocess.run(
            [str(SCRIPT), "stop"],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sending SIGKILL" in result.stdout
        assert "Cleared stale state store lock metadata" in result.stdout
        assert (home / "state-store.lock").read_text() == ""
        assert (home / "state-store.lock").stat().st_ino == lock_inode
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_lock_cleanup_refuses_live_replacement_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    store_id = str(uuid.uuid4())
    lock_path = home / "state-store.lock"
    holder = tmp_path / "stubborn-store-holder"
    _stubborn_store_holder(holder, lock_path=lock_path, store_id=store_id)
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    process = subprocess.Popen(
        [str(holder), str(lock_path), store_id],
        cwd=REPO,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if lock_path.exists() and lock_path.stat().st_size:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("replacement store holder did not acquire its lock")
        inode = lock_path.stat().st_ino
        contents = lock_path.read_text()
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {shlex.quote(str(SCRIPT))} status; "
                f"clear_lock_metadata {shlex.quote(str(lock_path))}",
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert lock_path.stat().st_ino == inode
        assert lock_path.read_text() == contents
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_fresh_start_waits_for_lock_creation(tmp_path: Path) -> None:
    """A new process may exist briefly before it creates its persistence lock."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "secrets").mkdir()
    store_id = str(uuid.uuid4())
    (home / "state-store.id").write_text(store_id + "\n")
    (home / "config.json").write_text(json.dumps({"state_store": {"port": 18903}}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\n{_FAKE_LEASE_RESPONSE}"
        'if [[ "$*" == *"/control/orchestrator-lease"* ]]; then\n'
        '  count_file="$AGENTIC_PERF_HOME/lease-query-count"\n'
        "  count=0\n"
        '  [ ! -f "$count_file" ] || read -r count < "$count_file"\n'
        "  count=$((count + 1))\n"
        '  printf \'%s\\n\' "$count" > "$count_file"\n'
        '  if [ -f "$AGENTIC_PERF_HOME/lease-force-mismatch" ]; then\n'
        '    pid="$(tr -d \'[:space:]\' < "$AGENTIC_PERF_HOME/orchestrator.pid")"\n'
        '    printf \'{"lease":{"pid":%s,"process_start_id":"wrong-incarnation"}}\\n\' "$pid"\n'
        "    exit 0\n"
        "  fi\n"
        '  if [ "$count" -lt 4 ]; then\n'
        '    if [ "$count" -eq 1 ]; then\n'
        '      pid="$(tr -d \'[:space:]\' < "$AGENTIC_PERF_HOME/orchestrator.pid")"\n'
        '      printf \'{"lease":{"pid":%s,"process_start_id":"wrong-incarnation"}}\\n\' "$pid"\n'
        '    elif [ "$count" -eq 2 ]; then\n'
        '      printf \'{"lease":{"pid":999999,"process_start_id":"other-process"}}\\n\'\n'
        "    else\n"
        "      printf '[]\\n'\n"
        "    fi\n"
        "    exit 0\n"
        "  fi\n"
        '  pid_file="$AGENTIC_PERF_HOME/orchestrator.pid"\n'
        '  if [ -f "$pid_file" ]; then\n'
        '    pid="$(tr -d \'[:space:]\' < "$pid_file")"\n'
        '    lease_response "$pid"\n'
        "  else\n"
        "    printf '{\"lease\":null}\\n'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'exec 9>>"$AGENTIC_PERF_HOME/state-store.lock"\n'
        "if flock -n 9; then flock -u 9; exit 7; fi\n"
        f"printf '%s\\n' '{{\"store_id\":\"{store_id}\"}}'\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    store_holder = tmp_path / "store-holder"
    orch_holder = tmp_path / "orch-holder"
    _holder(
        store_holder,
        role="store",
        lock_path=home / "state-store.lock",
        store_id=store_id,
    )
    _holder(
        orch_holder,
        role="orchestrator",
        lock_path=home / "orchestrator.pid",
        store_id=store_id,
    )
    nohup = fake_bin / "nohup"
    nohup.write_text(
        f"""#!/usr/bin/env bash
if [ \"$1\" = python3 ] && [ \"$2\" = -m ] && [ \"$3\" = uvicorn ]; then
    exec {store_holder} \"$AGENTIC_PERF_HOME/state-store.lock\" \"$TEST_STORE_ID\"
elif [ \"$1\" = python3 ] && [ \"$2\" = -m ] && [ \"$3\" = orchestrator.main ]; then
    exec {orch_holder} \"$AGENTIC_PERF_HOME/orchestrator.pid\"
fi
exec \"$@\"
"""
    )
    nohup.chmod(nohup.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TEST_STORE_ID": store_id,
            "START_BG_STORE_TIMEOUT": "2",
            "START_BG_ORCH_TIMEOUT": "2",
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"{SCRIPT} start; "
            '[ "$(cat "$AGENTIC_PERF_HOME/lease-query-count")" -ge 4 ] || exit 13; '
            f"{SCRIPT} start; {SCRIPT} restart; {SCRIPT} stop; "
            'touch "$AGENTIC_PERF_HOME/lease-force-mismatch"; '
            f"if {SCRIPT} start; then exit 12; fi; {SCRIPT} status",
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "State store started" in result.stdout
    assert "Orchestrator started" in result.stdout
    assert "Orchestrator already running" in result.stdout
    assert "Restarting services" in result.stdout
    assert "Services stopped" in result.stdout
    assert "Cleaning up failed orchestrator startup" in result.stdout
    assert "orchestrator failed to become ready" in result.stderr
    assert "Traceback" not in result.stderr
    assert "State store:  STOPPED" in result.stdout
    assert "Orchestrator: STOPPED" in result.stdout
    assert int((home / "lease-query-count").read_text()) >= 4


def test_start_waits_for_unverifiable_lock_owner_to_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "secrets").mkdir()
    store_id = str(uuid.uuid4())
    (home / "state-store.id").write_text(store_id + "\n")
    (home / "config.json").write_text(json.dumps({"state_store": {"port": 18903}}))
    holder = tmp_path / "unverifiable-holder"
    _holder(
        holder,
        role="unverifiable-store",
        lock_path=home / "state-store.lock",
        store_id=store_id,
    )
    env = os.environ.copy()
    env.update({"AGENTIC_PERF_HOME": str(home), "START_BG_STORE_TIMEOUT": "1"})
    process = subprocess.Popen(
        [str(holder), str(home / "state-store.lock"), store_id],
        cwd=REPO,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                ready = (home / "state-store.lock").stat().st_size > 0
            except FileNotFoundError:
                ready = False
            if ready:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("unverifiable holder did not acquire its lock")
        result = subprocess.run(
            [str(SCRIPT), "start"],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert result.returncode != 0
        assert "lock is held by an unverifiable process" in result.stdout
        assert "waiting up to" in result.stdout
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)


def test_start_store_surfaces_crash_diagnostics_promptly(tmp_path: Path) -> None:
    """If state store crashes immediately, launcher fails promptly and prints stderr diagnostics."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "secrets").mkdir()
    store_id = str(uuid.uuid4())
    (home / "state-store.id").write_text(store_id + "\n")
    (home / "config.json").write_text(json.dumps({"state_store": {"port": 18903}}))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Mock python3 to immediately fail with a traceback when invoking uvicorn state_store.main:app
    real_python = sys.executable
    mock_python = fake_bin / "python3"
    mock_python.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
    *"state_store.main:app"*)
        echo "Traceback (most recent call last):" >&2
        echo "  File \\"state_store/main.py\\", line 42, in <module>" >&2
        echo "ZeroDivisionError: division by zero" >&2
        exit 1
        ;;
    *)
        exec {shlex.quote(real_python)} "$@"
        ;;
esac
"""
    )
    mock_python.chmod(mock_python.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_STORE_TIMEOUT": "20",
        }
    )

    start = time.monotonic()
    result = subprocess.run(
        [str(SCRIPT), "start"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    elapsed = time.monotonic() - start

    assert result.returncode != 0
    # Must exit promptly rather than waiting out the 20s timeout
    assert elapsed < 5.0
    assert "DIAGNOSTICS: State store" in result.stderr
    assert "ZeroDivisionError: division by zero" in result.stderr


def test_start_store_watchdog_resets_on_heartbeat(tmp_path: Path) -> None:
    """State store startup watchdog resets inactivity timer when status updates arrive."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "secrets").mkdir()
    store_id = str(uuid.uuid4())
    (home / "state-store.id").write_text(store_id + "\n")
    (home / "config.json").write_text(json.dumps({"state_store": {"port": 18903}}))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_python = sys.executable
    code = f"""
import fcntl, json, os, sys, time
home = "{home}"
store_id = "{store_id}"
status_file = f"{{home}}/state-store.status"
lock_file = f"{{home}}/state-store.lock"

with open(status_file, "w") as f:
    json.dump({{"pid": os.getpid(), "phase": "initializing_trace_store", "detail": "", "updated_at": time.time()}}, f)
    f.write("\\n")
time.sleep(0.8)

with open(status_file, "w") as f:
    json.dump({{"pid": os.getpid(), "phase": "loading_tickets", "detail": "10/100", "updated_at": time.time()}}, f)
    f.write("\\n")
time.sleep(0.8)

fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
fields = open(f"/proc/{{os.getpid()}}/stat").read().rsplit(") ", 1)[1].split()
metadata = {{
    "pid": os.getpid(),
    "process_start_identity": f"{{os.getpid()}}:{{fields[19]}}",
    "configured_port": 18903,
    "store_id": store_id,
}}
os.ftruncate(fd, 0)
os.write(fd, json.dumps(metadata).encode())
os.fsync(fd)

with open(status_file, "w") as f:
    json.dump({{"pid": os.getpid(), "phase": "runtime_ready", "detail": "", "updated_at": time.time()}}, f)
    f.write("\\n")

time.sleep(300)
"""
    mock_python = fake_bin / "python3"
    mock_python.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
    *"state_store.main:app"*)
        exec -a "python3 -m uvicorn state_store.main:app" {shlex.quote(real_python)} -c {shlex.quote(code)}
        ;;
    *)
        exec {shlex.quote(real_python)} "$@"
        ;;
esac
"""
    )
    mock_python.chmod(mock_python.stat().st_mode | stat.S_IXUSR)

    curl = fake_bin / "curl"
    curl.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        {_FAKE_LEASE_RESPONSE}
        if [[ "$*" == *"/control/orchestrator-lease"* ]]; then
            pid_file="{home}/orchestrator.pid"
            if [ -f "$pid_file" ]; then
                pid="$(tr -d '[:space:]' < "$pid_file")"
                lease_response "$pid"
            else
                printf '{{"lease":null}}\\n'
            fi
            exit 0
        fi
        exec 9>>"{home}/state-store.lock"
        if flock -n 9; then flock -u 9; exit 7; fi
        printf '%s\\n' '{{"store_id":"{store_id}"}}'
    """)
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    orch_holder = tmp_path / "orch-holder"
    _holder(
        orch_holder,
        role="orchestrator",
        lock_path=home / "orchestrator.pid",
        store_id=store_id,
    )

    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_STORE_TIMEOUT": "1",
            "START_BG_INACTIVITY_TIMEOUT": "2",
        }
    )

    orch_process = subprocess.Popen(
        [str(orch_holder), str(home / "orchestrator.pid")], cwd=REPO, env=env
    )
    try:
        result = subprocess.run(
            [str(SCRIPT), "start"],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[state-store] initializing_trace_store" in result.stdout
        assert "[state-store] loading_tickets" in result.stdout
        assert "State store started" in result.stdout
    finally:
        orch_process.kill()
        subprocess.run([str(SCRIPT), "stop"], cwd=REPO, env=env, check=False)
