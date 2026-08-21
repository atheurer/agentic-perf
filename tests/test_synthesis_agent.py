"""Tests for the Synthesis agent.

Tests Investigation Record creation, operational metrics collection,
state transitions, and dispatcher integration.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State transitions ---


class TestStateTransitions:
    def test_synthesis_to_teardown(self):
        valid = VALID_TRANSITIONS[TicketStatus.SYNTHESIZING_RESULTS]
        assert TicketStatus.AWAITING_TEARDOWN in valid

    def test_synthesis_to_hitl(self):
        valid = VALID_TRANSITIONS[TicketStatus.SYNTHESIZING_RESULTS]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in valid

    def test_evaluate_to_synthesis(self):
        valid = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
        assert TicketStatus.SYNTHESIZING_RESULTS in valid


# --- Agent construction ---


class TestAgentConstruction:
    def test_agent_name(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent.agent_name == "synthesis-agent"

    def test_system_prompt(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        prompt = agent._system_prompt({})
        assert "Investigation Record" in prompt
        assert "submit_synthesis_result" in prompt


# --- Message building ---


class TestMessageBuilding:
    def test_includes_investigation_context(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Storage regression",
            "custom_fields": {
                "anomaly_context": {
                    "subsystem": "storage_io",
                    "metric": "iops",
                },
                "evaluation_result": {
                    "decision": "converged",
                    "convergence_gate": "isolation",
                    "confidence": 0.95,
                    "root_cause_summary": "virtio-blk regression",
                },
                "investigation_ledger": [
                    {
                        "iteration": 1,
                        "hypothesis": "high concurrency",
                        "conclusion": "no effect",
                        "info_gain": 0.0,
                    },
                    {
                        "iteration": 2,
                        "hypothesis": "low queue depth",
                        "conclusion": "confirmed",
                        "info_gain": 0.9,
                    },
                ],
            },
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "storage_io" in content
        assert "isolation" in content
        assert "0.95" in content
        assert "high concurrency" in content
        assert "low queue depth" in content


# --- Operational metrics ---


class TestOperationalMetrics:
    def test_collects_from_ledger(self):
        import tempfile

        from agents.synthesis.agent import SynthesisAgent
        from providers.events import EventBus
        from providers.llm.mock import MockLLMProvider

        tmp = Path(tempfile.mkdtemp(prefix="test-synth-"))
        event_bus = EventBus(log_dir=tmp)

        # Emit transition events for provision cycle counting
        event_bus.emit(
            "PERF-TEST",
            "system",
            "transition",
            {"from": "planning", "to": "awaiting_provision"},
        )
        event_bus.emit(
            "PERF-TEST",
            "system",
            "transition",
            {"from": "evaluating", "to": "awaiting_provision"},
        )

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )

        cf = {
            "investigation_ledger": [
                {"info_gain": 0.0},
                {"info_gain": 0.5},
                {"info_gain": 0.9},
            ],
            "evaluation_result": {
                "convergence_gate": "isolation",
            },
        }

        metrics = agent._collect_operational_metrics("PERF-TEST", cf)
        assert metrics["info_gain_trajectory"] == [0.0, 0.5, 0.9]
        assert metrics["provision_cycles"] == 2
        assert metrics["convergence_outcome"] == "isolation"

    def test_collects_from_eventbus(self):
        import tempfile

        from agents.synthesis.agent import SynthesisAgent
        from providers.events import EventBus
        from providers.llm.mock import MockLLMProvider

        tmp = tempfile.mkdtemp()
        event_bus = EventBus(log_dir=tmp)
        event_bus.record_llm_usage("PERF-TEST", 5000, 2000, 3000, "claude-sonnet-4-6")

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )

        metrics = agent._collect_operational_metrics("PERF-TEST", {})
        rc = metrics["resource_consumption"]
        assert rc["llm_tokens_total"] == 7000
        assert rc["llm_invocations"] == 1
        assert rc["estimated_cost_usd"] > 0

    def test_handles_no_eventbus(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        metrics = agent._collect_operational_metrics("PERF-TEST", {})
        assert metrics["resource_consumption"] == {}


# --- Handle completion ---


class TestHandleCompletion:
    @pytest.mark.asyncio
    async def test_converged_creates_record_and_transitions(
        self,
    ):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "anomaly_context": {
                            "subsystem": "storage_io",
                            "metric": "iops",
                            "direction": "degrading",
                            "platform": "NXP",
                            "magnitude": "-31%",
                        },
                        "evaluation_result": {
                            "convergence_gate": "isolation",
                        },
                        "investigation_ledger": [
                            {"info_gain": 0.9},
                        ],
                        "execution_plan": {"steps": []},
                    },
                },
                raise_for_status=lambda: None,
            ),
        )
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        # Mock MCP to capture create_investigation_record call
        mock_mcp = AsyncMock()
        mock_mcp.call_tool = AsyncMock(
            return_value='{"status": "created", "investigation_id": "RCA-NEW"}'
        )
        agent._mcp = mock_mcp

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_synthesis_result",
                    input={
                        "root_cause_summary": "virtio-blk regression",
                        "confidence": 0.95,
                        "convergence_outcome": "ISOLATION",
                        "build_id": "build-2026-06-28",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Verify record creation was attempted
        mock_mcp.call_tool.assert_called_once()
        call_args = mock_mcp.call_tool.call_args
        assert call_args[0][0] == "create_investigation_record"
        assert call_args[0][1]["subsystem"] == "storage_io"
        assert call_args[0][1]["root_cause_summary"] == "virtio-blk regression"

        # Verify transition to teardown
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "awaiting_teardown"

    @pytest.mark.asyncio
    async def test_persists_synthesis_result(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "evaluation_result": {},
                        "investigation_ledger": [],
                        "execution_plan": {"steps": []},
                    },
                },
                raise_for_status=lambda: None,
            ),
        )
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_synthesis_result",
                    input={
                        "root_cause_summary": "unknown",
                        "confidence": 0.3,
                        "convergence_outcome": "ENTROPY_STALL",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find synthesis_result in patch calls
        patch_calls = [
            c
            for c in agent._client.patch.call_args_list
            if "synthesis_result" in str(c)
        ]
        assert len(patch_calls) >= 1
        body = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )
        sr = body["fields"]["synthesis_result"]
        assert sr["convergence_outcome"] == "ENTROPY_STALL"
        assert sr["confidence"] == 0.3
        assert "operational_metrics" in sr


# --- Dispatcher ---


class TestDispatcherIntegration:
    def test_dispatcher_creates_synthesis_agent(self):
        from agents.synthesis.agent import SynthesisAgent
        from orchestrator.dispatcher import Dispatcher
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MockLLMProvider(),
            skill_provider=MockSkillProvider(),
        )
        agent = dispatcher.create_agent("synthesizing_results")
        assert isinstance(agent, SynthesisAgent)
        assert agent.agent_name == "synthesis-agent"


class TestSkippedPlanSteps:
    """Synthesis context correctly reports skipped steps."""

    def test_skipped_steps_in_context(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Test skipped steps",
            "custom_fields": {
                "execution_plan": {
                    "steps": [
                        {"id": 0, "agent_type": "analyze", "status": "completed"},
                        {"id": 1, "agent_type": "resource", "status": "completed"},
                        {"id": 2, "agent_type": "benchmark", "status": "skipped"},
                        {"id": 3, "agent_type": "review", "status": "skipped"},
                        {"id": 4, "agent_type": "teardown", "status": "skipped"},
                    ],
                },
            },
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "2 completed" in content
        assert "3 skipped" in content
        assert "concluded before" in content

    def test_no_skipped_steps(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "All completed",
            "custom_fields": {
                "execution_plan": {
                    "steps": [
                        {"id": 0, "agent_type": "resource", "status": "completed"},
                        {"id": 1, "agent_type": "benchmark", "status": "completed"},
                    ],
                },
            },
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "2 completed" in content
        assert "skipped" not in content

    def test_no_plan(self):
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "No plan",
            "custom_fields": {},
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "Plan" not in content

    @pytest.mark.asyncio
    async def test_orchestrator_retires_pending_steps(self):
        from unittest.mock import MagicMock
        from orchestrator.main import run_agent_task

        plan = {
            "current_step": 1,
            "steps": [
                {"id": 0, "agent_type": "analyze", "status": "completed"},
                {"id": 1, "agent_type": "review", "status": "completed"},
                {"id": 2, "agent_type": "benchmark", "status": "pending"},
                {"id": 3, "agent_type": "teardown", "status": "pending"},
            ],
        }

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = mock_agent
        dispatcher.store_url = "http://localhost:8090"
        dispatcher.events = None

        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.json.return_value = {
            "id": "PERF-123",
            "custom_fields": {"execution_plan": plan},
        }

        mock_patch = MagicMock()
        mock_patch.status_code = 200

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_get)
        mock_client.patch = AsyncMock(return_value=mock_patch)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with pytest.MonkeyPatch.context() as mp:
            import httpx
            mp.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)
            await run_agent_task(
                dispatcher,
                "synthesizing_results",
                "PERF-123",
                agent_task_timeout=0,
            )

        assert mock_client.patch.called
        patch_kwargs = mock_client.patch.call_args[1]
        patched_steps = patch_kwargs["json"]["fields"]["execution_plan"]["steps"]
        assert patched_steps[0]["status"] == "completed"
        assert patched_steps[1]["status"] == "completed"
        assert patched_steps[2]["status"] == "skipped"
        assert patched_steps[3]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_orchestrator_handles_none_execution_plan(self):
        from unittest.mock import MagicMock
        from orchestrator.main import run_agent_task

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = mock_agent
        dispatcher.store_url = "http://localhost:8090"
        dispatcher.events = None

        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.json.return_value = {
            "id": "PERF-123",
            "custom_fields": {"execution_plan": None},
        }

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_get)
        mock_client.patch = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with pytest.MonkeyPatch.context() as mp:
            import httpx
            mp.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)
            await run_agent_task(
                dispatcher,
                "synthesizing_results",
                "PERF-123",
                agent_task_timeout=0,
            )

        mock_client.patch.assert_not_called()

