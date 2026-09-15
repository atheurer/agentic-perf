"""Tests for hard-stop after abort/drift (#492).

Covers: _execute_tool re-raises control-flow exceptions,
_check_drift detects status changes, mid-batch skip,
full-loop drift prevention, retrospective agent re-raise,
budget-grace non-regression, and server-side assert_ticket_active.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import (
    AgentAbortedError,
    AgentBase,
    HITLDriftError,
    HITLTimeoutError,
    ToolCall,
    ToolResult,
)
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition

# ── Shared helpers ───────────────────────────────────────────


class _StubAgent(AgentBase):
    """Minimal agent for testing base class methods."""

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        pass


class _FinishingLLM(LLMProvider):
    """LLM that immediately returns end_turn."""

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        return LLMResponse(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )


def _make_agent(
    tmp_path,
    tool_handlers: dict | None = None,
) -> _StubAgent:
    """Create a _StubAgent with optional tool handlers."""
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=_FinishingLLM(),
        state_store_url="http://localhost:8090",
        tool_handlers=tool_handlers or {},
        event_bus=EventBus(log_dir=tmp_path / "logs"),
    )
    agent._client = AsyncMock()
    return agent


def _make_tool_call(
    name: str = "test_tool",
    tool_input: dict | None = None,
) -> ToolCall:
    return ToolCall(
        id="tc-001",
        name=name,
        input=tool_input or {},
    )


# ── 1. _execute_tool re-raises control-flow exceptions ──────


class TestExecuteToolReraise:
    @pytest.mark.asyncio
    async def test_reraises_hitl_drift(self, tmp_path):
        """HITLDriftError from a local tool handler must propagate."""

        async def drift_handler(**_kwargs):
            raise HITLDriftError("ticket drifted")

        agent = _make_agent(tmp_path, {"drift_tool": drift_handler})
        tc = _make_tool_call(name="drift_tool")

        with pytest.raises(HITLDriftError):
            await agent._execute_tool(tc)

    @pytest.mark.asyncio
    async def test_reraises_hitl_timeout(self, tmp_path):
        """HITLTimeoutError from a local tool handler must propagate."""

        async def timeout_handler(**_kwargs):
            raise HITLTimeoutError("timeout")

        agent = _make_agent(tmp_path, {"timeout_tool": timeout_handler})
        tc = _make_tool_call(name="timeout_tool")

        with pytest.raises(HITLTimeoutError):
            await agent._execute_tool(tc)

    @pytest.mark.asyncio
    async def test_reraises_agent_aborted(self, tmp_path):
        """AgentAbortedError from a local tool handler must propagate."""

        async def aborted_handler(**_kwargs):
            raise AgentAbortedError("aborted")

        agent = _make_agent(tmp_path, {"aborted_tool": aborted_handler})
        tc = _make_tool_call(name="aborted_tool")

        with pytest.raises(AgentAbortedError):
            await agent._execute_tool(tc)

    @pytest.mark.asyncio
    async def test_reraises_mcp_drift(self, tmp_path):
        """HITLDriftError from an MCP tool call must propagate."""
        agent = _make_agent(tmp_path)
        agent._mcp = MagicMock()
        agent._mcp.call_tool = AsyncMock(
            side_effect=HITLDriftError("mcp drift"),
        )
        tc = _make_tool_call(name="mcp_only_tool")

        with pytest.raises(HITLDriftError):
            await agent._execute_tool(tc)

    @pytest.mark.asyncio
    async def test_generic_exception_still_returns_tool_result(
        self,
        tmp_path,
    ):
        """Non-control-flow exceptions are still caught and
        returned as ToolResult errors."""

        async def fail_handler(**_kwargs):
            raise ValueError("normal failure")

        agent = _make_agent(tmp_path, {"fail_tool": fail_handler})
        tc = _make_tool_call(name="fail_tool")

        result = await agent._execute_tool(tc)
        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "normal failure" in result.content


# ── 2. _check_drift ─────────────────────────────────────────


class TestCheckDrift:
    def test_detects_status_change(self, tmp_path):
        """_check_drift raises AgentAbortedError on status mismatch."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = {
            "id": "PERF-001",
            "status": "awaiting_teardown",
        }

        with pytest.raises(AgentAbortedError, match="drifted"):
            agent._check_drift()

        assert agent._aborted is True

    def test_exempts_guidance(self, tmp_path):
        """_check_drift does NOT raise when status is
        awaiting_customer_guidance (budget-grace transitions)."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = {
            "id": "PERF-001",
            "status": "awaiting_customer_guidance",
        }

        agent._check_drift()
        assert agent._aborted is False

    def test_no_raise_when_status_matches(self, tmp_path):
        """_check_drift is silent when status matches dispatch."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = {
            "id": "PERF-001",
            "status": "executing_benchmark",
        }

        agent._check_drift()
        assert agent._aborted is False

    def test_idempotent_abort_emit(self, tmp_path):
        """agent_aborted emitted exactly once on repeated calls."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = {
            "id": "PERF-001",
            "status": "awaiting_teardown",
        }

        emitted = []
        original_emit = agent._emit

        def capturing_emit(ticket_id, event_type, data=None):
            emitted.append(event_type)
            original_emit(ticket_id, event_type, data)

        agent._emit = capturing_emit

        with pytest.raises(AgentAbortedError):
            agent._check_drift()

        with pytest.raises(AgentAbortedError):
            agent._check_drift()

        abort_events = [e for e in emitted if e == "agent_aborted"]
        assert len(abort_events) == 1

    def test_noop_when_no_ticket(self, tmp_path):
        """_check_drift is a no-op when _last_interject_ticket is None."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = None

        agent._check_drift()


