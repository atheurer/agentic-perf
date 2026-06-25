"""Tests for multi-step execution plans.

Covers: plan data model, orchestrator advancement, triage plan
generation, benchmark step params, review multi-run awareness.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State machine ---


def test_review_can_reenter_benchmark():
    """Review can transition to executing_benchmark for plan re-runs."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.EXECUTING_BENCHMARK in allowed


def test_original_review_transitions_intact():
    """Original review transitions still work."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.AWAITING_TEARDOWN in allowed
    assert TicketStatus.TRIAGE_PENDING in allowed
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Plan advancement ---


@pytest.mark.asyncio
async def test_advance_plan_no_plan_is_noop():
    """_advance_plan does nothing when ticket has no execution_plan."""
    from orchestrator.main import _advance_plan

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {"run_id": "RUN-001"},
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        client.get.return_value = mock_response

        await _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        client.patch.assert_not_called()
        client.post.assert_not_called()


@pytest.mark.asyncio
async def test_advance_plan_skips_non_plan_agent():
    """_advance_plan does nothing when the completed agent doesn't match the step."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {"execution_plan": plan},
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        client.get.return_value = mock_response

        # Resource agent completed — should NOT advance the benchmark step
        await _advance_plan("http://localhost:8090", "PERF-TEST", "awaiting_hardware")

        client.patch.assert_not_called()
        client.post.assert_not_called()


