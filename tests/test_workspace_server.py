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


async def test_mcp_jq_file_from_workspace(ws_env):
    data = {
        "summary": {"pass": True, "score": 98.6},
        "samples": [10, 20, 30, 40],
    }
    ws_env.save_file("run_summary.json", json.dumps(data))

    raw_resp = await ws_server.jq_file_from_workspace(
        file_ref="workspace://run_summary.json", filter=".summary.score"
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["result"] == 98.6


async def test_mcp_grep_file_from_workspace(ws_env):
    ws_env.save_file("dmesg.txt", "eth0: link up\neth0: NIC reset\neth1: link up\n")

    raw_resp = await ws_server.grep_file_from_workspace(
        file_ref="workspace://dmesg.txt", pattern="NIC reset"
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["total_matches"] == 1
    assert "NIC reset" in resp["lines"][0]["content"]


async def test_mcp_read_file_from_workspace(ws_env):
    ws_env.save_file("test.txt", "Line 1\nLine 2\nLine 3\nLine 4\n")

    raw_resp = await ws_server.read_file_from_workspace(
        file_ref="workspace://test.txt", start_line=2, max_lines=2
    )
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["lines_returned"] == 2
    assert "Line 2\nLine 3\n" in resp["content"]


async def test_mcp_list_files_from_workspace(ws_env):
    ws_env.save_file("file1.json", "{}")
    ws_env.save_file("file2.txt", "abc")

    raw_resp = await ws_server.list_files_from_workspace()
    resp = json.loads(raw_resp)
    assert resp["status"] == "ok"
    assert resp["count"] == 2
    files = {f["filename"] for f in resp["files"]}
    assert files == {"file1.json", "file2.txt"}


async def test_mcp_read_and_search_indexed_documents(ws_env):
    snapshot = ws_env.save_source_snapshot(
        "github",
        {"repository": "example/bench-perftest", "commit": "abc123"},
        {"README.md": "Perftest requires matching client and server IDs.\n"},
        benchmark="perftest",
    )
    workspace_ref = snapshot["files"]["README.md"]
    ws_env.save_effective_context(
        {
            "effective_source": "github",
            "workspace_refs": [workspace_ref],
        }
    )
    ws_env.index_context_documents(
        [
            {
                "ref": "benchmark/perftest/README.md",
                "uri": "crucible://benchmark/perftest/README.md",
                "namespace": "benchmark/perftest",
                "source": "github",
                "authority": "effective",
                "workspace_ref": workspace_ref,
                "provenance": {"commit": "abc123"},
            }
        ]
    )

    read_resp = json.loads(
        await ws_server.read_document_from_workspace("benchmark/perftest/README.md")
    )
    assert read_resp["status"] == "ok"
    assert "matching client and server IDs" in read_resp["content"]
    assert read_resp["provenance"]["commit"] == "abc123"

    search_resp = json.loads(
        await ws_server.search_documents_from_workspace(
            "matching.*IDs", namespace="benchmark/perftest"
        )
    )
    assert search_resp["status"] == "ok"
    assert search_resp["total_documents_matched"] == 1
    assert search_resp["results"][0]["ref"] == "benchmark/perftest/README.md"

    manifest_resp = json.loads(
        await ws_server.read_document_from_workspace(
            "workspace://context/effective-context.json"
        )
    )
    assert manifest_resp["status"] == "ok"
    assert manifest_resp["workspace_ref"] == (
        "workspace://context/effective-context.json"
    )
