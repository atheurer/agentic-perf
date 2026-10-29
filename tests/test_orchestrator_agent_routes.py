from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agents.analyze.agent import AnalyzeAgent
from agents.base import AgentBase
from agents.benchmark.agent import BenchmarkAgent
from agents.evaluate.agent import EvaluateAgent
from agents.gathering_context.agent import GatheringContextAgent
from agents.review.agent import ReviewAgent
from providers.llm.base import LLMResponse
from providers.llm.mock import MockLLMProvider
from providers.tracing import TraceDeliveryError
from state_store.models import VALID_TRANSITIONS, TicketStatus


class RouteRuntime:
    def __init__(self) -> None:
        self.ticket: dict[str, Any] = {}
        self.transitions: list[str] = []
        self.clients: list[httpx.AsyncClient] = []
        self.requests: list[httpx.Request] = []
        self.fail_after_transition = False
        self.reject_guidance = False
        self.fail_after_plan_patch = False
        self.fail_after_benchmark_patch = False
        self.fail_snapshot_read = False
        self.transport = httpx.MockTransport(self.handle)
        self.dispatcher = MagicMock()
        self.dispatcher._claim_ids = {"PERF-route": "claim-route"}
        self.dispatcher._trace_contexts = {}
        self.dispatcher._session_id = "session-route"
        self.dispatcher._fencing_epoch = 1
        self.dispatcher.store_url = "http://store"
        self.dispatcher.events = None
        self.dispatcher.is_deposed.return_value = False
        self.dispatcher.get_skill_provider_for_ticket = AsyncMock(
            return_value=MagicMock()
        )
        self.dispatcher.mark_done = AsyncMock()

    def prepare(self, agent_types: list[str], status: str) -> None:
        self.ticket = {
            "id": "PERF-route",
            "status": status,
            "custom_fields": {
                "execution_plan": {
                    "current_step": 0,
                    "steps": [
                        {
                            "id": index,
                            "agent_type": agent_type,
                            "status": "in_progress" if index == 0 else "pending",
                            "params": {},
                        }
                        for index, agent_type in enumerate(agent_types)
                    ],
                }
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content) if request.content else {}
        if request.method == "GET" and self.fail_snapshot_read:
            self.fail_snapshot_read = False
            raise httpx.ConnectError("initial ticket read unavailable", request=request)
        if request.method == "PATCH" and request.url.path.endswith("/fields"):
            self.ticket["custom_fields"].update(deepcopy(payload["fields"]))
            if self.fail_after_benchmark_patch and "run_id" in payload["fields"]:
                self.fail_after_benchmark_patch = False
                raise TraceDeliveryError(
                    "benchmark result committed before trace failure"
                )
            if self.fail_after_plan_patch and "execution_plan" in payload["fields"]:
                self.fail_after_plan_patch = False
                raise TraceDeliveryError("plan mutation committed before trace failure")
        elif request.method == "POST" and request.url.path.endswith("/transition"):
            status = payload["status"]
            if status == "awaiting_customer_guidance" and self.reject_guidance:
                return httpx.Response(503, request=request)
            current = TicketStatus(self.ticket["status"])
            assert (
                status == current.value
                or TicketStatus(status) in VALID_TRANSITIONS[current]
            )
            self.ticket["status"] = status
            self.transitions.append(status)
            if self.fail_after_transition:
                self.fail_after_transition = False
                raise TraceDeliveryError("transition committed before trace failure")
        return httpx.Response(200, json=deepcopy(self.ticket), request=request)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=self.transport, **kwargs)
        self.clients.append(client)
        return client

    async def complete(self, agent_class: type[AgentBase], result: dict) -> None:
        import orchestrator.main as main

        agent = agent_class(MockLLMProvider(), "http://store")
        response = LLMResponse(text=json.dumps(result))

        async def run(ticket_id: str) -> None:
            await agent._handle_completion(ticket_id, response)

        agent.run = run
        self.dispatcher.create_agent.return_value = agent
        await main.run_agent_task(
            self.dispatcher, self.ticket["status"], self.ticket["id"]
        )


