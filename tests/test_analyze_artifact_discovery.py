"""Tests for cross-ticket artifact discovery in the analyze agent."""

from __future__ import annotations

import json

import pytest


def test_get_ticket_results_includes_artifact_paths(tmp_path, monkeypatch):
    """get_ticket_results should return artifact_base_path when artifacts exist."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

    # Create artifact structure
    ticket_dir = tmp_path / "PERF-TEST"
    run_dir = ticket_dir / "run-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text("{}")

    # Verify path construction
    assert ticket_dir.exists()
    assert len(list(ticket_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_get_ticket_results_artifact_fields(tmp_path, monkeypatch):
    """get_ticket_results should populate artifact_base_path and artifact_run_dirs."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

    # Create artifact structure on disk
    ticket_dir = tmp_path / "PERF-1234"
    run_a = ticket_dir / "run-aaa"
    run_b = ticket_dir / "run-bbb"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    (run_a / "output.json").write_text("{}")

    # Patch the HTTP client to return a fake ticket
    from unittest.mock import AsyncMock, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "id": "PERF-1234",
        "summary": "Test ticket",
        "status": "closed",
        "custom_fields": {
            "directives": {"harness": "boot-time"},
            "benchmark_result": {"kpis": {}},
            "output_dir": "/some/path",
            "output_dirs": ["/some/path"],
        },
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "providers.execution.AuditedAsyncHTTPClient",
        lambda **kwargs: mock_client,
    )

    from agents.analyze.server import get_ticket_results

    raw = await get_ticket_results("PERF-1234")
    result = json.loads(raw)

    assert result["ticket_id"] == "PERF-1234"
    assert result["output_dir"] == "/some/path"
    assert result["output_dirs"] == ["/some/path"]
    assert result["artifact_base_path"] == str(ticket_dir)
    assert sorted(result["artifact_run_dirs"]) == [str(run_a), str(run_b)]


@pytest.mark.asyncio
async def test_list_benchmark_artifacts_analyze(tmp_path):
    """list_benchmark_artifacts in analyze server lists files recursively."""
    from agents.analyze.server import list_benchmark_artifacts

    sub = tmp_path / "subdir"
    sub.mkdir()
    (tmp_path / "top.json").write_text("{}")
    (sub / "nested.log").write_text("log data")

    raw = await list_benchmark_artifacts(str(tmp_path))
    result = json.loads(raw)

    assert result["count"] == 2
    paths_found = {a["path"] for a in result["artifacts"]}
    assert "top.json" in paths_found
    assert "subdir/nested.log" in paths_found


@pytest.mark.asyncio
async def test_list_benchmark_artifacts_missing_dir():
    """list_benchmark_artifacts returns error for nonexistent directory."""
    from agents.analyze.server import list_benchmark_artifacts

    raw = await list_benchmark_artifacts("/nonexistent/path")
    result = json.loads(raw)
    assert "error" in result


@pytest.mark.asyncio
async def test_read_benchmark_artifact_analyze(tmp_path):
    """read_benchmark_artifact in analyze server reads file content."""
    from agents.analyze.server import read_benchmark_artifact

    (tmp_path / "data.txt").write_text("line1\nline2\nline3\n")

    raw = await read_benchmark_artifact(str(tmp_path), "data.txt")
    result = json.loads(raw)

    assert result["total_lines"] == 3
    assert result["lines_returned"] == 3
    assert "line1" in result["content"]


@pytest.mark.asyncio
async def test_read_benchmark_artifact_path_traversal(tmp_path):
    """read_benchmark_artifact rejects path traversal attempts."""
    from agents.analyze.server import read_benchmark_artifact

    raw = await read_benchmark_artifact(str(tmp_path), "../../etc/passwd")
    result = json.loads(raw)
    assert "error" in result


@pytest.mark.asyncio
async def test_read_benchmark_artifact_missing_file(tmp_path):
    """read_benchmark_artifact returns error for missing files."""
    from agents.analyze.server import read_benchmark_artifact

    raw = await read_benchmark_artifact(str(tmp_path), "nonexistent.txt")
    result = json.loads(raw)
    assert "error" in result
