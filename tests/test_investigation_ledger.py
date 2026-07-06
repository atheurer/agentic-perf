"""Tests for the investigation ledger.

Tests the ledger model, custom_fields extraction, append logic,
AgentBase helpers, and universal plan creation in triage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from providers.ledger import (
    LedgerEntry,
    append_ledger_entry,
    get_ledger,
    get_working_hypothesis,
)

# --- LedgerEntry model ---


class TestLedgerEntry:
    def test_defaults(self):
        entry = LedgerEntry(iteration=1)
        assert entry.iteration == 1
        assert entry.plan_steps == []
        assert entry.hypothesis == ""
        assert entry.conclusion == ""
        assert entry.info_gain == 0.0
        assert entry.timestamp is not None

    def test_full_entry(self):
        entry = LedgerEntry(
            iteration=2,
            plan_steps=[3, 4],
            hypothesis="regression at low queue depth",
            params_rationale="prior run showed no effect at high concurrency",
            conclusion="61% degradation confirmed at iodepth=1",
            info_gain=0.61,
        )
        assert entry.iteration == 2
        assert entry.plan_steps == [3, 4]
        assert entry.info_gain == 0.61

    def test_serialization_roundtrip(self):
        entry = LedgerEntry(
            iteration=1,
            plan_steps=[0],
            hypothesis="test hypothesis",
            conclusion="test conclusion",
            info_gain=0.5,
        )
        data = entry.model_dump(mode="json")
        restored = LedgerEntry(**data)
        assert restored.iteration == entry.iteration
        assert restored.plan_steps == entry.plan_steps
        assert restored.hypothesis == entry.hypothesis
        assert restored.info_gain == entry.info_gain


# --- get_ledger ---


class TestGetLedger:
    def test_empty_when_missing(self):
        ledger = get_ledger({})
        assert ledger == []

    def test_empty_list(self):
        ledger = get_ledger({"investigation_ledger": []})
        assert ledger == []

    def test_parses_entries(self):
        cf = {
            "investigation_ledger": [
                {
                    "iteration": 1,
                    "plan_steps": [0],
                    "hypothesis": "h1",
                    "conclusion": "c1",
                    "info_gain": 0.3,
                },
                {
                    "iteration": 2,
                    "plan_steps": [1],
                    "hypothesis": "h2",
                    "conclusion": "c2",
                    "info_gain": 0.6,
                },
            ],
        }
        ledger = get_ledger(cf)
        assert len(ledger) == 2
        assert ledger[0].hypothesis == "h1"
        assert ledger[1].info_gain == 0.6


# --- append_ledger_entry ---


class TestAppendLedgerEntry:
    def test_append_to_empty(self):
        entry = LedgerEntry(
            iteration=1,
            plan_steps=[0],
            hypothesis="initial hypothesis",
        )
        fields = append_ledger_entry({}, entry)
        assert "investigation_ledger" in fields
        assert len(fields["investigation_ledger"]) == 1
        assert fields["investigation_ledger"][0]["iteration"] == 1

    def test_append_preserves_existing(self):
        existing_cf = {
            "investigation_ledger": [
                {
                    "iteration": 1,
                    "plan_steps": [0],
                    "hypothesis": "h1",
                    "conclusion": "c1",
                    "info_gain": 0.3,
                    "params_rationale": "",
                    "timestamp": "2026-06-26T00:00:00Z",
                },
            ],
        }
        entry = LedgerEntry(
            iteration=2,
            plan_steps=[1],
            hypothesis="h2",
            conclusion="c2",
            info_gain=0.6,
        )
        fields = append_ledger_entry(existing_cf, entry)
        ledger = fields["investigation_ledger"]
        assert len(ledger) == 2
        assert ledger[0]["hypothesis"] == "h1"
        assert ledger[1]["hypothesis"] == "h2"

    def test_does_not_mutate_original(self):
        existing_cf = {
            "investigation_ledger": [
                {
                    "iteration": 1,
                    "plan_steps": [0],
                    "hypothesis": "h1",
                    "conclusion": "",
                    "info_gain": 0.0,
                    "params_rationale": "",
                    "timestamp": "2026-06-26T00:00:00Z",
                },
            ],
        }
        original_len = len(existing_cf["investigation_ledger"])
        entry = LedgerEntry(iteration=2)
        append_ledger_entry(existing_cf, entry)
        assert len(existing_cf["investigation_ledger"]) == original_len


# --- get_working_hypothesis ---


class TestGetWorkingHypothesis:
    def test_from_ledger(self):
        cf = {
            "hypothesis": "triage hypothesis",
            "investigation_ledger": [
                {"hypothesis": "h1"},
                {"hypothesis": "h2 refined"},
            ],
        }
        assert get_working_hypothesis(cf) == "h2 refined"

    def test_falls_back_to_triage(self):
        cf = {"hypothesis": "triage hypothesis"}
        assert get_working_hypothesis(cf) == "triage hypothesis"

    def test_empty_returns_empty(self):
        assert get_working_hypothesis({}) == ""


# --- AgentBase helpers ---


class TestAgentBaseHelpers:
    @pytest.mark.asyncio
    async def test_get_investigation_ledger(self):
        from agents.base import AgentBase
        from providers.llm.mock import MockLLMProvider

        # Create a minimal concrete subclass
        class _Stub(AgentBase):
            def _system_prompt(self):
                return ""

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        agent = _Stub(
            agent_name="test",
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
                        "investigation_ledger": [
                            {
                                "iteration": 1,
                                "plan_steps": [0],
                                "hypothesis": "test",
                            },
                        ],
                    },
                },
                raise_for_status=lambda: None,
            ),
        )
        ledger = await agent._get_investigation_ledger("PERF-TEST")
        assert len(ledger) == 1
        assert ledger[0]["hypothesis"] == "test"

    @pytest.mark.asyncio
    async def test_append_ledger_entry(self):
        from agents.base import AgentBase
        from providers.llm.mock import MockLLMProvider

        class _Stub(AgentBase):
            def _system_prompt(self):
                return ""

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        agent = _Stub(
            agent_name="test",
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
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
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        await agent._append_ledger_entry(
            "PERF-TEST",
            iteration=1,
            plan_steps=[0],
            hypothesis="initial hypothesis",
            conclusion="baseline established",
            info_gain=0.3,
        )

        # Verify the patch call
        patch_calls = agent._client.patch.call_args_list
        assert len(patch_calls) == 1
        body = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )
        ledger = body["fields"]["investigation_ledger"]
        assert len(ledger) == 1
        assert ledger[0]["iteration"] == 1
        assert ledger[0]["hypothesis"] == "initial hypothesis"
        assert ledger[0]["plan_steps"] == [0]


# --- Universal plan creation in triage ---


class TestUniversalPlan:
    @pytest.mark.asyncio
    async def test_single_benchmark_gets_plan(self):
        """Triage creates a 1-step plan for single-benchmark
        requests (no execution_plan in LLM result)."""
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
                        "hypothesis": "baseline CPU perf",
                        "benchmark_suite": "stress-ng",
                        "parsed_specs": {},
                        "roles": ["client"],
                        "min_hosts": 1,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find the patch call that sets fields
        patch_calls = [
            c for c in agent._client.patch.call_args_list if "fields" in str(c)
        ]
        assert len(patch_calls) >= 1
        body = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )
        fields = body["fields"]
        assert "execution_plan" in fields
        plan = fields["execution_plan"]
        assert plan["current_step"] == 0
        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["agent_type"] == "benchmark"
        assert plan["steps"][0]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_multi_step_plan_preserved(self):
        """Triage preserves multi-step plans from LLM."""
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
                        "hypothesis": "compare thread counts",
                        "benchmark_suite": "uperf",
                        "parsed_specs": {},
                        "roles": ["client", "server"],
                        "min_hosts": 2,
                        "execution_plan": [
                            {
                                "agent_type": "benchmark",
                                "params": {"label": "1-thread"},
                            },
                            {
                                "agent_type": "benchmark",
                                "params": {"label": "8-threads"},
                            },
                            {
                                "agent_type": "review",
                                "params": {},
                            },
                        ],
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        patch_calls = [
            c for c in agent._client.patch.call_args_list if "fields" in str(c)
        ]
        body = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )
        plan = body["fields"]["execution_plan"]
        assert len(plan["steps"]) == 3
        assert plan["steps"][0]["params"]["label"] == "1-thread"
        assert plan["steps"][2]["agent_type"] == "review"