@pytest.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RouteRuntime]:
    import agents.base as base
    import orchestrator.main as main

    runtime = RouteRuntime()
    monkeypatch.setattr(base, "AuditedAsyncHTTPClient", runtime.client)
    monkeypatch.setattr(main, "AuditedAsyncHTTPClient", runtime.client)
    monkeypatch.setattr(main, "_pending_retry_writes", {})
    monkeypatch.setattr(main, "_retry_write_locks", {})
    yield runtime
    for client in runtime.clients:
        await client.aclose()


async def test_real_gathering_agent_advances_data_only_plan(
    runtime: RouteRuntime,
) -> None:
    runtime.prepare(["gathering_context", "analyze", "review"], "gathering_context")

    await runtime.complete(GatheringContextAgent, {"decision": "NO_MATCH"})
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert runtime.transitions == ["analyzing"]
    assert plan["current_step"] == 1
    assert plan["steps"][1]["status"] == "in_progress"

    await runtime.complete(AnalyzeAgent, {"conclusive": True, "finding": "identified"})
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 2
    assert runtime.ticket["status"] == "awaiting_review"

    await runtime.complete(ReviewAgent, {"verdict": "hypothesis_confirmed"})
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 3
    assert all(step["status"] == "completed" for step in plan["steps"])
    assert runtime.transitions == ["analyzing", "awaiting_review", "awaiting_teardown"]


async def test_dedup_match_preserves_retrospective_route(
    runtime: RouteRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.prepare(["gathering_context", "analyze", "review"], "gathering_context")
    provider = MagicMock()
    provider.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "providers.investigation.registry.create_record_provider", lambda: provider
    )

    await runtime.complete(
        GatheringContextAgent,
        {"decision": "MATCH_FOUND", "matched_investigation_id": "IR-existing"},
    )

    assert runtime.transitions == ["retrospective_pending"]
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == len(plan["steps"])
    assert plan["steps"][0]["status"] == "completed"
    assert all(step["status"] == "skipped" for step in plan["steps"][1:])


@pytest.mark.parametrize("decision", ["converged", "stalled"])
async def test_real_evaluation_preserves_synthesis_route(
    runtime: RouteRuntime, decision: str
) -> None:
    runtime.prepare(
        ["evaluate", "analyze", "review", "synthesis"], "evaluating_convergence"
    )

    await runtime.complete(EvaluateAgent, {"decision": decision})

    assert runtime.transitions == ["synthesizing_results"]
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 3
    assert [step["status"] for step in plan["steps"]] == [
        "completed",
        "skipped",
        "skipped",
        "in_progress",
    ]


@pytest.mark.parametrize(
    ("decision", "status", "agent_type"),
    [
        ("loop_analyze", "analyzing", "analyze"),
        ("loop_plan", "planning_investigation", "benchmark"),
    ],
)
async def test_real_evaluation_preserves_appended_iteration(
    runtime: RouteRuntime, decision: str, status: str, agent_type: str
) -> None:
    runtime.prepare(["evaluate", "analyze", "review"], "evaluating_convergence")
    runtime.ticket["custom_fields"]["analysis_result"] = {"conclusive": False}

    await runtime.complete(
        EvaluateAgent, {"decision": decision, "next_params": '{"label": "new-run"}'}
    )

    assert runtime.transitions == [status]
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 3
    assert plan["steps"][3]["agent_type"] == agent_type
    assert plan["steps"][3]["status"] == "in_progress"


async def test_real_evaluation_keeps_dynamic_route_without_matching_plan_step(
    runtime: RouteRuntime,
) -> None:
    runtime.prepare(["evaluate", "analyze", "review"], "evaluating_convergence")

    await runtime.complete(EvaluateAgent, {"decision": "loop_provision"})

    assert runtime.transitions == ["awaiting_provision"]
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == len(plan["steps"])
    assert plan["steps"][0]["status"] == "completed"


async def test_evaluation_guidance_preserves_incomplete_plan(
    runtime: RouteRuntime,
) -> None:
    runtime.prepare(["evaluate", "analyze", "review"], "evaluating_convergence")

    await runtime.complete(EvaluateAgent, {"decision": "loop_analyze"})

    assert runtime.transitions == ["awaiting_customer_guidance"]
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 0
    assert plan["steps"][0]["status"] == "in_progress"


