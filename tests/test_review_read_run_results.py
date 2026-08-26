"""Unit tests for read_run_results in agents/review/server.py."""

from __future__ import annotations

import json

import pytest

import agents.review.server as review_server
from tests.conftest import MockSSHExecutor, SSHResult


@pytest.fixture(autouse=True)
def patch_review_server(monkeypatch):
    """Wire a MockSSHExecutor into the review server module for each test."""
    mock = MockSSHExecutor()
    monkeypatch.setattr(review_server, "_ssh", mock)
    monkeypatch.setattr(review_server, "_initialized", True)
    return mock


@pytest.mark.asyncio
async def test_read_run_results_listing_mode(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "find /var/lib/crucible/run/uperf-run-123": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123/config/tool-params.json\n/var/lib/crucible/run/uperf-run-123/result.json\n",
        ),
        "stat --printf='%s %n\\n'": SSHResult(
            exit_code=0,
            stdout="150 /var/lib/crucible/run/uperf-run-123/config/tool-params.json\n500 /var/lib/crucible/run/uperf-run-123/result.json\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="",
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["run_id"] == "run-123"
    assert data["run_dir"] == "/var/lib/crucible/run/uperf-run-123"
    assert len(data["files"]) == 2
    assert (
        data["files"][0]["path"]
        == "/var/lib/crucible/run/uperf-run-123/config/tool-params.json"
    )
    assert data["files"][0]["size_bytes"] == 150


@pytest.mark.asyncio
async def test_read_run_results_relative_path_success(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f /var/lib/crucible/run/uperf-run-123/config/tool-params.json": SSHResult(
            exit_code=0,
            stdout="",
        ),
        "head -c 4000 /var/lib/crucible/run/uperf-run-123/config/tool-params.json": SSHResult(
            exit_code=0,
            stdout='{"sample": 1}',
        ),
        "stat --printf='%s' /var/lib/crucible/run/uperf-run-123/config/tool-params.json": SSHResult(
            exit_code=0,
            stdout="13\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="config/tool-params.json",
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["run_id"] == "run-123"
    assert data["file_path"] == "config/tool-params.json"
    assert (
        data["resolved_path"]
        == "/var/lib/crucible/run/uperf-run-123/config/tool-params.json"
    )
    assert data["content"] == '{"sample": 1}'
    assert data["bytes_read"] == 13
    assert data["file_size"] == 13
    assert data["is_compressed"] is False


@pytest.mark.asyncio
async def test_read_run_results_relative_path_compressed(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f /var/lib/crucible/run/uperf-run-123/turbostat.txt.xz": SSHResult(
            exit_code=0,
            stdout="",
        ),
        "xzcat /var/lib/crucible/run/uperf-run-123/turbostat.txt.xz 2>/dev/null | head -c 4000": SSHResult(
            exit_code=0,
            stdout="CPU stats output line",
        ),
        "stat --printf='%s' /var/lib/crucible/run/uperf-run-123/turbostat.txt.xz": SSHResult(
            exit_code=0,
            stdout="250\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="turbostat.txt.xz",
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["is_compressed"] is True
    assert (
        data["resolved_path"] == "/var/lib/crucible/run/uperf-run-123/turbostat.txt.xz"
    )
    assert data["content"] == "CPU stats output line"


@pytest.mark.asyncio
async def test_read_run_results_absolute_path_success(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f /var/lib/crucible/run/uperf-run-123/result.json": SSHResult(
            exit_code=0,
            stdout="",
        ),
        "head -c 4000 /var/lib/crucible/run/uperf-run-123/result.json": SSHResult(
            exit_code=0,
            stdout='{"result": "pass"}',
        ),
        "stat --printf='%s' /var/lib/crucible/run/uperf-run-123/result.json": SSHResult(
            exit_code=0,
            stdout="18\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="/var/lib/crucible/run/uperf-run-123/result.json",
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["resolved_path"] == "/var/lib/crucible/run/uperf-run-123/result.json"
    assert data["content"] == '{"result": "pass"}'


@pytest.mark.asyncio
async def test_read_run_results_relative_path_traversal_denied(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="../../etc/shadow",
    )
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "Access denied" in data["message"]
    # Ensure no test -f or reading was attempted
    assert not any("test -f" in c["command"] for c in patch_review_server.calls)
    assert not any("head -c" in c["command"] for c in patch_review_server.calls)


@pytest.mark.asyncio
async def test_read_run_results_absolute_path_traversal_denied(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="/etc/passwd",
    )
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "Access denied" in data["message"]
    assert not any("test -f" in c["command"] for c in patch_review_server.calls)
    assert not any("head -c" in c["command"] for c in patch_review_server.calls)


@pytest.mark.asyncio
async def test_read_run_results_relative_file_not_found(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f /var/lib/crucible/run/uperf-run-123/config/nonexistent.json": SSHResult(
            exit_code=1,
            stdout="",
            stderr="",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="config/nonexistent.json",
    )
    data = json.loads(raw)
    assert data["status"] == "not_found"
    assert data["run_id"] == "run-123"
    assert data["run_dir"] == "/var/lib/crucible/run/uperf-run-123"
    assert data["file_path"] == "config/nonexistent.json"
    assert (
        data["resolved_path"]
        == "/var/lib/crucible/run/uperf-run-123/config/nonexistent.json"
    )
    assert "not found in /var/lib/crucible/run/uperf-run-123" in data["message"]


@pytest.mark.asyncio
async def test_read_run_results_absolute_file_not_found(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f /var/lib/crucible/run/uperf-run-123/nonexistent.json": SSHResult(
            exit_code=1,
            stdout="",
            stderr="",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="/var/lib/crucible/run/uperf-run-123/nonexistent.json",
    )
    data = json.loads(raw)
    assert data["status"] == "not_found"
    assert data["run_id"] == "run-123"
    assert (
        "File not found: /var/lib/crucible/run/uperf-run-123/nonexistent.json"
        in data["message"]
    )


@pytest.mark.asyncio
async def test_read_run_results_run_dir_not_found(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*unknown*": SSHResult(
            exit_code=1,
            stdout="",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="unknown",
        file_path="config/tool-params.json",
    )
    data = json.loads(raw)
    assert data["status"] == "not_found"
    assert "No run directory found" in data["message"]


@pytest.mark.asyncio
async def test_read_run_results_path_with_single_quote_quoted(patch_review_server):
    patch_review_server._results = {
        "ls -d /var/lib/crucible/run/*run-123*": SSHResult(
            exit_code=0,
            stdout="/var/lib/crucible/run/uperf-run-123\n",
        ),
        "test -f '/var/lib/crucible/run/uperf-run-123/file'\"'\"'name.json'": SSHResult(
            exit_code=0,
            stdout="",
        ),
        "head -c 4000 '/var/lib/crucible/run/uperf-run-123/file'\"'\"'name.json'": SSHResult(
            exit_code=0,
            stdout='{"safe": true}',
        ),
        "stat --printf='%s' '/var/lib/crucible/run/uperf-run-123/file'\"'\"'name.json'": SSHResult(
            exit_code=0,
            stdout="15\n",
        ),
    }

    raw = await review_server.read_run_results(
        controller="10.0.0.1",
        run_id="run-123",
        file_path="file'name.json",
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["content"] == '{"safe": true}'
