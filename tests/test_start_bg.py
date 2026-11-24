"""Lifecycle tests for scripts/start-bg.sh."""

from __future__ import annotations

import fcntl
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

import pytest

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


def _wait_for_store_lock_metadata(
    path: Path, *, expected_pid: int, store_id: str
) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            metadata = json.loads(path.read_text())
            if (
                metadata.get("pid") == expected_pid
                and metadata.get("store_id") == store_id
            ):
                return path.stat().st_ino
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("store holder did not publish lock metadata")


def _supervisor_lock_holder(path: Path, *, lock_path: Path, write_pid: bool) -> None:
    code = """
import fcntl, os, sys, time
from pathlib import Path

lock_path = sys.argv[1]
write_pid = sys.argv[2] == "1"
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
if write_pid:
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
Path(lock_path + ".ready").write_text("ready")
time.sleep(600)
"""
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        exec -a {shlex.quote(f"bash {SCRIPT} __supervise-orchestrator")} \\
            /usr/bin/python3 -c {shlex.quote(code)} "$@"
        """
    ).lstrip()
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_fake_orchestrator_python(
    path: Path, code: str, *, invocations: Path | None = None
) -> None:
    child_code = textwrap.dedent(code).strip()
    log_invocation = (
        f"    printf 'start\\n' >> {shlex.quote(str(invocations))}\n"
        if invocations is not None
        else ""
    )
    script = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "orchestrator.main" ]; then\n'
        f"{log_invocation}"
        f'    exec -a "python3 -m orchestrator.main" {shlex.quote(sys.executable)} '
        f"-c {shlex.quote(child_code)}\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
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
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_LOG_BACKUPS": "invalid",
            "START_BG_RESTART_BACKOFF": "invalid",
        }
    )
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


def test_start_stop_and_restart_share_lifecycle_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)

    with (home / "state-store-launch.lock").open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for action in ("start", "stop", "restart"):
                result = subprocess.run(
                    [str(SCRIPT), action],
                    cwd=REPO,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )

                assert result.returncode == 1, result.stdout + result.stderr
                assert (
                    "another start-bg.sh invocation is starting or stopping"
                    in result.stderr
                )
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def test_restart_preflights_settings_before_stopping_services(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_RESTART_BACKOFF": "invalid",
        }
    )
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
cmd_stop_locked() {{ touch "$AGENTIC_PERF_HOME/stopped-services"; return 0; }}
cmd_start_locked() {{ touch "$AGENTIC_PERF_HOME/started-services"; return 0; }}
if cmd_restart; then status=0; else status=$?; fi
[ "$status" -eq 2 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "START_BG_RESTART_BACKOFF must be a positive integer" in result.stderr
    assert not (home / "stopped-services").exists()
    assert not (home / "started-services").exists()


@pytest.mark.parametrize(
    ("config_text", "expected_error"),
    [
        ("{", "line 1"),
        (
            '{"trace": {"query": {"workers": 0}}}',
            "trace.query.workers must be > 0",
        ),
        ('{"chat": []}', "chat must be a JSON object"),
    ],
)
def test_restart_preflights_application_config_before_stopping_services(
    tmp_path: Path, config_text: str, expected_error: str
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text(config_text, encoding="utf-8")
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
cmd_stop_locked() {{ touch "$AGENTIC_PERF_HOME/stopped-services"; return 0; }}
cmd_start_locked() {{ touch "$AGENTIC_PERF_HOME/started-services"; return 0; }}
if cmd_restart; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected_error in result.stderr
    assert not (home / "stopped-services").exists()
    assert not (home / "started-services").exists()


def test_start_rejects_invalid_launch_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_RESTART_BACKOFF": "invalid",
        }
    )
    result = subprocess.run(
        [str(SCRIPT), "start"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "START_BG_RESTART_BACKOFF must be a positive integer" in result.stderr


def test_start_rejects_oversized_lease_timeout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_ORCH_LEASE_TIMEOUT": "9" * 30,
        }
    )
    result = subprocess.run(
        [str(SCRIPT), "start"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "START_BG_ORCH_LEASE_TIMEOUT must be <= 86400" in result.stderr


def test_start_rejects_oversized_restart_backoff(tmp_path: Path) -> None:
    for setting in (
        "START_BG_RESTART_BACKOFF",
        "START_BG_RESTART_BACKOFF_MAX",
    ):
        env = os.environ.copy()
        env.update(
            {
                "AGENTIC_PERF_HOME": str(tmp_path / setting.lower()),
                setting: "9" * 30,
            }
        )
        result = subprocess.run(
            [str(SCRIPT), "start"],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 2
        assert f"{setting} must be <= 86400" in result.stderr


def test_start_rejects_oversized_log_backups(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(tmp_path / "home"),
            "START_BG_LOG_BACKUPS": "101",
        }
    )
    result = subprocess.run(
        [str(SCRIPT), "start"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "START_BG_LOG_BACKUPS must be <= 100" in result.stderr


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


def test_supervisor_restarts_and_reports_crash_loop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    _write_fake_orchestrator_python(
        python,
        """