@pytest.mark.parametrize("reject_guidance", [False, True])
async def test_post_mutation_trace_failure_blocks_stale_plan_dispatch(
    runtime: RouteRuntime, reject_guidance: bool
) -> None:
    from orchestrator.retry_guard import retry_is_suppressed

    runtime.prepare(["gathering_context", "analyze", "review"], "gathering_context")
    runtime.fail_after_transition = True
    runtime.reject_guidance = reject_guidance

    await runtime.complete(GatheringContextAgent, {"decision": "NO_MATCH"})

    assert runtime.transitions[0] == "analyzing"
    plan = runtime.ticket["custom_fields"]["execution_plan"]
    assert plan["current_step"] == 0
    assert plan["steps"][0]["status"] == "in_progress"
    if reject_guidance:
        assert runtime.ticket["status"] == "analyzing"
        assert retry_is_suppressed(
            runtime.ticket["custom_fields"], "dispatch", "analyzing", now=0
        )
    else:
        assert runtime.ticket["status"] == "awaiting_customer_guidance"


@pytest.mark.parametrize("fail_snapshot_read", [False, True])
async def test_trace_failure_after_plan_patch_pauses_even_without_status_change(
    runtime: RouteRuntime, fail_snapshot_read: bool
) -> None:
    runtime.prepare(["analyze", "review"], "analyzing")
    runtime.fail_after_plan_patch = True
    runtime.fail_snapshot_read = fail_snapshot_read

    await runtime.complete(AnalyzeAgent, {"conclusive": True})

    assert runtime.ticket["custom_fields"]["execution_plan"]["current_step"] == 1
    assert runtime.ticket["status"] == "awaiting_customer_guidance"


@pytest.mark.parametrize("reject_guidance", [False, True])
async def test_benchmark_result_trace_failure_blocks_replay_without_plan_progress(
    runtime: RouteRuntime, reject_guidance: bool
) -> None:
    from orchestrator.retry_guard import RETRY_STATE_FIELD, retry_is_suppressed

    runtime.prepare(["benchmark", "review"], "executing_benchmark")
    plan_before = deepcopy(runtime.ticket["custom_fields"]["execution_plan"])
    runtime.ticket["custom_fields"].update(
        {"run_id": "previous-run", "output_dirs": ["/results/previous-run"]}
    )
    runtime.fail_after_benchmark_patch = True
    runtime.reject_guidance = reject_guidance

    await runtime.complete(
        BenchmarkAgent,
        {
            "run_id": "committed-run",
            "benchmark_status": "completed",
            "output_dir": "/results/committed-run",
            "run_file_used": {"benchmarks": [{"name": "test", "samples": [1, 2]}]},
        },
    )

    fields = runtime.ticket["custom_fields"]
    assert fields["run_id"] == "committed-run"
    assert fields["benchmark_status"] == "completed"
    assert fields["output_dir"] == "/results/committed-run"
    assert fields["output_dirs"] == ["/results/previous-run", "/results/committed-run"]
    assert fields["run_file_used"]["benchmarks"][0]["samples"] == [1, 2]
    assert fields["execution_plan"] == plan_before
    retry = fields[RETRY_STATE_FIELD]["dispatch"]
    assert retry["status"] == "executing_benchmark"
    assert retry["exhausted"] is True
    assert retry["next_retry_at"] is None
    assert retry_is_suppressed(fields, "dispatch", "executing_benchmark", now=0)
    assert retry_is_suppressed(fields, "dispatch", "executing_benchmark", now=1e12)

    guard_patch = next(
        index
        for index, request in enumerate(runtime.requests)
        if request.method == "PATCH"
        and RETRY_STATE_FIELD in json.loads(request.content)["fields"]
    )
    guidance_request = next(
        index
        for index, request in enumerate(runtime.requests)
        if request.url.path.endswith("/transition")
    )
    assert guard_patch < guidance_request
    if reject_guidance:
        assert runtime.ticket["status"] == "executing_benchmark"
        assert runtime.transitions == []
    else:
        assert runtime.ticket["status"] == "awaiting_customer_guidance"
        assert runtime.transitions == ["awaiting_customer_guidance"]
