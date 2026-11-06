"""Tests for cross-ticket artifact discovery in the analyze agent."""

from __future__ import annotations

import json

import pytest


def _mock_ticket_client(monkeypatch, payload):
    from unittest.mock import AsyncMock, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = payload

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "providers.execution.AuditedAsyncHTTPClient",
        lambda **kwargs: mock_client,
    )


@pytest.mark.asyncio
async def test_get_ticket_results_artifact_fields(tmp_path, monkeypatch):
    """get_ticket_results should populate artifact_base_path and artifact_run_dirs."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

    ticket_dir = tmp_path / "PERF-1234"
    run_a = ticket_dir / "run-aaa"
    run_b = ticket_dir / "run-bbb"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    (run_a / "output.json").write_text("{}")

    _mock_ticket_client(
        monkeypatch,
        {
            "id": "PERF-1234",
            "summary": "Test ticket",
            "status": "closed",
            "custom_fields": {
                "directives": {"harness": "boot-time"},
                "benchmark_result": {"kpis": {}},
                "output_dir": "/some/path",
                "output_dirs": ["/some/path"],
            },
        },
    )

    from agents.analyze.server import get_ticket_results

    result = json.loads(await get_ticket_results("PERF-1234"))

    assert result["ticket_id"] == "PERF-1234"
    assert result["output_dir"] == "/some/path"
    assert result["output_dirs"] == ["/some/path"]
    assert result["artifact_base_path"] == str(ticket_dir)
    assert sorted(result["artifact_run_dirs"]) == [str(run_a), str(run_b)]


@pytest.mark.asyncio
async def test_get_ticket_results_discovers_artifacts_without_output_dir(
    tmp_path, monkeypatch
):
    """Artifacts are discoverable even when output_dir was never persisted."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)

    ticket_dir = tmp_path / "PERF-NOOUT"
    run_dir = ticket_dir / "run-xyz"
    run_dir.mkdir(parents=True)

    _mock_ticket_client(
        monkeypatch,
        {"id": "PERF-NOOUT", "summary": "t", "status": "closed", "custom_fields": {}},
    )

    from agents.analyze.server import get_ticket_results

    result = json.loads(await get_ticket_results("PERF-NOOUT"))

    assert result["output_dir"] is None
    assert result["artifact_base_path"] == str(ticket_dir)
    assert result["artifact_run_dirs"] == [str(run_dir)]


@pytest.mark.asyncio
async def test_get_ticket_results_rejects_escaping_ticket_id(tmp_path, monkeypatch):
    """A ticket id that escapes the artifact root yields no artifact path."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path / "artifacts")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "escape").mkdir()

    _mock_ticket_client(
        monkeypatch,
        {"id": "../escape", "summary": "t", "status": "closed", "custom_fields": {}},
    )

    from agents.analyze.server import get_ticket_results

    result = json.loads(await get_ticket_results("../escape"))
    assert "artifact_base_path" not in result


@pytest.mark.asyncio
async def test_list_benchmark_artifacts_analyze(tmp_path, monkeypatch):
    """list_benchmark_artifacts in analyze server lists files recursively."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import list_benchmark_artifacts

    run_dir = tmp_path / "PERF-1" / "run-1"
    sub = run_dir / "subdir"
    sub.mkdir(parents=True)
    (run_dir / "top.json").write_text("{}")
    (sub / "nested.log").write_text("log data")

    result = json.loads(await list_benchmark_artifacts(str(run_dir)))

    assert result["count"] == 2
    paths_found = {a["path"] for a in result["artifacts"]}
    assert "top.json" in paths_found
    assert "subdir/nested.log" in paths_found


@pytest.mark.asyncio
async def test_list_benchmark_artifacts_rejects_outside_artifact_root(
    tmp_path, monkeypatch
):
    """Directories outside ARTIFACT_DIR are rejected."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path / "artifacts")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "outside").mkdir()
    from agents.analyze.server import list_benchmark_artifacts

    result = json.loads(await list_benchmark_artifacts(str(tmp_path / "outside")))
    assert "error" in result


@pytest.mark.asyncio
async def test_list_benchmark_artifacts_missing_dir(tmp_path, monkeypatch):
    """list_benchmark_artifacts returns error for nonexistent directory."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import list_benchmark_artifacts

    result = json.loads(await list_benchmark_artifacts(str(tmp_path / "nope")))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_benchmark_artifact_analyze(tmp_path, monkeypatch):
    """read_benchmark_artifact in analyze server reads file content."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import read_benchmark_artifact

    run_dir = tmp_path / "PERF-2" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "data.txt").write_text("line1\nline2\nline3\n")

    result = json.loads(await read_benchmark_artifact(str(run_dir), "data.txt"))

    assert result["total_lines"] == 3
    assert result["lines_returned"] == 3
    assert "line1" in result["content"]


@pytest.mark.asyncio
async def test_read_benchmark_artifact_offset_limit(tmp_path, monkeypatch):
    """read_benchmark_artifact honours offset/limit."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import read_benchmark_artifact

    run_dir = tmp_path / "PERF-3" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "data.txt").write_text("\n".join(f"line{i}" for i in range(10)))

    result = json.loads(
        await read_benchmark_artifact(str(run_dir), "data.txt", offset=3, limit=2)
    )
    assert result["offset"] == 3
    assert result["lines_returned"] == 2
    assert result["content"] == "line3\nline4"


@pytest.mark.asyncio
async def test_read_benchmark_artifact_path_traversal(tmp_path, monkeypatch):
    """read_benchmark_artifact rejects traversal outside the run dir."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import read_benchmark_artifact

    run_dir = tmp_path / "PERF-4" / "run-1"
    run_dir.mkdir(parents=True)
    (tmp_path / "PERF-4" / "secret.txt").write_text("secret")

    result = json.loads(await read_benchmark_artifact(str(run_dir), "../secret.txt"))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_benchmark_artifact_rejects_outside_artifact_root(
    tmp_path, monkeypatch
):
    """An output_dir outside ARTIFACT_DIR is rejected before reading."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path / "artifacts")
    (tmp_path / "artifacts").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    from agents.analyze.server import read_benchmark_artifact

    result = json.loads(await read_benchmark_artifact(str(outside), "secret.txt"))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_benchmark_artifact_missing_file(tmp_path, monkeypatch):
    """read_benchmark_artifact returns error for missing files."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    from agents.analyze.server import read_benchmark_artifact

    run_dir = tmp_path / "PERF-5" / "run-1"
    run_dir.mkdir(parents=True)

    result = json.loads(await read_benchmark_artifact(str(run_dir), "nonexistent.txt"))
    assert "error" in result