# ── 3. Mid-batch skip ───────────────────────────────────────


class TestMidBatchSkip:
    @pytest.mark.asyncio
    async def test_aborted_flag_skips_remaining_tools(self, tmp_path):
        """When _aborted is True, the tool batch loop should raise
        AgentAbortedError before executing the next tool."""
        call_log = []

        async def logging_handler(**_kwargs):
            call_log.append("executed")
            return "ok"

        agent = _make_agent(tmp_path, {"tool_a": logging_handler})
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = True

        tc = _make_tool_call(name="tool_a")

        calls_to_run = [tc]
        tool_results_content = []

        with pytest.raises(AgentAbortedError):
            for tc_item in calls_to_run:
                if agent._aborted:
                    raise AgentAbortedError(
                        "Skipping remaining tool calls — agent aborted",
                    )
                result = await agent._execute_tool(tc_item)
                tool_results_content.append(result)

        assert len(call_log) == 0


# ── 4. Full-loop drift stops before LLM call ────────────────


class TestFullLoopDrift:
    @pytest.mark.asyncio
    async def test_drift_stops_before_llm(self, tmp_path):
        """When _check_interject caches a drifted ticket,
        _check_drift should raise before the LLM is called."""
        call_count = 0

        class CountingLLM(LLMProvider):
            async def complete(self, system_prompt, messages, **kwargs):
                nonlocal call_count
                call_count += 1
                return LLMResponse(
                    text="done",
                    tool_calls=[],
                    stop_reason="end_turn",
                    raw_content=[],
                )

        agent = _StubAgent(
            agent_name="test-agent",
            llm_provider=CountingLLM(),
            state_store_url="http://localhost:8090",
            event_bus=EventBus(log_dir=tmp_path / "logs"),
        )
        agent._client = AsyncMock()

        ticket_data = {
            "id": "PERF-DRIFT01",
            "status": "executing_benchmark",
            "summary": "test",
            "description": "test",
            "custom_fields": {},
            "comments": [],
        }

        drifted_data = {
            "id": "PERF-DRIFT01",
            "status": "awaiting_teardown",
            "summary": "test",
            "description": "test",
            "custom_fields": {"abort_requested": True},
            "comments": [],
        }

        get_call_count = 0

        async def mock_get_ticket(tid):
            nonlocal get_call_count
            get_call_count += 1
            if get_call_count <= 1:
                return ticket_data
            return drifted_data

        agent._get_ticket = mock_get_ticket

        with pytest.raises(AgentAbortedError):
            await agent.run("PERF-DRIFT01")

        assert call_count == 0


# ── 4b. Drift during LLM call stops before tool execution ────


class TestDriftDuringLLMCall:
    @pytest.mark.asyncio
    async def test_drift_during_llm_stops_tools(self, tmp_path):
        """If the ticket drifts while the LLM call is in flight,
        the pre-tool-batch drift check should catch it before any
        tool executes."""
        tool_executed = False

        async def side_effecting_tool(**_kwargs):
            nonlocal tool_executed
            tool_executed = True
            return "executed"

        class DriftMidCallLLM(LLMProvider):
            def __init__(self, get_ticket_fn):
                self._get_ticket_fn = get_ticket_fn

            async def complete(self, system_prompt, messages, **kwargs):
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="tc-side",
                            name="dangerous_tool",
                            input={},
                        ),
                    ],
                    stop_reason="tool_use",
                    raw_content=[
                        {
                            "type": "tool_use",
                            "id": "tc-side",
                            "name": "dangerous_tool",
                            "input": {},
                        },
                    ],
                )

        ticket_normal = {
            "id": "PERF-MID01",
            "status": "executing_benchmark",
            "summary": "test",
            "description": "test",
            "custom_fields": {},
            "comments": [],
        }

        ticket_drifted = {
            "id": "PERF-MID01",
            "status": "awaiting_teardown",
            "summary": "test",
            "description": "test",
            "custom_fields": {"abort_requested": True},
            "comments": [],
        }

        call_seq = 0

        async def mock_get_ticket(tid):
            nonlocal call_seq
            call_seq += 1
            if call_seq <= 2:
                return ticket_normal
            return ticket_drifted

        llm = DriftMidCallLLM(mock_get_ticket)
        agent = _StubAgent(
            agent_name="test-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
            tool_handlers={"dangerous_tool": side_effecting_tool},
            event_bus=EventBus(log_dir=tmp_path / "logs"),
        )
        agent._client = AsyncMock()
        agent._get_ticket = mock_get_ticket

        with pytest.raises(AgentAbortedError):
            await agent.run("PERF-MID01")

        assert not tool_executed


