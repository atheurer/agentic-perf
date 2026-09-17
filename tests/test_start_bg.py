"""Lifecycle tests for scripts/start-bg.sh."""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "start-bg.sh"


def _holder(path: Path, *, role: str, lock_path: Path, store_id: str) -> None:
    if role == "store":
        argv0 = "python3 -m uvicorn state_store.main:app"
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
        f"#!/usr/bin/env bash\n"
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
