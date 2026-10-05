from __future__ import annotations

import asyncio
import json

import pytest

import agents.benchmark.agent as benchmark_agent_module
from agents.benchmark.agent import BenchmarkAgent
from agents.mcp_client import _MCP_TIMEOUT_CANCELLATION
from agents.review.agent import ReviewAgent
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
                        "notes": (
                            "Throughput: 27.4 Gbps; latency p99: 1.8 ms; "
                            "samples: 5; errors: none."
                        ),
                    },
                )
            ],
            stop_reason="tool_use",
        ),
    )
    assert persisted_fields["run_id"] == "execution-1"
    assert persisted_fields["benchmark_notes"] == (
        "Throughput: 27.4 Gbps; latency p99: 1.8 ms; samples: 5; errors: none."
    )

    review = ReviewAgent.__new__(ReviewAgent)
    review._skill_provider = None
    review._repo_cache = None
    review._referenced_artifacts = {}
    review_ticket = {
        "id": "ticket-1",
        "summary": "Arcaflow workflow result",
        "description": "Review workflow measurements.",
        "custom_fields": {
            "directives": {"harness": "arcaflow-workflows"},
            **persisted_fields,
        },
        "comments": [
            {
                "author": "benchmark-agent",
                "body": "Benchmark completed; run metadata only.",
            }
        ],
    }
    review_content = review._build_messages(review_ticket)[0]["content"]
    assert "## Benchmark Output Summary" in review_content
    assert "Throughput: 27.4 Gbps" in review_content
    assert "latency p99: 1.8 ms" in review_content
    assert "samples: 5" in review_content
    assert "Benchmark completed; run metadata only." not in review_content


def test_benchmark_notes_are_bounded_for_ticket_and_review_context():
    from agents.benchmark.agent import (
        _MAX_BENCHMARK_NOTES_CHARS,
        _bounded_benchmark_notes,
    )

    bounded = _bounded_benchmark_notes("x" * (_MAX_BENCHMARK_NOTES_CHARS + 100))

    assert len(bounded) == _MAX_BENCHMARK_NOTES_CHARS
    assert bounded.endswith("[benchmark notes truncated]")


@pytest.mark.parametrize("benchmark_notes", [None, ""])
def test_arcaflow_review_reports_missing_execution_evidence(benchmark_notes):
    review = ReviewAgent.__new__(ReviewAgent)
    review._skill_provider = None
    review._repo_cache = None
    review._referenced_artifacts = {}
    custom_fields = {"directives": {"harness": "arcaflow-plugins"}}
    if benchmark_notes is not None:
        custom_fields["benchmark_notes"] = benchmark_notes
    ticket = {
        "id": "ticket-no-summary",
        "summary": "Review Arcaflow results",
        "description": "Analyze the benchmark.",
        "custom_fields": custom_fields,
        "comments": [],
    }

    prompt = review._system_prompt(ticket)
    content = review._build_messages(ticket)[0]["content"]

    assert "No benchmark_notes execution summary is available" in prompt
    assert "state this evidence limitation" in prompt
    assert "No benchmark_notes execution summary is available" in content
    assert "do not infer measurements" in content


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
async def test_arcaflow_late_launch_response_is_cancelled_after_caller_cancellation():
    class _LateLaunchWorkflowMCP(_WorkflowMCP):
        def __init__(self):
            super().__init__(exported_payload='{"duration":300}')
            self.launch_started = asyncio.Event()
            self.finish_launch = asyncio.Event()

        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "workflow_execute":
                self.calls.append((name, arguments))
                self.launch_started.set()
                await self.finish_launch.wait()
                return json.dumps({"execution_id": "late-execution-1"})
            if name == "workflow_execution_cancel":
                self.calls.append((name, arguments))
                return json.dumps({"cancelled": True})
            return await super().call_tool(name, arguments)

    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _LateLaunchWorkflowMCP()
    agent._mcp = mcp
    execution = asyncio.create_task(
        agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            {"duration": "5m"},
            "benchmark",
        )
    )

    await mcp.launch_started.wait()
    execution.cancel()
    await asyncio.sleep(0)
    mcp.finish_launch.set()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert (
        "workflow_execution_cancel",
        {"execution_id": "late-execution-1"},
    ) in mcp.calls


