from __future__ import annotations

import asyncio
import json

import pytest

from agents.benchmark.agent import BenchmarkAgent
from providers.llm.base import LLMResponse, ToolCall


class _WorkflowMCP:
    def __init__(
        self,
        *,
        exported_payload: str,
        output_error: Exception | None = None,
    ):
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
    assert result["run_id"] == "execution-1"
    assert ("workflow_input_export", {"input": original_input}) in mcp.calls
    assert ("workflow_execute", {"input": exported_payload}) in mcp.calls

    persisted_fields: dict = {}

    async def update_fields(_ticket_id: str, fields: dict) -> None:
        persisted_fields.update(fields)

    async def get_ticket(_ticket_id: str) -> dict:
        return {"custom_fields": {}}

    async def no_op(*_args, **_kwargs) -> None:
        return None

    agent._update_fields = update_fields
    agent._get_ticket = get_ticket
    agent._add_comment = no_op
    agent._transition_ticket = no_op
    agent._active_validation_id = None

    async def plan_controls_next_transition(_ticket_id: str) -> bool:
        return False

    agent._plan_controls_next_transition = plan_controls_next_transition
    await agent._handle_completion(
        "ticket-1",
        LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="submit-1",
                    name="submit_benchmark_result",
                    input={
                        "run_id": result["run_id"],
                        "benchmark_status": "completed",
                    },
                )
            ],
            stop_reason="tool_use",
        ),
    )
    assert persisted_fields["run_id"] == "execution-1"


@pytest.mark.asyncio
async def test_arcaflow_cancellation_cancels_engine_execution_before_reraising():
    class _BlockingWorkflowMCP(_WorkflowMCP):
        def __init__(self):
            super().__init__(exported_payload='{"duration":300}')
            self.status_started = asyncio.Event()

        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "workflow_execution_status":
                self.calls.append((name, arguments))
                self.status_started.set()
                await asyncio.Event().wait()
            if name == "workflow_execution_cancel":
                self.calls.append((name, arguments))
                return json.dumps({"cancelled": True})
            return await super().call_tool(name, arguments)

    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _BlockingWorkflowMCP()
    agent._mcp = mcp
    execution = asyncio.create_task(
        agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            {"duration": "5m"},
            "benchmark",
        )
    )

    await mcp.status_started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert (
        "workflow_execution_cancel",
        {"execution_id": "execution-1"},
    ) in mcp.calls


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
