"""Tests for the Evaluate agent.

Tests convergence assessment, loop-back decisions, ledger updates,
benchmark routing, and dispatcher integration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State machine transitions ---


class TestStateTransitions:
    def test_evaluating_to_planning(self):
        valid = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
        assert TicketStatus.PLANNING_INVESTIGATION in valid

    def test_evaluating_to_provision(self):
        valid = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
        assert TicketStatus.AWAITING_PROVISION in valid

    def test_evaluating_to_synthesis(self):
        valid = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
        assert TicketStatus.SYNTHESIZING_RESULTS in valid

    def test_evaluating_to_hitl(self):
        valid = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in valid

    def test_benchmark_to_evaluating(self):
        valid = VALID_TRANSITIONS[TicketStatus.EXECUTING_BENCHMARK]
        assert TicketStatus.EVALUATING_CONVERGENCE in valid


# --- Agent construction ---


class TestAgentConstruction:
    def test_agent_name(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent.agent_name == "evaluate-agent"

    def test_unlimited_iterations(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent.max_iterations == 0

    def test_system_prompt_content(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        prompt = agent._system_prompt({})
        assert "Isolation" in prompt
        assert "Entropy Stall" in prompt
        assert "Expected Regression" in prompt
        assert "loop_plan" in prompt
        assert "converged" in prompt


# --- Message building ---


class TestMessageBuilding:
    def test_includes_anomaly_context(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
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
                "hypothesis": "virtio-blk regression",
                "investigation_ledger": [
                    {
                        "iteration": 1,
                        "hypothesis": "high concurrency",
                        "conclusion": "no effect at iodepth=32",
                        "info_gain": 0.0,
                    },
                ],
                "execution_plan": {
                    "steps": [
                        {
                            "id": 0,
                            "agent_type": "benchmark",
                            "status": "completed",
                            "params": {"iodepth": 32},
                            "results": {
                                "run_id": "RUN-001",
                                "benchmark_status": "completed",
                            },
                        },
                    ],
                },
                "run_id": "RUN-001",
                "benchmark_status": "completed",
            },
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "storage_io" in content
        assert "virtio-blk" in content
        assert "no effect at iodepth=32" in content
        assert "RUN-001" in content

    def test_no_ledger_first_evaluation(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "test",
            "custom_fields": {
                "hypothesis": "test hypothesis",
            },
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "first evaluation" in content.lower()


# --- Deterministic check ---


class TestDeterministicCheck:
    def test_max_iterations_detected(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        cf = {
            "convergence_criteria": {"max_iterations": 2},
            "iteration_results": [
                {"iteration": 0, "metric_value": 100.0},
                {"iteration": 1, "metric_value": 99.0},
            ],
        }
        outcome = agent._check_deterministic(cf)
        assert "MAX_ITERATIONS" in outcome

    def test_no_criteria_returns_empty(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent._check_deterministic({}) == ""


# --- Handle completion ---


class TestHandleCompletion:
    @pytest.mark.asyncio
    async def test_converged_transitions_to_synthesis(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
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
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "id": 0,
                                    "status": "completed",
                                    "results": {},
                                },
                            ],
                        },
                        "investigation_ledger": [],
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
                    name="submit_evaluation_result",
                    input={
                        "decision": "converged",
                        "convergence_gate": "isolation",
                        "confidence": 0.95,
                        "root_cause_summary": "virtio-blk regression",
                        "info_gain": 0.92,
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
        assert body["status"] == "synthesizing_results"

    @pytest.mark.asyncio
    async def test_loop_plan_transitions_to_planning(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
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
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "id": 0,
                                    "status": "completed",
                                    "results": {},
                                },
                            ],
                        },
                        "investigation_ledger": [],
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
                    name="submit_evaluation_result",
                    input={
                        "decision": "loop_plan",
                        "updated_hypothesis": "try iodepth=1",
                        "params_rationale": "high concurrency masked defect",
                        "next_params": '{"iodepth": 1}',
                        "info_gain": 0.0,
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
        assert body["status"] == "planning_investigation"

    @pytest.mark.asyncio
    async def test_loop_plan_appends_plan_step(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
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
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "id": 0,
                                    "status": "completed",
                                    "results": {},
                                },
                            ],
                        },
                        "investigation_ledger": [],
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
                    name="submit_evaluation_result",
                    input={
                        "decision": "loop_plan",
                        "updated_hypothesis": "try iodepth=1",
                        "next_params": '{"iodepth": 1}',
                        "info_gain": 0.0,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find the patch call that updates execution_plan
        patch_calls = [
            c for c in agent._client.patch.call_args_list if "execution_plan" in str(c)
        ]
        assert len(patch_calls) >= 1
        body = patch_calls[-1].kwargs.get(
            "json",
            patch_calls[-1][1].get("json", {}),
        )
        plan = body["fields"]["execution_plan"]
        assert len(plan["steps"]) == 2
        assert plan["steps"][1]["params"] == {"iodepth": 1}
        assert plan["steps"][1]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_stalled_transitions_to_synthesis(self):
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
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
                        "execution_plan": {
                            "steps": [
                                {"id": 0, "status": "completed"},
                            ],
                        },
                        "investigation_ledger": [],
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
                    name="submit_evaluation_result",
                    input={
                        "decision": "stalled",
                        "convergence_gate": "entropy_stall",
                        "confidence": 0.3,
                        "info_gain": 0.02,
                        "notes": "No new information after 3 iterations",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "synthesizing_results"

    @pytest.mark.asyncio
    async def test_populates_iteration_results(self):
        """Evaluate agent writes iteration_results for
        deterministic convergence checks."""
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        # Return ticket with an existing ledger entry
        # (simulating post-append state)
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "execution_plan": {
                            "steps": [
                                {
                                    "id": 0,
                                    "status": "completed",
                                },
                            ],
                        },
                        "investigation_ledger": [
                            {
                                "iteration": 1,
                                "info_gain": 0.5,
                                "conclusion": "narrowed to X",
                            },
                        ],
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
                    name="submit_evaluation_result",
                    input={
                        "decision": "converged",
                        "convergence_gate": "isolation",
                        "confidence": 0.95,
                        "info_gain": 0.5,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find the patch call with iteration_results
        patch_calls = [
            c
            for c in agent._client.patch.call_args_list
            if "iteration_results" in str(c)
        ]
        assert len(patch_calls) >= 1
        body = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )
        ir = body["fields"]["iteration_results"]
        assert len(ir) == 1
        assert ir[0]["iteration"] == 1
        assert ir[0]["info_gain"] == 0.5

    @pytest.mark.asyncio
    async def test_deterministic_overrides_llm_loop(
        self,
    ):
        """When a deterministic gate fires, code overrides
        the LLM's decision to loop back."""
        from agents.evaluate.agent import EvaluateAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = EvaluateAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        # Simulate deterministic MAX_ITERATIONS having fired
        agent._deterministic_outcome = "MAX_ITERATIONS \u2014 reached 5 iterations"
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "execution_plan": {
                            "steps": [
                                {
                                    "id": 0,
                                    "status": "completed",
                                },
                            ],
                        },
                        "investigation_ledger": [],
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

        # LLM wants to loop, but deterministic says stop
        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_evaluation_result",
                    input={
                        "decision": "loop_plan",
                        "updated_hypothesis": "try more",
                        "info_gain": 0.1,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Should transition to synthesis, not planning
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "synthesizing_results"


# --- Benchmark routing ---


class TestBenchmarkRouting:
    @pytest.mark.asyncio
    async def test_investigation_ticket_routes_to_evaluate(self):
        """Benchmark agent routes to evaluating_convergence
        for investigation tickets."""
        from agents.benchmark.agent import BenchmarkAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = BenchmarkAgent(
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
        # Ticket has investigation_ledger → investigation mode
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "investigation_ledger": [
                            {"iteration": 1},
                        ],
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
                    name="submit_benchmark_result",
                    input={
                        "run_id": "RUN-001",
                        "benchmark_status": "completed",
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
        assert body["status"] == "evaluating_convergence"

    @pytest.mark.asyncio
    async def test_adhoc_ticket_routes_to_review(self):
        """Benchmark agent routes to awaiting_review for
        ad-hoc tickets (no investigation context)."""
        from agents.benchmark.agent import BenchmarkAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = BenchmarkAgent(
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
        # No investigation_ledger or anomaly_context
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
                    name="submit_benchmark_result",
                    input={
                        "run_id": "RUN-002",
                        "benchmark_status": "completed",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "awaiting_review"


# --- Dispatcher integration ---


class TestDispatcherIntegration:
    def test_dispatcher_creates_evaluate_agent(self):
        from agents.evaluate.agent import EvaluateAgent
        from orchestrator.dispatcher import Dispatcher
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MockLLMProvider(),
            skill_provider=MockSkillProvider(),
        )
        agent = dispatcher.create_agent("evaluating_convergence")
        assert isinstance(agent, EvaluateAgent)
        assert agent.agent_name == "evaluate-agent"
        assert agent.max_iterations == 0
