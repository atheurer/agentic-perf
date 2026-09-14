"""Deterministic processes used by orchestration safety integration tests."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_store(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = "<log unavailable>"
            if process.stdout:
                output = process.stdout.read(4096)
            raise AssertionError(f"state store exited: {output[-2000:]}")
        try:
            if (
                httpx.get(
                    f"http://127.0.0.1:{port}/api/v1/health", timeout=0.2
                ).status_code
                == 200
            ):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.02)
    raise AssertionError("state store did not become ready")


def start_store(home: Path, port: int) -> subprocess.Popen[str]:
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "AGENTIC_PERF_HOME": str(home),
        "STORE_PORT": str(port),
        "AGENTIC_PERF_API_TOKEN": "fault-token",
    }
    log = (home / "state-store.log").open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "state_store.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process._fault_log = log  # type: ignore[attr-defined]
    log.close()
    wait_for_store(port, process)
    return process


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def diagnostics(processes: list[subprocess.Popen[str]]) -> str:
    """Return bounded process evidence suitable for a failed assertion."""
    chunks: list[str] = []
    for process in processes:
        output = ""
        log_path = getattr(process, "_fault_log", None)
        if log_path:
            try:
                output = Path(log_path.name).read_text(encoding="utf-8")[-1500:]
            except (OSError, ValueError):
                output = "<log unavailable>"
        elif process.stdout:
            try:
                output = process.stdout.read(4096)
            except (OSError, ValueError):
                output = "<log unavailable>"
        chunks.append(f"pid={process.pid} rc={process.poll()} log={output[-1500:]}")
    return "\n".join(chunks)


def lease_worker() -> None:
    """Subprocess entry point for a real leader-lease contender."""
    import asyncio
    import uuid

    from orchestrator.leader_lease import LeaderLeaseClient
    from providers.tracing import bind_trace_context, new_trace_context

    async def run() -> None:
        url = os.environ["FAULT_STORE_URL"]
        barrier = Path(os.environ["FAULT_BARRIER"])
        ready = barrier.with_name(f"{barrier.name}.{os.getpid()}.ready")
        ready.touch()
        while not barrier.exists():
            await asyncio.sleep(0.01)
        client = LeaderLeaseClient(
            url,
            instance_name=os.environ["FAULT_INSTANCE"],
            ttl_seconds=float(os.environ.get("FAULT_TTL", "30")),
            session_id=uuid.UUID(os.environ["FAULT_SESSION"]),
        )
        try:
            os.environ["STATE_STORE_URL"] = url
            os.environ["AGENTIC_PERF_API_TOKEN"] = "fault-token"
            bind_trace_context(
                new_trace_context(ticket_id="control", agent_id="orchestrator")
            )
            lease = await client.acquire()
            Path(os.environ["FAULT_RESULT"]).write_text(
                json.dumps({"status": "winner", "lease": lease})
            )
            # Keep the process alive while the test examines the lease.
            while not Path(os.environ["FAULT_STOP"]).exists():
                await asyncio.sleep(0.01)
        except Exception as exc:  # subprocess result is the assertion surface
            Path(os.environ["FAULT_RESULT"]).write_text(
                json.dumps({"status": "loser", "error": str(exc)})
            )

    asyncio.run(run())


class FakeBenchmarkController:
    """Durable controller ledger with file barriers, not timing sleeps."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ledger = root / "controller.jsonl"
        self.lock = root / "controller.lock"
        self.root.mkdir(parents=True, exist_ok=True)

    def _append(self, operation: str, **identifiers: str) -> None:
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": operation, **identifiers}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def validate(self, validation_id: str, runfile_digest: str) -> None:
        self._append(
            "validate", validation_id=validation_id, runfile_digest=runfile_digest
        )

    def arm_barrier(self, name: str) -> Path:
        """Create a release-controlled barrier for the next caller."""
        barrier = self.root / f"barrier.{name}"
        barrier.unlink(missing_ok=True)
        return barrier

    def release(self, barrier: Path) -> None:
        barrier.touch()

    def wait_for_release(self, barrier: Path, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not barrier.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not barrier.exists():
            raise TimeoutError(f"controller barrier was not released: {barrier}")

    def launch(self, intent_id: str, approval_id: str, session_id: str) -> bool:
        # This is deliberately the same atomic shape required of an external
        # controller: serialize the check and durable record, so two reconnects
        # cannot both observe an empty ledger and launch.
        with self.lock.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records = self.records()
                if any(
                    record.get("operation") == "launch"
                    and record.get("intent_id") == intent_id
                    for record in records
                ):
                    return False
                self._append(
                    "launch",
                    intent_id=intent_id,
                    approval_id=approval_id,
                    session_id=session_id,
                )
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def records(self) -> list[dict[str, Any]]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]
