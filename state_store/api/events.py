from __future__ import annotations

from fastapi import APIRouter, Query, Request

from providers.cost import estimate_cumulative_cost

router = APIRouter(prefix="/tickets", tags=["events"])
usage_router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/{ticket_id}/events")
def get_events(
    ticket_id: str,
    request: Request,
    since: int = Query(0, description="Return events with seq > this value"),
    limit: int = Query(200, description="Max events to return"),
):
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        return {"events": [], "latest_seq": 0}
    events = event_bus.get_events(ticket_id, since=since, limit=limit)
    latest_seq = events[-1]["seq"] if events else since
    return {"events": events, "latest_seq": latest_seq}


@router.get("/{ticket_id}/transcript")
def get_transcript(
    ticket_id: str,
    request: Request,
    agent: str = Query(
        None,
        description="Filter to a single agent name",
    ),
):
    """Return all events for a ticket as a full transcript."""
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        return {"ticket_id": ticket_id, "events": []}
    events = event_bus.get_events(ticket_id, since=0, limit=10000)
    if agent:
        events = [e for e in events if e.get("agent") == agent]

    store = request.app.state.store
    ticket = store.get(ticket_id)
    ticket_data = {}
    if ticket:
        ticket_data = {
            "summary": ticket.summary,
            "description": ticket.description,
            "status": ticket.status.value,
        }

    return {
        "ticket_id": ticket_id,
        "ticket": ticket_data,
        "events": events,
    }


@router.get("/{ticket_id}/usage")
def get_usage(ticket_id: str, request: Request):
    """Get cumulative LLM token usage and estimated cost.

    Computes usage from stored llm_usage events emitted by
    the OTLP span processor. This works across process
    boundaries since events are persisted to JSONL files.
    """
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        return {
            "ticket_id": ticket_id,
            "usage": {},
            "estimated_cost_usd": 0.0,
            "by_agent": {},
        }

    # Compute usage from stored events rather than
    # in-memory accumulators, since the state store
    # and orchestrator are separate processes.
    events = event_bus.get_events(ticket_id, since=0, limit=10000)

    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_create = 0
    llm_calls = 0
    total_duration = 0
    by_agent: dict[str, dict] = {}

    models_seen: set[str] = set()
    for evt in events:
        if evt.get("event_type") != "llm_usage":
            continue
        data = evt.get("data", {})
        in_tok = data.get("input_tokens", 0) or 0
        out_tok = data.get("output_tokens", 0) or 0
        dur = data.get("duration_ms", 0) or 0
        model = data.get("model", "")
        cr = data.get("cache_read_input_tokens", 0) or 0
        cc = data.get("cache_creation_input_tokens", 0) or 0

        if not in_tok and not out_tok:
            continue

        total_in += in_tok
        total_out += out_tok
        total_cache_read += cr
        total_cache_create += cc
        total_duration += dur
        llm_calls += 1
        if model:
            models_seen.add(model)

        agent = evt.get("agent", "")
        if agent and agent != "system":
            if agent not in by_agent:
                by_agent[agent] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                    "llm_calls": 0,
                    "total_duration_ms": 0,
                    "models_used": set(),
                }
            ba = by_agent[agent]
            ba["input_tokens"] += in_tok
            ba["output_tokens"] += out_tok
            ba["cache_read_input_tokens"] += cr
            ba["cache_creation_input_tokens"] += cc
            ba["total_tokens"] += in_tok + out_tok
            ba["llm_calls"] += 1
            ba["total_duration_ms"] += dur
            if model:
                ba["models_used"].add(model)

    usage = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "total_tokens": total_in + total_out,
        "llm_calls": llm_calls,
        "total_duration_ms": total_duration,
        "models_used": sorted(models_seen),
    }

    # Per-agent cost estimates
    agent_costs = {}
    for agent, au in by_agent.items():
        au["models_used"] = sorted(au.get("models_used", set()))
        agent_costs[agent] = {
            **au,
            "estimated_cost_usd": round(estimate_cumulative_cost(au), 6),
        }

    return {
        "ticket_id": ticket_id,
        "usage": usage,
        "estimated_cost_usd": round(estimate_cumulative_cost(usage), 6),
        "by_agent": agent_costs,
    }


def _compute_ticket_usage(
    event_bus: object,
    ticket_id: str,
) -> dict:
    """Compute lightweight usage summary for a single ticket."""
    events = event_bus.get_events(ticket_id, since=0, limit=10000)

    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_create = 0
    llm_calls = 0
    models_seen: set[str] = set()

    for evt in events:
        if evt.get("event_type") != "llm_usage":
            continue
        data = evt.get("data", {})
        in_tok = data.get("input_tokens", 0) or 0
        out_tok = data.get("output_tokens", 0) or 0
        if not in_tok and not out_tok:
            continue
        total_in += in_tok
        total_out += out_tok
        total_cache_read += data.get("cache_read_input_tokens", 0) or 0
        total_cache_create += data.get("cache_creation_input_tokens", 0) or 0
        llm_calls += 1
        model = data.get("model", "")
        if model:
            models_seen.add(model)

    usage = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "total_tokens": total_in + total_out,
        "llm_calls": llm_calls,
        "models_used": sorted(models_seen),
    }
    return {
        **usage,
        "estimated_cost_usd": round(estimate_cumulative_cost(usage), 6),
    }


@usage_router.get("/summary")
def get_usage_summary(request: Request):
    """Get usage summary across all tickets."""
    event_bus = getattr(request.app.state, "event_bus", None)
    store = request.app.state.store
    tickets = store.list_tickets()

    empty_global = {
        "total_tokens": 0,
        "llm_calls": 0,
        "estimated_cost_usd": 0.0,
    }

    if event_bus is None:
        return {"global": empty_global, "by_ticket": {}}

    by_ticket = {}
    g_tokens = 0
    g_calls = 0
    g_cost = 0.0

    for ticket in tickets:
        tu = _compute_ticket_usage(event_bus, ticket.id)
        if tu["llm_calls"] > 0:
            by_ticket[ticket.id] = tu
            g_tokens += tu["total_tokens"]
            g_calls += tu["llm_calls"]
            g_cost += tu["estimated_cost_usd"]

    return {
        "global": {
            "total_tokens": g_tokens,
            "llm_calls": g_calls,
            "estimated_cost_usd": round(g_cost, 6),
        },
        "by_ticket": by_ticket,
    }
