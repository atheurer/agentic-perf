"""Tests for the Gathering Context agent (dedup gate).

Tests the agent's dedup decision logic: matching against open
Investigation Records, state machine transitions, and field
persistence.
"""

from __future__ import annotations

import pytest

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State machine transitions ---


class TestStateTransitions:
    """Verify gathering_context transitions in the state machine."""

    def test_gathering_context_to_planning(self):
        """Can transition to planning_investigation (no match)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.PLANNING_INVESTIGATION in valid

    def test_gathering_context_to_retrospective(self):
        """Can transition to retrospective_pending (dedup match)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.RETROSPECTIVE_PENDING in valid

    def test_gathering_context_to_hitl(self):
        """Can transition to awaiting_customer_guidance."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in valid

    def test_gathering_context_not_to_closed(self):
        """Cannot transition directly to closed (must go through
        retrospective first)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.CLOSED not in valid

    def test_triage_to_gathering_context(self):
        """Triage can route to gathering_context."""
        valid = VALID_TRANSITIONS[TicketStatus.TRIAGE_PENDING]
        assert TicketStatus.GATHERING_CONTEXT in valid


# --- Agent construction ---


class TestAgentConstruction:
    """Verify agent can be constructed and has correct attributes."""

    def test_agent_name(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent.agent_name == "gathering-context-agent"

    def test_system_prompt_content(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        prompt = agent._system_prompt({})
        assert "Investigation Records" in prompt
        assert "MATCH_FOUND" in prompt
        assert "NO_MATCH" in prompt


# --- Message building ---


class TestMessageBuilding:
    """Verify _build_messages extracts anomaly context."""

    def test_with_anomaly_context(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Storage regression on NXP",
            "custom_fields": {
                "anomaly_context": {
                    "subsystem": "storage_io",
                    "metric": "iops_4k_randread",
                    "direction": "degrading",
                    "platform": "NXP_S32G",
                    "magnitude": "-31%",
                },
                "hypothesis": "virtio-blk driver regression",
            },
        }
        msgs = agent._build_messages(ticket)
        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert "storage_io" in content
        assert "iops_4k_randread" in content
        assert "NXP_S32G" in content
        assert "-31%" in content
        assert "virtio-blk" in content

    def test_without_anomaly_context(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Run fio test",
            "custom_fields": {},
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "No anomaly context found" in content
        assert "NO_MATCH" in content


# --- Handle completion ---


class TestHandleCompletion:
    """Verify _handle_completion transitions correctly."""

    @pytest.mark.asyncio
    async def test_no_match_transitions_to_planning(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
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
                    name="submit_gathering_context_result",
                    input={
                        "decision": "NO_MATCH",
                        "notes": "No open records for storage_io",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "planning_investigation"

    @pytest.mark.asyncio
    async def test_match_transitions_to_retrospective(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
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
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                        "match_confidence": 0.92,
                        "match_rationale": (
                            "Same subsystem and metric, similar "
                            "magnitude on related platform"
                        ),
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "retrospective_pending"

    @pytest.mark.asyncio
    async def test_match_persists_dedup_result(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
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
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                        "match_confidence": 0.92,
                        "match_rationale": "Same regression",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check fields update
        patch_calls = [
            c for c in agent._client.patch.call_args_list if "fields" in str(c)
        ]
        assert len(patch_calls) == 1
        fields = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )["fields"]
        dedup = fields["dedup_result"]
        assert dedup["decision"] == "MATCH_FOUND"
        assert dedup["matched_investigation_id"] == "RCA-998"
        assert dedup["match_confidence"] == 0.92


# --- Triage routing ---


class TestTriageRouting:
    """Verify triage routes based on anomaly_context presence."""

    @pytest.mark.asyncio
    async def test_anomaly_context_routes_to_gathering(self):
        """Triage routes to gathering_context when anomaly_context
        is present on the ticket."""
        from unittest.mock import AsyncMock

        from agents.triage.agent import TriageAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = TriageAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            skill_provider=MockSkillProvider(),
        )
        agent._client = AsyncMock()

        # Mock: _update_fields and _add_comment succeed
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
        # Mock: _get_ticket returns ticket WITH anomaly_context
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "anomaly_context": {
                            "subsystem": "storage_io",
                            "metric": "iops_4k_randread",
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_triage_result",
                    input={
                        "hypothesis": "storage regression",
                        "benchmark_suite": "fio",
                        "parsed_specs": {},
                        "roles": ["client"],
                        "min_hosts": 1,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find the transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "gathering_context"

    @pytest.mark.asyncio
    async def test_no_anomaly_context_routes_to_hardware(self):
        """Triage routes to awaiting_hardware when no
        anomaly_context is present (ad-hoc ticket)."""
        from unittest.mock import AsyncMock

        from agents.triage.agent import TriageAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = TriageAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            skill_provider=MockSkillProvider(),
        )
        agent._client = AsyncMock()
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
        # Mock: _get_ticket returns ticket WITHOUT anomaly_context
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {},
                },
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_triage_result",
                    input={
                        "hypothesis": "baseline network",
                        "benchmark_suite": "uperf",
                        "parsed_specs": {},
                        "roles": ["client", "server"],
                        "min_hosts": 2,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "awaiting_hardware"


# --- Dispatcher integration ---


class TestDispatcherIntegration:
    """Verify dispatcher creates the real agent, not a stub."""

    def test_dispatcher_creates_gathering_context_agent(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from orchestrator.dispatcher import Dispatcher
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MockLLMProvider(),
            skill_provider=MockSkillProvider(),
        )
        agent = dispatcher.create_agent("gathering_context")
        assert isinstance(agent, GatheringContextAgent)
        assert agent.agent_name == "gathering-context-agent"