import fcntl, os
from pathlib import Path
folder = Path(os.environ["AGENTIC_PERF_HOME"])
fd = os.open(folder / "orchestrator.pid", os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
os.fsync(fd)
print("simulated orchestrator crash", flush=True)
raise SystemExit(7)
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_RESTART_BACKOFF": "1",
            "START_BG_RESTART_BACKOFF_MAX": "1",
            "START_BG_RESTART_STABLE_SECONDS": "60",
            "START_BG_CRASH_LOOP_THRESHOLD": "2",
            "START_BG_LOG_MAX_BYTES": "96",
        }
    )
    process = subprocess.Popen(
        [str(SCRIPT), "__supervise-orchestrator"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_path = home / "orchestrator-supervisor.status"
    try:
        deadline = time.monotonic() + 8
        status = {}
        while time.monotonic() < deadline:
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text())
                except (OSError, json.JSONDecodeError):
                    status = {}
                if status.get("phase") == "crash_looping":
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("supervisor did not enter crash-loop status")

        assert status["consecutive_failures"] >= 2
        assert status["next_restart_epoch"] > 0
        assert "status 7" in status["detail"]
        assert (home / "logs" / "orchestrator.log.1").exists()
        assert (
            "simulated orchestrator crash"
            in (home / "logs" / "orchestrator.log").read_text()
        )
        assert (
            "CRASH LOOP" in (home / "logs" / "orchestrator-supervisor.log").read_text()
        )
        supervisor_logs = list((home / "logs").glob("orchestrator-supervisor.log*"))
        assert len(supervisor_logs) > 1
        assert max(path.stat().st_size for path in supervisor_logs) < 512
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)


def test_supervisor_stop_during_backoff_does_not_restart_orchestrator(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocations = home / "orchestrator-invocations"
    python = fake_bin / "python3"
    _write_fake_orchestrator_python(
        python,
        """
import fcntl, os
from pathlib import Path
folder = Path(os.environ["AGENTIC_PERF_HOME"])
fd = os.open(folder / "orchestrator.pid", os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
os.fsync(fd)
raise SystemExit(7)
""",
        invocations=invocations,
    )
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_RESTART_BACKOFF": "30",
            "START_BG_RESTART_BACKOFF_MAX": "30",
            "START_BG_RESTART_STABLE_SECONDS": "60",
            "START_BG_CRASH_LOOP_THRESHOLD": "3",
        }
    )
    process = subprocess.Popen(
        [str(SCRIPT), "__supervise-orchestrator"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_path = home / "orchestrator-supervisor.status"
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text())
                except (OSError, json.JSONDecodeError):
                    status = {}
                if status.get("phase") == "restarting":
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("supervisor did not enter restart backoff")

        assert "status 7" in status["detail"]
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        time.sleep(0.1)

        assert process.returncode == 0
        assert invocations.read_text().splitlines() == ["start"]
        assert (
            "orchestrator supervisor stopped"
            in (home / "logs" / "orchestrator-supervisor.log").read_text()
        )
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)


