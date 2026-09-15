"""Deterministic processes used by orchestration safety integration tests."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from providers.llm.base import LLMProvider, LLMResponse, ToolCall, ToolDefinition


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
    log_path = home / "state-store.log"
    log = log_path.open("w+", encoding="utf-8")
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
    process._fault_log_path = log_path  # type: ignore[attr-defined]
    log.close()
    wait_for_store(port, process)
    return process


def wait_for_log(
    process: subprocess.Popen[str], needle: str, *, timeout: float = 15
) -> None:
    """Wait for a production process to emit its readiness event.

    This is deliberately a predicate wait, rather than a fixed startup sleep:
    the log line is emitted only after ``poll_loop`` has acquired the real
    leader lease and constructed its dispatcher.
    """
    deadline = time.monotonic() + timeout
    log_path = Path(getattr(process, "_fault_log_path"))
    while time.monotonic() < deadline:
        output = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if needle in output:
            return
        if process.poll() is not None:
            raise AssertionError(f"process exited before {needle!r}: {output[-2000:]}")
        time.sleep(0.02)
    output = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    raise AssertionError(f"timed out waiting for {needle!r}: {output[-2000:]}")


def start_orchestrator(
    home: Path, store_url: str, *, instance_name: str
) -> subprocess.Popen[str]:
    """Start the real ``orchestrator.main`` with an isolated runtime home."""
    home.mkdir(parents=True, exist_ok=True)
    # ``poll_loop`` creates a cache for every known harness before it starts.
    # Empty local git repositories make those intentional no-network failures
    # immediate, while preserving the production startup path.
    for name in (
        "crucible-examples",
        "zathras",
        "kube-burner",
        "k8s-netperf",
        "benchmark-runner",
        "clusterbuster",
        "vstorm",
        "ioscale",
        "forge",
        "boot-time-analysis-scripts",
    ):
        (home / "skill-cache" / name / ".git").mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "instance_name": instance_name,
                "state_store": {"url": store_url},
                "llm": {"provider": "mock"},
                "poll_interval": 0.05,
                "orchestrator_lease": {"ttl_seconds": 30, "renew_interval": 1},
                "max_concurrent_agents": 1,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ | {
        "AGENTIC_PERF_HOME": str(home),
        "STATE_STORE_URL": store_url,
        "AGENTIC_PERF_API_TOKEN": "fault-token",
        "PYTHONUNBUFFERED": "1",
    }
    log_path = home / "orchestrator.log"
    log = log_path.open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "orchestrator.main"],
        cwd=Path(__file__).parents[2],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process._fault_log_path = log_path  # type: ignore[attr-defined]
    log.close()
    return process


class ScriptedMockLLM(LLMProvider):
    """A deterministic transcript provider for real agent/MCP integration tests.

    Transcript arguments may refer to values generated earlier in the workflow
    with ``${name}`` (for example ``${validation_id}`` or
    ``${approval_request_id}``).  Tests bind those values after observing the
    real MCP result, rather than predicting UUIDs produced by the service.
    The provider deliberately lives in ``tests``: production mock behavior
    remains simple and cannot accidentally acquire test-only interpolation.
    """

    def __init__(self, transcript: list[dict[str, Any]]) -> None:
        self._transcript = list(transcript)
        self._values: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def bind(self, **values: Any) -> None:
        """Expose runtime service identifiers to later transcript entries."""
        self._values.update(values)

    def bind_json(self, result: str | dict[str, Any], *names: str) -> dict[str, Any]:
        """Capture named IDs from a real MCP JSON response and return it."""
        decoded = json.loads(result) if isinstance(result, str) else result
        if not isinstance(decoded, dict):
            raise AssertionError(f"expected object result, got {decoded!r}")
        missing = [name for name in names if name not in decoded]
        if missing:
            raise AssertionError(f"MCP response did not contain {missing}: {decoded}")
        self.bind(**{name: decoded[name] for name in names})
        return decoded

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            if name not in self._values:
                raise AssertionError(
                    f"transcript references unbound runtime value: {name}"
                )
            return self._values[name]
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        return value

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        del system_prompt, tools, max_tokens, timeout
        if not self._transcript:
            return LLMResponse(text="", stop_reason="end_turn")
        step = self._transcript.pop(0)
        resolved = self._resolve(step)
        self.calls.append({"step": resolved, "messages": messages})
        calls = [
            ToolCall(
                id=str(call.get("id", f"tc-{len(self.calls)}")),
                name=str(call["name"]),
                input=dict(call.get("input", {})),
            )
            for call in resolved.get("tool_calls", [])
        ]
        return LLMResponse(
            text=resolved.get("text"),
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            raw_content=[],
        )


class FakeControllerCommand:
    """A process-visible external controller/SSH substitute with a durable ledger.

    ``command`` is intended for a test subprocess ``PATH``.  Each invocation
    obtains the same advisory lock used by :class:`FakeBenchmarkController`,
    records argv durably, and can be paused by a named file barrier.  This lets
    a test crash a real main or MCP subprocess exactly after an external
    request is accepted without relying on wall-clock sleeps.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.controller = FakeBenchmarkController(root)
        self.bin_dir = root / "bin"
        self.command = self.bin_dir / "fault-controller"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.command.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "sys.path.insert(0, os.environ['FAULT_PROJECT_ROOT'])\n"
            "from tests.integration.orchestration_faults import fake_controller_command\n"
            "fake_controller_command()\n",
            encoding="utf-8",
        )
        self.command.chmod(0o755)
        # These names let a transcript or a subprocess exercise the same
        # durable ledger through the command shape it normally sees.
        for alias in ("ssh", "crucible"):
            alias_path = self.bin_dir / alias
            if not alias_path.exists():
                alias_path.symlink_to(self.command.name)

    def environment(self) -> dict[str, str]:
        return {
            "FAULT_CONTROLLER_ROOT": str(self.root),
            "FAULT_PROJECT_ROOT": str(Path(__file__).parents[2]),
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        }

    def arm_barrier(self, name: str) -> Path:
        return self.controller.arm_barrier(name)

    def records(self) -> list[dict[str, Any]]:
        return self.controller.records()


