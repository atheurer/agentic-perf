from __future__ import annotations

import json

import pytest

from agents.benchmark.agent import BenchmarkAgent


class _WorkflowMCP:
    def __init__(self, *, exported_payload: str, output_error: Exception | None = None):
        self.exported_payload = exported_payload
        self.output_error = output_error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if name == "workflow_load":
            return json.dumps({"loaded": True})
        if name == "workflow_input_export":
            return self.exported_payload
        if name == "workflow_execute":
            return json.dumps({"execution_id": "execution-1"})
        if name == "workflow_execution_status":
            return json.dumps({"state": "completed"})
        if name == "workflow_execution_output":
            if self.output_error is not None:
                raise self.output_error
            return json.dumps({"output_data": {"result": "ok"}})
        raise AssertionError(f"unexpected MCP tool: {name}")


@pytest.mark.asyncio
async def test_arcaflow_execution_passes_exported_input_to_engine():
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    original_input = {"duration": "5m"}
    exported_payload = '{"duration":300}'
    mcp = _WorkflowMCP(exported_payload=exported_payload)
    agent._mcp = mcp

    result = json.loads(
        await agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            original_input,
            "benchmark",
        )
    )

    assert result["status"] == "completed"
    assert ("workflow_input_export", {"input": original_input}) in mcp.calls
    assert ("workflow_execute", {"input": exported_payload}) in mcp.calls


@pytest.mark.asyncio
async def test_arcaflow_output_retrieval_failure_is_not_reported_as_completed():
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _WorkflowMCP(
        exported_payload='{"duration":300}',
        output_error=RuntimeError("engine output unavailable"),
    )
    agent._mcp = mcp

    result = json.loads(
        await agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            {"duration": "5m"},
            "benchmark",
        )
    )

    assert result["status"] == "failed"
    assert result["execution_state"] == "completed"
    assert result["output"] is None
    assert "workflow_execution_output failed" in result["error"]
    assert "engine output unavailable" in result["error"]
