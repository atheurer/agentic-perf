"""Integration coverage for the state-store persistence-root process lock."""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from state_store.process_lock import PersistenceRootLock, PersistenceRootLockedError


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(home: Path, port: int, *, token: str | None = None) -> subprocess.Popen[str]:
    env = os.environ | {"AGENTIC_PERF_HOME": str(home), "STORE_PORT": str(port)}
    if token is not None:
        env["AGENTIC_PERF_API_TOKEN"] = token
    return subprocess.Popen(
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
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(process.stdout.read() if process.stdout else "server exited")
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
        time.sleep(0.05)
    pytest.fail("state store did not become ready")


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_in_process_initialization_holds_persistence_lock() -> None:
    """Test helpers must opt into a lock-backed writable application."""
    from state_store.main import create_app

    app = create_app(initialize_immediately=True)
    try:
        assert app.state.process_lock.fd is not None
        assert app.state.runtime_initialized is True
    finally:
        app.router.on_shutdown[0]()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_cannot_reuse_parent_lock_or_runtime(
    tmp_path, monkeypatch
) -> None:
    """A fork must use a fresh lock FD, never the parent's open description."""
    import state_store.main as main

    monkeypatch.setattr(main, "AGENTIC_PERF_HOME", tmp_path)
    monkeypatch.setattr(main, "TRACE_DB_PATH", tmp_path / "trace.db")
    parent_app = main.create_app(initialize_immediately=True)
    read_fd, write_fd = os.pipe()
    try:
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                # Reusing the inherited app must discard inherited backends and
                # attempt a new lock, which remains held by the parent.
                main._start_runtime(parent_app, 12345)
            except Exception as exc:
                os.write(write_fd, type(exc).__name__.encode())
            else:
                os.write(write_fd, b"unexpected-success")
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 256)
        os.close(read_fd)
        assert os.waitpid(child, 0)[1] == 0
        assert result == b"PersistenceRootLockedError"
    finally:
        main._close_runtime(parent_app)

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            child_app = main.create_app(initialize_immediately=True)
            assert child_app.state.process_lock.fd is not None
            main._close_runtime(child_app)
            os.write(write_fd, b"acquired")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 256)
    os.close(read_fd)
    assert os.waitpid(child, 0)[1] == 0
    assert result == b"acquired"


def test_second_process_same_home_fails_before_writes(tmp_path: Path) -> None:
    first_port = _port()
    first = _start(tmp_path, first_port)
    _wait_ready(first_port, first)
    try:
        before = {
            p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")
        }
        second = _start(tmp_path, _port())
        assert second.wait(timeout=10) != 0
        output = second.stdout.read() if second.stdout else ""
        assert "persistence root is locked" in output
        # A failed contender must not construct any writable backend.
        after = {
            p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")
        }
        assert after == before
    finally:
        _stop(first)


def test_stale_pid_file_does_not_block_and_store_id_survives_restart(
    tmp_path: Path,
) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "state-store.pid").write_text("999999\n")
    first_port = _port()
    first = _start(tmp_path, first_port)
    _wait_ready(first_port, first)
    first_id = (tmp_path / "state-store.id").read_text().strip()
    _stop(first)
    second_port = _port()
    second = _start(tmp_path, second_port)
    _wait_ready(second_port, second)
    try:
        assert (tmp_path / "state-store.id").read_text().strip() == first_id
    finally:
        _stop(second)


def test_authenticated_diagnostics_expose_stable_store_identity(tmp_path: Path) -> None:
    port = _port()
    token = "diagnostic-test-token"
    server = _start(tmp_path, port, token=token)
    _wait_ready(port, server)
    try:
        health = httpx.get(f"http://127.0.0.1:{port}/api/v1/health")
        assert "store_id" not in health.json()
        denied = httpx.get(f"http://127.0.0.1:{port}/api/v1/diagnostics")
        assert denied.status_code == 401
        response = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/diagnostics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["store_id"] == (tmp_path / "state-store.id").read_text().strip()
        assert data["process_session_id"] == data["process"]["session_id"]
        assert data["persistence_root_fingerprint"]
    finally:
        _stop(server)


def test_held_lock_with_malformed_metadata_blocks_safely(tmp_path: Path) -> None:
    path = tmp_path / "state-store.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.write(fd, b"not-json")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(
            PersistenceRootLockedError, match="holder metadata: unavailable"
        ):
            PersistenceRootLock(tmp_path, _port()).acquire()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_empty_store_id_is_recovered_atomically_while_locked(tmp_path: Path) -> None:
    (tmp_path / "state-store.id").write_text("")
    lock = PersistenceRootLock(tmp_path, _port())
    lock.acquire()
    try:
        assert lock.store_id is not None
        assert uuid.UUID(lock.store_id)
        assert (tmp_path / "state-store.id").read_text().strip() == lock.store_id
        assert not list(tmp_path.glob(".state-store.id.*.tmp"))
    finally:
        lock.release()


def test_failed_setup_releases_lock_for_a_retry(tmp_path: Path, monkeypatch) -> None:
    import state_store.process_lock as process_lock

    monkeypatch.setattr(
        process_lock,
        "ensure_store_id",
        lambda _path: (_ for _ in ()).throw(OSError("disk failure")),
    )
    failed = PersistenceRootLock(tmp_path, _port())
    with pytest.raises(OSError, match="disk failure"):
        failed.acquire()
    assert failed.fd is None

    monkeypatch.undo()
    retry = PersistenceRootLock(tmp_path, _port())
    retry.acquire()
    retry.release()


def test_abrupt_exit_releases_kernel_lock(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path; from state_store.process_lock import PersistenceRootLock; "
        f"PersistenceRootLock(Path({str(tmp_path)!r}), 12345).acquire(); os._exit(0)"
    )
    result = subprocess.run([sys.executable, "-c", "import os; " + script], check=False)
    assert result.returncode == 0
    lock = PersistenceRootLock(tmp_path, _port())
    lock.acquire()
    lock.release()