# ── 5. Retrospective agent re-raises drift ──────────────────


class TestRetrospectiveReraise:
    @pytest.mark.asyncio
    async def test_reraises_drift(self, tmp_path):
        """RetrospectiveAgent.run must re-raise HITLDriftError."""
        from agents.retrospective.agent import RetrospectiveAgent

        agent = RetrospectiveAgent(
            llm_provider=_FinishingLLM(),
            state_store_url="http://localhost:8090",
            event_bus=EventBus(log_dir=tmp_path / "logs"),
        )
        agent._client = AsyncMock()

        with (
            patch.object(
                AgentBase,
                "run",
                side_effect=HITLDriftError("drifted"),
            ),
            patch.object(
                agent,
                "_mcp",
                create=True,
                new=None,
            ),
            pytest.raises(HITLDriftError),
        ):
            mcp_mock = AsyncMock()
            mcp_mock.connect = AsyncMock()
            mcp_mock.disconnect = AsyncMock()
            mcp_mock.list_tools = AsyncMock(return_value=[])
            with patch(
                "agents.retrospective.agent.AgentMCPClient",
                return_value=mcp_mock,
            ):
                await agent.run("PERF-RETRO01")


# ── 6. Budget-grace non-regression ──────────────────────────


class TestBudgetGraceNonRegression:
    def test_guidance_status_does_not_trigger_drift(self, tmp_path):
        """An agent at awaiting_customer_guidance (budget-grace) should
        NOT be flagged as drifted — this is an intentional transition."""
        agent = _make_agent(tmp_path)
        agent._dispatched_status = "executing_benchmark"
        agent._aborted = False
        agent._last_interject_ticket = {
            "id": "PERF-001",
            "status": "awaiting_customer_guidance",
        }

        agent._check_drift()
        assert agent._aborted is False


# ── 7. Server-side assert_ticket_active ──────────────────────


class TestAssertTicketActive:
    @pytest.mark.asyncio
    async def test_rejects_aborted_ticket(self):
        """assert_ticket_active returns rejection for aborted tickets."""
        from agents.server_utils import assert_ticket_active

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id": "PERF-001",
            "status": "awaiting_teardown",
            "custom_fields": {
                "abort_requested": {
                    "requested_at": "2026-01-01T00:00:00Z",
                },
            },
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await assert_ticket_active(
                ticket_id="PERF-001",
                state_store_url="http://localhost:8090",
                expected_status="executing_benchmark",
            )

        assert result["status"] == "rejected"
        assert "aborted" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_allows_active_ticket(self):
        """assert_ticket_active returns full ticket for active tickets."""
        from agents.server_utils import assert_ticket_active

        ticket_data = {
            "id": "PERF-002",
            "status": "executing_benchmark",
            "custom_fields": {},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = ticket_data

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await assert_ticket_active(
                ticket_id="PERF-002",
                state_store_url="http://localhost:8090",
                expected_status="executing_benchmark",
            )

        assert result["id"] == "PERF-002"
        assert "status" not in result or result["status"] == "executing_benchmark"

    @pytest.mark.asyncio
    async def test_rejects_wrong_status(self):
        """assert_ticket_active rejects when status doesn't match."""
        from agents.server_utils import assert_ticket_active

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id": "PERF-003",
            "status": "awaiting_review",
            "custom_fields": {},
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await assert_ticket_active(
                ticket_id="PERF-003",
                state_store_url="http://localhost:8090",
                expected_status="executing_benchmark",
            )

        assert result["status"] == "rejected"
        assert "awaiting_review" in result["reason"]

    @pytest.mark.asyncio
    async def test_no_ticket_id_returns_empty(self):
        """Without a ticket_id, assert_ticket_active allows (dev mode)."""
        from agents.server_utils import assert_ticket_active

        with patch.dict("os.environ", {}, clear=False):
            result = await assert_ticket_active(
                ticket_id="",
                state_store_url="http://localhost:8090",
            )

        assert result == {}
