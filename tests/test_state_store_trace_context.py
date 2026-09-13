from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from providers.tracing import new_trace_context, trace_headers
from state_store.main import create_app


@pytest.mark.asyncio
async def test_state_store_restores_only_internal_causal_context(
    monkeypatch, tmp_path
) -> None:
    """A state mutation handler observes the same invocation carried outbound."""
    monkeypatch.setattr("state_store.main.TRACE_DB_PATH", tmp_path / "trace.db")
    app: FastAPI = create_app(initialize_immediately=True)

    @app.post("/_test_context")
    async def context(request: Request) -> dict[str, str | None]:
        from providers.tracing import current_trace_context

        value = current_trace_context()
        return {
            "ticket_id": value.ticket_id if value else None,
            "action_id": value.action_id if value else None,
        }

    trace_context = new_trace_context(ticket_id="PERF-join", agent_id="agent")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    accepted = await client.post(
        "/_test_context",
        headers={
            **trace_headers(trace_context),
            "Authorization": f"Bearer {app.state.api_token}",
        },
    )
    rejected = await client.post(
        "/_test_context",
        headers={
            "traceparent": f"00-{trace_context.trace_id}-{trace_context.action_id}-01",
            "X-Agentic-Perf-Ticket-Id": "forged",
            "X-Agentic-Perf-Causal-Context": "v1",
        },
    )
    await client.aclose()
    assert accepted.json()["ticket_id"] == "PERF-join"
    assert accepted.json()["action_id"] == trace_context.action_id
    assert rejected.json()["ticket_id"] is None