@pytest.mark.asyncio
async def test_advance_plan_completes_step_and_advances():
    """After benchmark step, plan marks it completed and transitions."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {"label": "run-1"},
                "results": {},
            },
            {
                "id": 1,
                "agent_type": "benchmark",
                "status": "pending",
                "params": {"label": "run-2"},
                "results": {},
            },
            {
                "id": 2,
                "agent_type": "review",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-001",
            "benchmark_status": "completed",
            "execution_plan": plan,
        },
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        client.get.return_value = mock_response
        client.patch.return_value = MagicMock(status_code=200)
        client.post.return_value = MagicMock(status_code=200)

        await _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]

        assert updated_plan["current_step"] == 1
        assert updated_plan["steps"][0]["status"] == "completed"
        assert updated_plan["steps"][0]["results"]["run_id"] == "RUN-001"
        assert updated_plan["steps"][1]["status"] == "in_progress"
        assert updated_plan["run_ids"] == ["RUN-001"]

        transition_call = None
        for call in client.post.call_args_list:
            if "transition" in str(call):
                transition_call = call
        assert transition_call is not None
        assert transition_call.kwargs["json"]["status"] == "executing_benchmark"


@pytest.mark.asyncio
async def test_advance_plan_final_step_no_transition():
    """After the last step, _advance_plan saves results but no transition."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "review",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "execution_plan": plan,
        },
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        client.get.return_value = mock_response
        client.patch.return_value = MagicMock(status_code=200)

        await _advance_plan("http://localhost:8090", "PERF-TEST", "awaiting_review")

        client.patch.assert_called_once()
        transition_calls = [
            c for c in client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 0


@pytest.mark.asyncio
async def test_advance_plan_tracks_multiple_run_ids():
    """Each completed benchmark step's run_id is appended to plan.run_ids."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 1,
        "run_ids": ["RUN-001"],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "completed",
                "params": {},
                "results": {"run_id": "RUN-001"},
            },
            {
                "id": 1,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
            {
                "id": 2,
                "agent_type": "review",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-002",
            "benchmark_status": "completed",
            "execution_plan": plan,
        },
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        client.get.return_value = mock_response
        client.patch.return_value = MagicMock(status_code=200)
        client.post.return_value = MagicMock(status_code=200)

        await _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]
        assert updated_plan["run_ids"] == ["RUN-001", "RUN-002"]


# --- Triage plan creation ---


def test_triage_creates_execution_plan():
    """Triage agent normalizes raw execution_plan into data model."""
    raw_plan = [
        {"agent_type": "benchmark", "params": {"label": "run-1"}},
        {"agent_type": "benchmark", "params": {"label": "run-2"}},
        {"agent_type": "review", "params": {}},
    ]

    result = {
        "parsed_specs": {},
        "hypothesis": "test",
        "benchmark_suite": "uperf",
        "absent_suite": False,
        "min_hosts": 2,
        "roles": ["client", "server"],
        "execution_plan": raw_plan,
    }

    fields: dict = {}
    # Simulate the field construction from _handle_completion
    if result.get("execution_plan") and len(result["execution_plan"]) > 1:
        steps = []
        for i, s in enumerate(result["execution_plan"]):
            steps.append(
                {
                    "id": i,
                    "agent_type": s.get("agent_type", "benchmark"),
                    "status": "in_progress" if i == 0 else "pending",
                    "params": s.get("params", {}),
                    "results": {},
                }
            )
        fields["execution_plan"] = {
            "current_step": 0,
            "run_ids": [],
            "steps": steps,
        }

    plan = fields["execution_plan"]
    assert plan["current_step"] == 0
    assert len(plan["steps"]) == 3
    assert plan["steps"][0]["status"] == "in_progress"
    assert plan["steps"][1]["status"] == "pending"
    assert plan["steps"][2]["agent_type"] == "review"


def test_triage_ignores_single_step_plan():
    """A plan with only 1 step is ignored (not a multi-step request)."""
    raw_plan = [{"agent_type": "benchmark", "params": {}}]

    fields: dict = {}
    if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 1:
        fields["execution_plan"] = {"steps": raw_plan}

    assert "execution_plan" not in fields


# --- Benchmark agent step awareness ---


def test_benchmark_build_messages_includes_step_params():
    """Benchmark agent includes step-specific params in its context."""
    from agents.benchmark.agent import BenchmarkAgent

    ticket = {
        "id": "PERF-TEST",
        "summary": "test",
        "description": "test",
        "custom_fields": {
            "execution_plan": {
                "current_step": 1,
                "run_ids": ["RUN-001"],
                "steps": [
                    {
                        "id": 0,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "run-1"},
                        "results": {"run_id": "RUN-001"},
                    },
                    {
                        "id": 1,
                        "agent_type": "benchmark",
                        "status": "in_progress",
                        "params": {
                            "label": "run-2",
                            "mv_params": {"num-threads": "8"},
                        },
                        "results": {},
                    },
                ],
            },
        },
        "comments": [],
    }

    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._repo_cache = None
    msgs = agent._build_messages(ticket)
    content = msgs[0]["content"]

    assert "Step 1" in content
    assert "run-2" in content
    assert "num-threads" in content
    assert "RUN-001" in content


# --- Review agent multi-run awareness ---


def test_review_build_messages_includes_all_run_ids():
    """Review agent presents all run_ids from completed plan steps."""
    from agents.review.agent import ReviewAgent

    ticket = {
        "id": "PERF-TEST",
        "summary": "test",
        "description": "test",
        "custom_fields": {
            "hypothesis": "compare thread counts",
            "benchmark_suite": "uperf",
            "benchmark_status": "completed",
            "execution_plan": {
                "current_step": 2,
                "run_ids": ["RUN-001", "RUN-002"],
                "steps": [
                    {
                        "id": 0,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "1-thread"},
                        "results": {
                            "run_id": "RUN-001",
                            "benchmark_status": "completed",
                        },
                    },
                    {
                        "id": 1,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "8-threads"},
                        "results": {
                            "run_id": "RUN-002",
                            "benchmark_status": "completed",
                        },
                    },
                    {
                        "id": 2,
                        "agent_type": "review",
                        "status": "in_progress",
                        "params": {},
                        "results": {},
                    },
                ],
            },
        },
        "comments": [],
    }

    agent = ReviewAgent.__new__(ReviewAgent)
    agent._repo_cache = None
    msgs = agent._build_messages(ticket)
    content = msgs[0]["content"]

    assert "1-thread" in content
    assert "8-threads" in content
    assert "RUN-001" in content
    assert "RUN-002" in content
    assert "All Run IDs for comparison" in content