def test_supervisor_startup_failure_preserves_store_when_cleanup_is_unconfirmed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_ORCH_TIMEOUT": "1",
            "START_BG_ORCH_LEASE_TIMEOUT": "1",
            "START_BG_MAX_START_TIMEOUT": "1",
        }
    )
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
wait_for_orchestrator() {{ return 0; }}
process_start_identity() {{ printf '%s:test\\n' "$1"; }}
start_orchestrator_supervisor() {{ return 1; }}
terminate_process() {{
    printf '%s\\n' "$1" > "$AGENTIC_PERF_HOME/cleanup-pid"
    kill -KILL "$1" 2>/dev/null || true
    return 1
}}
nohup() {{ sleep 30 >/dev/null 2>&1; }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "orchestrator startup did not reach a safe stopped state" in result.stderr
    assert "leaving the state store running for safety" in result.stderr
    assert not (home / "store-stopped").exists()
    assert (home / "cleanup-pid").read_text().strip().isdigit()


def test_existing_orchestrator_startup_failure_preserves_fresh_store(
    tmp_path: Path,
) -> None:
    for failure_mode in ("supervisor", "lease"):
        home = tmp_path / failure_mode
        (home / "logs").mkdir(parents=True)
        (home / "config.json").write_text("{}")
        env = os.environ.copy()
        env.update(
            {
                "AGENTIC_PERF_HOME": str(home),
                "START_BG_ORCH_TIMEOUT": "1",
                "START_BG_ORCH_LEASE_TIMEOUT": "1",
            }
        )
        supervisor_result = "1" if failure_mode == "supervisor" else "0"
        script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
lock_is_held() {{ [ "$1" = "$ORCH_LOCK" ]; }}
orchestrator_pid() {{ printf '4242\\n'; }}
orchestrator_owner_valid() {{ [ "$1" = 4242 ]; }}
start_orchestrator_supervisor() {{ return {supervisor_result}; }}
wait_for_orchestrator() {{ return 1; }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            "orchestrator startup did not reach a safe stopped state" in result.stderr
        )
        assert "leaving the state store running for safety" in result.stderr
        assert not (home / "store-stopped").exists()


def test_stale_supervisor_pid_without_lock_does_not_block_fresh_start(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    (home / "logs" / "orchestrator-supervisor.pid").write_text("1111\n")
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
lock_is_held() {{ return 1; }}
supervisor_pid() {{ printf '1111\\n'; }}
supervisor_owner_valid() {{ return 0; }}
clear_lock_metadata() {{ return 0; }}
process_start_identity() {{ printf '%s:test\\n' "$1"; }}
wait_for_orchestrator() {{ return 0; }}
start_orchestrator_supervisor() {{
    touch "$AGENTIC_PERF_HOME/supervisor-started"
    return 0
}}
nohup() {{ :; }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 0 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Starting orchestrator" in result.stdout
    assert "supervisor is active" not in result.stdout
    assert (home / "supervisor-started").exists()
    assert not (home / "store-stopped").exists()


def test_active_supervisor_startup_failure_preserves_fresh_store(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_ORCH_TIMEOUT": "1",
            "START_BG_ORCH_LEASE_TIMEOUT": "1",
        }
    )
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
lock_is_held() {{ [ "$1" = "$ORCH_SUPERVISOR_LOCK" ]; }}
supervisor_pid() {{ printf '4343\\n'; }}
supervisor_lock_pid() {{ printf '4343\\n'; }}
supervisor_owner_valid() {{ [ "$1" = 4343 ]; }}
sleep() {{ SECONDS=$((SECONDS + 10)); }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "orchestrator startup did not reach a safe stopped state" in result.stderr
    assert "leaving the state store running for safety" in result.stderr
    assert not (home / "store-stopped").exists()


def test_active_supervisor_lock_without_pid_metadata_preserves_fresh_store(
    tmp_path: Path,
) -> None:
    for metadata in ("missing", "stale"):
        home = tmp_path / metadata
        (home / "logs").mkdir(parents=True)
        (home / "config.json").write_text("{}")
        env = os.environ.copy()
        env.update(
            {
                "AGENTIC_PERF_HOME": str(home),
                "START_BG_ORCH_TIMEOUT": "1",
                "START_BG_ORCH_LEASE_TIMEOUT": "1",
            }
        )
        stale_pid = "1111" if metadata == "stale" else ""
        script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
lock_is_held() {{ [ "$1" = "$ORCH_SUPERVISOR_LOCK" ]; }}
supervisor_pid() {{ printf '{stale_pid}\\n'; }}
supervisor_lock_pid() {{ printf '4343\\n'; }}
supervisor_owner_valid() {{ [ "$1" = 4343 ]; }}
sleep() {{ SECONDS=$((SECONDS + 10)); }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Repaired orchestrator supervisor PID metadata" in result.stdout
        assert "Starting orchestrator" not in result.stdout
        assert (
            "orchestrator startup did not reach a safe stopped state" in result.stderr
        )
        assert "leaving the state store running for safety" in result.stderr
        assert not (home / "store-stopped").exists()
        assert (home / "logs" / "orchestrator-supervisor.pid").read_text() == "4343\n"


def test_supervisor_started_orchestrator_lease_failure_preserves_fresh_store(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "config.json").write_text("{}")
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "START_BG_ORCH_TIMEOUT": "1",
            "START_BG_ORCH_LEASE_TIMEOUT": "1",
        }
    )
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
start_store() {{ return 0; }}
stop_store() {{ touch "$AGENTIC_PERF_HOME/store-stopped"; return 0; }}
orch_lock_checks=0
lock_is_held() {{
    if [ "$1" = "$ORCH_SUPERVISOR_LOCK" ]; then return 0; fi
    if [ "$1" = "$ORCH_LOCK" ]; then
        orch_lock_checks=$((orch_lock_checks + 1))
        [ "$orch_lock_checks" -gt 1 ]
        return
    fi
    return 1
}}
supervisor_pid() {{ printf '4343\\n'; }}
supervisor_lock_pid() {{ printf '4343\\n'; }}
supervisor_owner_valid() {{ [ "$1" = 4343 ]; }}
orchestrator_pid() {{ printf '4242\\n'; }}
orchestrator_owner_valid() {{ [ "$1" = 4242 ]; }}
wait_for_orchestrator() {{ return 1; }}
if cmd_start; then status=0; else status=$?; fi
[ "$status" -eq 1 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "supervisor-started orchestrator PID 4242" in result.stderr
    assert "orchestrator startup did not reach a safe stopped state" in result.stderr
    assert "leaving the state store running for safety" in result.stderr
    assert not (home / "store-stopped").exists()


def test_supervisor_stop_and_status_use_lock_owner_over_stale_pid(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "orchestrator-supervisor.pid").write_text("1111\n")
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    script = f"""
source {shlex.quote(str(SCRIPT))} status >/dev/null 2>&1
alive=1
lock_is_held() {{ [ "$1" = "$ORCH_SUPERVISOR_LOCK" ] && [ "$alive" -eq 1 ]; }}
supervisor_pid() {{ printf '1111\\n'; }}
supervisor_lock_pid() {{ printf '4343\\n'; }}
process_start_identity() {{ printf '%s:test\\n' "$1"; }}
supervisor_owner_valid() {{ [ "$1" = 1111 ] || [ "$1" = 4343 ]; }}
process_alive() {{ [ "$1" = 4343 ] && [ "$alive" -eq 1 ]; }}
endpoint_identity() {{ return 1; }}
clear_lock_metadata() {{ return 0; }}
terminate_verified_process() {{
    printf '%s %s\\n' "$1" "$2" >> "$AGENTIC_PERF_HOME/supervisor-signals"
    alive=0
    return 0
}}
cmd_status > "$AGENTIC_PERF_HOME/status-output"
stop_orchestrator_supervisor
status=$?
[ "$status" -eq 0 ]
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    status_output = (home / "status-output").read_text()
    assert "Orchestrator supervisor: RUNNING (PID 4343" in status_output
    assert "1111" not in status_output
    assert (home / "supervisor-signals").read_text().splitlines() == ["4343 4343:test"]


def test_terminate_verified_process_refuses_pid_identity_change(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    env = os.environ.copy()
    env.update({"AGENTIC_PERF_HOME": str(home), "START_BG_STOP_TIMEOUT": "1"})
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" status >/dev/null 2>&1; '
                'terminate_verified_process "$2" "$3"',
                str(SCRIPT),
                str(SCRIPT),
                str(process.pid),
                f"{process.pid}:0",
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

        assert result.returncode != 0
        assert "changed identity; refusing to signal it" in result.stderr
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_replacement_supervisor_acquires_lock_after_forced_backoff_exit(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocations = home / "orchestrator-invocations"
    python = fake_bin / "python3"
    python.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [ "$1" = "-m" ] && [ "$2" = "orchestrator.main" ]; then
                printf 'start\\n' >> {shlex.quote(str(invocations))}
                exit 7
            fi
            exec /usr/bin/python3 "$@"
            """
        )
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_RESTART_BACKOFF": "30",
            "START_BG_RESTART_BACKOFF_MAX": "30",
            "START_BG_RESTART_STABLE_SECONDS": "60",
            "START_BG_CRASH_LOOP_THRESHOLD": "3",
        }
    )
    first = subprocess.Popen(
        [str(SCRIPT), "__supervise-orchestrator"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen | None = None
    status_path = home / "orchestrator-supervisor.status"
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                status = json.loads(status_path.read_text())
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if status.get("phase") == "restarting":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("supervisor did not enter restart backoff")

        time.sleep(0.1)
        first.kill()
        first.wait(timeout=5)
        second = subprocess.Popen(
            [str(SCRIPT), "__supervise-orchestrator"],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if invocations.exists() and len(invocations.read_text().splitlines()) >= 2:
                break
            if second.poll() is not None:
                raise AssertionError("replacement supervisor exited before launching")
            time.sleep(0.05)
        else:
            raise AssertionError("replacement supervisor did not acquire the lock")
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)


def test_stop_uses_supervisor_lock_owner_when_pid_file_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    holder = tmp_path / "supervisor-holder"
    lock_path = home / "orchestrator-supervisor.lock"
    _supervisor_lock_holder(holder, lock_path=lock_path, write_pid=True)
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    process = subprocess.Popen(
        [str(holder), str(lock_path), "1"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = Path(f"{lock_path}.ready")
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()

        result = subprocess.run(
            [str(SCRIPT), "stop"], cwd=REPO, env=env, capture_output=True, text=True
        )
        process.wait(timeout=5)

        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Stopping orchestrator supervisor (PID {process.pid})" in result.stdout
        assert "Services stopped." in result.stdout
        assert lock_path.read_text() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_competing_supervisor_preserves_active_lock_metadata(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    holder = tmp_path / "supervisor-holder"
    lock_path = home / "orchestrator-supervisor.lock"
    _supervisor_lock_holder(holder, lock_path=lock_path, write_pid=True)
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    process = subprocess.Popen(
        [str(holder), str(lock_path), "1"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = Path(f"{lock_path}.ready")
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()

        contender = subprocess.run(
            [str(SCRIPT), "__supervise-orchestrator"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert contender.returncode == 0, contender.stdout + contender.stderr
        assert process.poll() is None
        assert lock_path.read_text() == str(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_stop_refuses_unverifiable_live_supervisor_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    holder = tmp_path / "supervisor-holder"
    lock_path = home / "orchestrator-supervisor.lock"
    _supervisor_lock_holder(holder, lock_path=lock_path, write_pid=False)
    env = os.environ.copy()
    env["AGENTIC_PERF_HOME"] = str(home)
    process = subprocess.Popen(
        [str(holder), str(lock_path), "0"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    store_id = str(uuid.uuid4())
    store_lock = home / "state-store.lock"
    store_holder = tmp_path / "store-holder"
    _holder(store_holder, role="store", lock_path=store_lock, store_id=store_id)
    store = subprocess.Popen(
        [str(store_holder), str(store_lock), store_id],
        cwd=REPO,
        env=env,
    )
    ready = Path(f"{lock_path}.ready")
    try:
        store_lock_inode = _wait_for_store_lock_metadata(
            store_lock, expected_pid=store.pid, store_id=store_id
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()

        result = subprocess.run(
            [str(SCRIPT), "stop"], cwd=REPO, env=env, capture_output=True, text=True
        )

        assert result.returncode != 0
        assert "supervisor lock is held by an unverifiable process" in (
            result.stdout + result.stderr
        )
        assert process.poll() is None
        assert store.poll() is None
        assert store_lock.stat().st_ino == store_lock_inode
        assert "Stopping state store" not in result.stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if store.poll() is None:
            store.kill()
            store.wait(timeout=5)


def test_replacement_supervisor_acquires_lock_when_child_survives(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocations = home / "orchestrator-invocations"
    python = fake_bin / "python3"
    _write_fake_orchestrator_python(
        python,
        """
import fcntl, os, time
from pathlib import Path
folder = Path(os.environ["AGENTIC_PERF_HOME"])
fd = os.open(folder / "orchestrator.pid", os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
os.fsync(fd)
(folder / "orchestrator-ready").write_text(str(os.getpid()))
time.sleep(30)
""",
        invocations=invocations,
    )
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_RESTART_BACKOFF": "30",
            "START_BG_RESTART_BACKOFF_MAX": "30",
            "START_BG_RESTART_STABLE_SECONDS": "60",
            "START_BG_CRASH_LOOP_THRESHOLD": "3",
        }
    )
    first = subprocess.Popen(
        [str(SCRIPT), "__supervise-orchestrator"],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen | None = None
    child_pid = 0
    child_ready = home / "orchestrator-ready"
    status_path = home / "orchestrator-supervisor.status"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                launched_pid = int(child_ready.read_text())
                child_pid = int((home / "orchestrator.pid").read_text())
            except (OSError, ValueError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if launched_pid == child_pid:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("first supervisor did not launch its orchestrator")

        first.kill()
        first.wait(timeout=5)
        os.kill(child_pid, 0)

        second = subprocess.Popen(
            [str(SCRIPT), "__supervise-orchestrator"],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if second.poll() is not None:
                raise AssertionError("replacement supervisor exited")
            try:
                status = json.loads(status_path.read_text())
            except (OSError, json.JSONDecodeError):
                status = {}
            if status.get("pid") == second.pid and status.get("phase") == "monitoring":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("replacement supervisor did not acquire its lock")

        time.sleep(2.5)
        assert invocations.read_text().splitlines() == ["start"]
        assert (home / "orchestrator.pid").read_text() == str(child_pid)
        os.kill(child_pid, 0)
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = (
                        Path(f"/proc/{child_pid}/stat")
                        .read_text()
                        .rsplit(") ", 1)[1]
                        .split()[0]
                    )
                except (OSError, IndexError):
                    break
                if state in {"Z", "X"}:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("surviving orchestrator child did not exit")


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
        lock_inode = _wait_for_store_lock_metadata(
            home / "state-store.lock", expected_pid=process.pid, store_id=store_id
        )

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
        inode = _wait_for_store_lock_metadata(
            lock_path, expected_pid=process.pid, store_id=store_id
        )
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

    def run_launcher(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), command],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )

    first_start = run_launcher("start")
    assert first_start.returncode == 0, first_start.stdout + first_start.stderr
    assert "State store started" in first_start.stdout
    assert "Orchestrator started" in first_start.stdout
    assert int((home / "lease-query-count").read_text()) >= 4

    second_start = run_launcher("start")
    assert second_start.returncode == 0, second_start.stdout + second_start.stderr
    assert "Orchestrator already running" in second_start.stdout

    restarted = run_launcher("restart")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert "Restarting services" in restarted.stdout
    assert "Services stopped" in restarted.stdout

    stopped = run_launcher("stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "Services stopped" in stopped.stdout

    (home / "lease-force-mismatch").touch()
    failed_start = run_launcher("start")
    assert failed_start.returncode != 0, failed_start.stdout + failed_start.stderr
    assert "Cleaning up failed orchestrator startup" in failed_start.stdout
    assert "orchestrator failed to become ready" in failed_start.stderr
    assert "Traceback" not in failed_start.stderr

    status = run_launcher("status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "State store:  STOPPED" in status.stdout
    assert "Orchestrator: STOPPED" in status.stdout
    assert int((home / "lease-query-count").read_text()) >= 4


def test_orchestrator_watchdog_waits_for_lease_acquisition(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    lease_ready = home / "orchestrator-lease-ready"
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\n{_FAKE_LEASE_RESPONSE}"
        'if [[ "$*" == *"/control/orchestrator-lease"* ]]; then\n'
        f"  if [ -f {shlex.quote(str(lease_ready))} ]; then\n"
        '    pid="$(tr -d \'[:space:]\' < "$AGENTIC_PERF_HOME/orchestrator.pid")"\n'
        '    lease_response "$pid"\n'
        "  else\n"
        "    printf '{\"lease\":null}\\n'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    holder = tmp_path / "orchestrator-holder"
    code = textwrap.dedent(
        f"""\
        import fcntl, json, os, time
        from pathlib import Path

        home = Path({str(home)!r})
        fd = os.open(home / "orchestrator.pid", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        (home / "orchestrator.status").write_text(
            json.dumps({{
                "pid": os.getpid(),
                "phase": "initializing",
                "detail": "loading runtime state",
                "updated_at": time.time(),
            }})
        )
        time.sleep(1.4)
        (home / "orchestrator.status").write_text(
            json.dumps({{
                "pid": os.getpid(),
                "phase": "acquiring_lease",
                "detail": "waiting for the previous lease",
                "updated_at": time.time(),
            }})
        )
        time.sleep(1.2)
        (home / "orchestrator-lease-ready").write_text("ready")
        time.sleep(10)
        """
    )
    holder.write_text(
        "#!/usr/bin/env bash\n"
        'exec -a "python3 -m orchestrator.main" '
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}\n"
    )
    holder.chmod(holder.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_ORCH_TIMEOUT": "2",
            "START_BG_ORCH_LEASE_TIMEOUT": "2",
            "START_BG_MAX_START_TIMEOUT": "1",
        }
    )
    process = subprocess.Popen([str(holder)], cwd=REPO, env=env)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                ready = (home / "orchestrator.pid").stat().st_size > 0 and (
                    home / "orchestrator.status"
                ).exists()
            except FileNotFoundError:
                ready = False
            if ready:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("fake orchestrator did not acquire its lock")

        started = time.monotonic()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" status >/dev/null 2>&1; wait_for_orchestrator "$2"',
                str(SCRIPT),
                str(SCRIPT),
                str(process.pid),
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=6,
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 0, result.stdout + result.stderr
        assert elapsed >= 2.4
    finally:
        process.kill()
        process.wait(timeout=5)


def test_default_lease_wait_covers_acquisition_retry_budget(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "orchestrator_lease": {
                    "ttl_seconds": 1,
                    "recovery_delay_seconds": 1,
                    "max_attempts": 2,
                }
            }
        )
    )
    env = os.environ.copy()
    env.pop("START_BG_ORCH_LEASE_TIMEOUT", None)
    env.pop("ORCHESTRATOR_LEASE_TTL_SECONDS", None)
    env.pop("ORCHESTRATOR_LEASE_RECOVERY_DELAY_SECONDS", None)
    env.pop("ORCHESTRATOR_LEASE_MAX_ATTEMPTS", None)
    env["AGENTIC_PERF_HOME"] = str(home)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" status >/dev/null 2>&1; read_orchestrator_lease_wait_timeout',
            str(SCRIPT),
            str(SCRIPT),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert int(result.stdout.strip()) >= 27


def test_lease_wait_budget_handles_large_attempts_without_linear_loop(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"orchestrator_lease": {"max_attempts": 10**400}})
    )
    env = os.environ.copy()
    env.pop("ORCHESTRATOR_LEASE_MAX_ATTEMPTS", None)
    env["AGENTIC_PERF_HOME"] = str(home)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" status >/dev/null 2>&1; read_orchestrator_lease_wait_timeout',
            str(SCRIPT),
            str(SCRIPT),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert 1 <= int(result.stdout.strip()) <= 86_400


def test_status_ignores_nonfinite_lease_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "ORCHESTRATOR_LEASE_TTL_SECONDS": "nan",
            "ORCHESTRATOR_LEASE_RECOVERY_DELAY_SECONDS": "inf",
        }
    )

    result = subprocess.run(
        [str(SCRIPT), "status"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_supervisor_handoff_waits_for_leader_lease(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    lease_ready = home / "orchestrator-lease-ready"
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\n{_FAKE_LEASE_RESPONSE}"
        'if [[ "$*" == *"/control/orchestrator-lease"* ]]; then\n'
        f"  if [ -f {shlex.quote(str(lease_ready))} ]; then\n"
        '    pid="$(tr -d \'[:space:]\' < "$AGENTIC_PERF_HOME/orchestrator.pid")"\n'
        '    lease_response "$pid"\n'
        "  else\n"
        "    printf '{\"lease\":null}\\n'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    python = fake_bin / "python3"
    _write_fake_orchestrator_python(
        python,
        f"""
import fcntl, json, os, time
from pathlib import Path
home = Path({str(home)!r})
fd = os.open(home / "orchestrator.pid", os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
os.fsync(fd)
(home / "orchestrator.status").write_text(json.dumps({{
    "pid": os.getpid(),
    "phase": "acquiring_lease",
    "detail": "waiting for the previous lease",
    "updated_at": time.time(),
}}))
time.sleep(0.8)
(home / "orchestrator-lease-ready").write_text("ready")
time.sleep(5)
""",
    )

    supervisor_holder = tmp_path / "supervisor-holder"
    supervisor_lock = home / "orchestrator-supervisor.lock"
    _supervisor_lock_holder(
        supervisor_holder,
        lock_path=supervisor_lock,
        write_pid=True,
    )
    env = os.environ.copy()
    env.update(
        {
            "AGENTIC_PERF_HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "START_BG_ORCH_TIMEOUT": "2",
            "START_BG_ORCH_LEASE_TIMEOUT": "2",
        }
    )
    supervisor = subprocess.Popen(
        [str(supervisor_holder), str(supervisor_lock), "1"],
        cwd=REPO,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        ready = Path(f"{supervisor_lock}.ready")
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()
        (home / "logs" / "orchestrator-supervisor.pid").write_text(
            f"{supervisor.pid}\n"
        )

        started = time.monotonic()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" status >/dev/null 2>&1; '
                "(sleep 0.4; python3 -m orchestrator.main) & child=$!; "
                'start_orchestrator; status=$?; kill "$child" 2>/dev/null || true; '
                'wait "$child" 2>/dev/null || true; exit "$status"',
                str(SCRIPT),
                str(SCRIPT),
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 2, result.stdout + result.stderr
        assert "Orchestrator started by supervisor" in result.stdout
        assert elapsed >= 1.0
    finally:
        supervisor.kill()
        supervisor.wait(timeout=5)


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


def test_restart_waits_for_supervisor_lock_ownership(tmp_path: Path) -> None:
    """#1399: restart must not report success before the replacement
    supervisor owns its lock — a stop issued in that window would skip the
    supervisor, and the late supervisor would relaunch an orchestrator the
    user believes was stopped."""
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
    # A supervisor that exists immediately but acquires its lock 2s later —
    # the exact race window from #1399.
    slow_supervisor = tmp_path / "slow-supervisor"
    slow_supervisor.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 2\n"
        'exec 9>>"$AGENTIC_PERF_HOME/orchestrator-supervisor.lock"\n'
        "flock -n 9 || exit 0\n"
        'printf \'%s\\n\' "$$" > "$AGENTIC_PERF_HOME/orchestrator-supervisor.lock"\n'
        'exec -a "bash start-bg.sh __supervise-orchestrator" '
        "sleep 600\n"
    )
    slow_supervisor.chmod(slow_supervisor.stat().st_mode | stat.S_IXUSR)
    nohup = fake_bin / "nohup"
    nohup.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = bash ] && [ "$3" = __supervise-orchestrator ]; then\n'
        f"    exec {slow_supervisor}\n"
        'elif [ "$1" = python3 ] && [ "$2" = -m ] && [ "$3" = uvicorn ]; then\n'
        f'    exec {store_holder} "$AGENTIC_PERF_HOME/state-store.lock" "{store_id}"\n'
        'elif [ "$1" = python3 ] && [ "$2" = -m ] && [ "$3" = orchestrator.main ]; then\n'
        f'    exec {orch_holder} "$AGENTIC_PERF_HOME/orchestrator.pid"\n'
        "fi\n"
        'exec "$@"\n'
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

    def run_launcher(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), command],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    first_start = run_launcher("start")
    assert first_start.returncode == 0, first_start.stdout + first_start.stderr
    assert "Orchestrator started" in first_start.stdout

    # The supervisor exists (process matches) but has NOT acquired its lock
    # yet; restart must block until it does.
    restarted = run_launcher("restart")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert "Services stopped" in restarted.stdout
    # By the time restart reports success, the new supervisor must own its
    # lock, so an immediate stop reaches it.
    stopped = run_launcher("stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "Services stopped" in stopped.stdout

    # Wait past the delayed-lock window: no supervisor may come back and
    # relaunch an orchestrator the user believes was stopped.
    time.sleep(3)
    status = run_launcher("status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "Orchestrator: STOPPED" in status.stdout, status.stdout + status.stderr
