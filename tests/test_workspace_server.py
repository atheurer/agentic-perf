from __future__ import annotations

import json

import pytest

import paths
from agents.workspace import server as ws_server
from providers.workspace.manager import WorkspaceManager


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    manager = WorkspaceManager(ticket_id="PERF-TEST-MCP")
    # Reset global manager in ws_server
    monkeypatch.setattr(ws_server, "_manager", manager)
    return manager


async def test_mcp_jq_query(ws_env):
    data = {
        "summary": {"pass": True, "score": 98.6},
        "samples": [10, 20, 30, 40],
    }
    ws_env.save_file("run_summary.json", json.dumps(data))

    raw_resp = await ws_server.jq_query(
        file_ref="workspace://run_summary.json", filter=".summary.score"
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["result"] == 98.6


async def test_mcp_grep_file(ws_env):
    ws_env.save_file("dmesg.txt", "eth0: link up\neth0: NIC reset\neth1: link up\n")

    raw_resp = await ws_server.grep_file(
        file_ref="workspace://dmesg.txt", pattern="NIC reset"
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["total_matches"] == 1
    assert "NIC reset" in resp["lines"][0]["content"]


async def test_mcp_read_file_slice(ws_env):
    ws_env.save_file("test.txt", "Line 1\nLine 2\nLine 3\nLine 4\n")

    raw_resp = await ws_server.read_file_slice(
        file_ref="workspace://test.txt", start_line=2, max_lines=2
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["lines_returned"] == 2
    assert "Line 2\nLine 3\n" in resp["content"]


async def test_mcp_list_workspace_files(ws_env):
    ws_env.save_file("file1.json", "{}")
    ws_env.save_file("file2.txt", "abc")

    raw_resp = await ws_server.list_workspace_files()
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["count"] == 2
    files = {f["filename"] for f in resp["files"]}
    assert files == {"file1.json", "file2.txt"}