@pytest.mark.asyncio
async def test_arcaflow_late_launch_during_stop_grace_dispatches_cancel_before_deadline(
    monkeypatch,
):
    class _SharedDeadlineWorkflowMCP(_WorkflowMCP):
        def __init__(self):
            super().__init__(exported_payload='{"duration":300}')
            self.launch_started = asyncio.Event()
            self.release_launch = asyncio.Event()
            self.cancel_started = asyncio.Event()
            self.cancel_finished = asyncio.Event()
            self.cancel_started_at = 0.0
            self.cancel_finished_at = 0.0
            self.cancel_reason: tuple = ()
            self.launch_cancel_reason: tuple = ()

        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "workflow_execute":
                self.calls.append((name, arguments))
                self.launch_started.set()
                try:
                    await self.release_launch.wait()
                except asyncio.CancelledError as exc:
                    self.launch_cancel_reason = exc.args
                    await self.release_launch.wait()
                return json.dumps({"execution_id": "late-execution-2"})
            if name == "workflow_execution_cancel":
                self.calls.append((name, arguments))
                self.cancel_started_at = asyncio.get_running_loop().time()
                self.cancel_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    self.cancel_reason = exc.args
                    self.cancel_finished_at = asyncio.get_running_loop().time()
                    self.cancel_finished.set()
                    raise
            return await super().call_tool(name, arguments)

    reconciliation_timeout = 0.15
    stop_grace = 0.03
    launch_cancel_reserve = 0.03
    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_RECONCILIATION_TIMEOUT_SECONDS",
        reconciliation_timeout,
    )
    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_TASK_STOP_GRACE_SECONDS",
        stop_grace,
    )
    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_LAUNCH_CANCEL_RESERVE_SECONDS",
        launch_cancel_reserve,
    )
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _SharedDeadlineWorkflowMCP()
    agent._mcp = mcp
    execution = asyncio.create_task(
        agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            {"duration": "5m"},
            "benchmark",
        )
    )

    async def release_late_launch() -> None:
        await asyncio.sleep(0.105)
        mcp.release_launch.set()

    await mcp.launch_started.wait()
    release_task = asyncio.create_task(release_late_launch())
    cancellation_started = asyncio.get_running_loop().time()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=0.5)
    await release_task

    assert mcp.cancel_started.is_set()
    assert mcp.cancel_finished.is_set()
    assert mcp.cancel_reason == (_MCP_TIMEOUT_CANCELLATION,)
    assert mcp.launch_cancel_reason == (_MCP_TIMEOUT_CANCELLATION,)
    assert mcp.cancel_started_at < cancellation_started + reconciliation_timeout
    assert (
        mcp.cancel_finished_at - mcp.cancel_started_at < reconciliation_timeout * 0.75
    )


@pytest.mark.asyncio
async def test_arcaflow_hung_launch_does_not_block_caller_cancellation(
    monkeypatch,
    caplog,
):
    class _HungLaunchWorkflowMCP(_WorkflowMCP):
        def __init__(self):
            super().__init__(exported_payload='{"duration":300}')
            self.launch_started = asyncio.Event()
            self.release_launch = asyncio.Event()
            self.launch_finished = asyncio.Event()
            self.launch_cancel_reason: tuple = ()

        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "workflow_execute":
                self.calls.append((name, arguments))
                self.launch_started.set()
                while not self.release_launch.is_set():
                    try:
                        await self.release_launch.wait()
                    except asyncio.CancelledError as exc:
                        self.launch_cancel_reason = exc.args
                        continue
                self.launch_finished.set()
                return json.dumps({"execution_id": "late-but-unreconciled"})
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_RECONCILIATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_TASK_STOP_GRACE_SECONDS",
        0.01,
    )
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _HungLaunchWorkflowMCP()
    agent._mcp = mcp
    execution = asyncio.create_task(
        agent._execute_arcaflow_workflow(
            "https://example.test/workflow.yaml",
            {"duration": "5m"},
            "benchmark",
        )
    )

    await mcp.launch_started.wait()
    execution.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=0.5)
        assert "remote outcome is indeterminate" in caplog.text
        assert mcp.launch_cancel_reason == (_MCP_TIMEOUT_CANCELLATION,)
    finally:
        mcp.release_launch.set()
        await asyncio.wait_for(mcp.launch_finished.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_arcaflow_hung_cancel_does_not_block_caller_cancellation(
    monkeypatch,
    caplog,
):
    class _HungCancelWorkflowMCP(_WorkflowMCP):
        def __init__(self):
            super().__init__(exported_payload='{"duration":300}')
            self.status_started = asyncio.Event()
            self.release_cancel = asyncio.Event()
            self.cancel_finished = asyncio.Event()
            self.cancel_reason: tuple = ()

        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "workflow_execution_status":
                self.calls.append((name, arguments))
                self.status_started.set()
                await asyncio.Event().wait()
            if name == "workflow_execution_cancel":
                self.calls.append((name, arguments))
                while not self.release_cancel.is_set():
                    try:
                        await self.release_cancel.wait()
                    except asyncio.CancelledError as exc:
                        self.cancel_reason = exc.args
                        continue
                self.cancel_finished.set()
                return json.dumps({"cancelled": True})
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_RECONCILIATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        benchmark_agent_module,
        "_ARCAFLOW_CANCELLATION_TASK_STOP_GRACE_SECONDS",
        0.01,
    )
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    mcp = _HungCancelWorkflowMCP()
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
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=0.5)
        assert "remote outcome is indeterminate" in caplog.text
        assert mcp.cancel_reason == (_MCP_TIMEOUT_CANCELLATION,)
    finally:
        mcp.release_cancel.set()
        await asyncio.wait_for(mcp.cancel_finished.wait(), timeout=0.5)


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