def fake_controller_command() -> None:
    """Entry point installed by :class:`FakeControllerCommand` for subprocesses."""
    root = Path(os.environ["FAULT_CONTROLLER_ROOT"])
    controller = FakeBenchmarkController(root)
    operation = os.environ.get("FAULT_CONTROLLER_OPERATION", "launch")
    barrier_name = os.environ.get("FAULT_CONTROLLER_BARRIER", "")
    if barrier_name:
        controller.wait_for_release(root / f"barrier.{barrier_name}")
    # The argv itself is the durable, process-visible external request identity.
    digest = json.dumps(sys.argv[1:], sort_keys=True, separators=(",", ":"))
    accepted = controller.launch(
        intent_id=f"{operation}:{digest}",
        approval_id=os.environ.get("FAULT_APPROVAL_ID", "test-approval"),
        session_id=os.environ.get("FAULT_SESSION_ID", "test-session"),
    )
    print(
        json.dumps(
            {"status": "launched" if accepted else "replayed", "operation": operation}
        ),
        flush=True,
    )


@dataclass
class FaultHarness:
    """Owns isolated runtime homes sharing one real local state-store service.

    Scenarios must use predicate/file-barrier waits instead of fixed sleeps and
    must call :meth:`close` (or use the context manager) so failures retain
    bounded diagnostics while processes cannot leak into later tests.
    """

    root: Path
    token: str = "fault-token"
    port: int = field(default_factory=free_port)
    store: subprocess.Popen[str] | None = None
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    controller: FakeControllerCommand = field(init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.controller = FakeControllerCommand(self.root / "controller")

    @property
    def store_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def start(self) -> None:
        if self.store is not None:
            raise RuntimeError("fault harness state store is already running")
        self.store = start_store(self.root / "shared-store", self.port)
        self.processes.append(self.store)

    def start_main(self, name: str) -> subprocess.Popen[str]:
        if self.store is None:
            raise RuntimeError("start the shared state store before a main process")
        process = start_orchestrator(
            self.root / f"runtime-{name}", self.store_url, instance_name=name
        )
        self.processes.append(process)
        return process

    def barrier(self, name: str) -> Path:
        return self.controller.arm_barrier(name)

    def release(self, barrier: Path) -> None:
        self.controller.controller.release(barrier)

    def restart_store(self) -> None:
        if self.store is None:
            raise RuntimeError("state store is not running")
        stop_process(self.store)
        self.store = start_store(self.root / "shared-store", self.port)
        self.processes.append(self.store)

    def crash(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    def evidence(self) -> str:
        return diagnostics(self.processes)

    def close(self) -> None:
        for process in reversed(self.processes):
            stop_process(process)

    def __enter__(self) -> "FaultHarness":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
        log_path = getattr(process, "_fault_log_path", None)
        if log_path:
            try:
                output = Path(log_path).read_text(encoding="utf-8")[-1500:]
            except OSError:
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
