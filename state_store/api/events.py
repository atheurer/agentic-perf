from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from providers.cost import estimate_cost
from providers.usage import summarize_usage_events

from ..models import TicketStatus
from ..store import TicketNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["events"])
usage_router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/{ticket_id}/events")
def get_events(
    ticket_id: str,
    request: Request,
    since: int = Query(0, description="Return events with seq > this value"),
    limit: int = Query(200, description="Max events to return", ge=1, le=1000),
):
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        return {"events": [], "latest_seq": 0, "terminal_events": []}
    events = event_bus.get_events(ticket_id, since=since, limit=limit)
    latest_seq = events[-1]["seq"] if events else since
    terminal_events = event_bus.get_terminal_events(ticket_id)
    return {
        "events": events,
        "latest_seq": latest_seq,
        "terminal_events": terminal_events,
    }


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
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    ticket_data = {
        "summary": ticket.summary,
        "description": ticket.description,
        "status": ticket.status.value,
    }

    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        events = []
    else:
        events = event_bus.get_events(ticket_id, since=0, limit=10000)
        if agent:
            events = [e for e in events if e.get("agent") == agent]

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
    total_cost = 0.0
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

        # Cost per event using its actual model, so mixed-model
        # tickets get correct totals instead of pricing all
        # tokens at whichever model sorts first alphabetically.
        evt_cost = estimate_cost(
            model,
            in_tok,
            out_tok,
            cache_read_input_tokens=cr,
            cache_creation_input_tokens=cc,
        )
        total_cost += evt_cost

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
                    "estimated_cost_usd": 0.0,
                    "models_used": set(),
                }
            ba = by_agent[agent]
            ba["input_tokens"] += in_tok
            ba["output_tokens"] += out_tok
            ba["cache_read_input_tokens"] += cr
            ba["cache_creation_input_tokens"] += cc
            ba["total_tokens"] += in_tok + out_tok + cr + cc
            ba["llm_calls"] += 1
            ba["total_duration_ms"] += dur
            ba["estimated_cost_usd"] += evt_cost
            if model:
                ba["models_used"].add(model)

    usage = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "total_tokens": total_in + total_out + total_cache_read + total_cache_create,
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
            "estimated_cost_usd": round(au["estimated_cost_usd"], 6),
        }

    return {
        "ticket_id": ticket_id,
        "usage": usage,
        "estimated_cost_usd": round(total_cost, 6),
        "by_agent": agent_costs,
    }


def _compute_ticket_usage(
    event_bus: object,
    ticket_id: str,
) -> dict:
    """Compute lightweight usage summary for a single ticket."""
    events = event_bus.get_usage_events(ticket_id)

    return _summarize_usage_events(events)


def _summarize_usage_events(events: list[dict]) -> dict:
    """Aggregate already-filtered usage events for one ticket."""
    return summarize_usage_events(events)


_summary_cache: dict = {}
_summary_cache_ts: float = 0.0
_closed_ticket_usage_cache: dict[str, dict] = {}
_SUMMARY_TTL: float = 5.0
_summary_compute_lock = threading.Lock()


def invalidate_summary_cache() -> None:
    """Reset the usage-summary cache (called by tests)."""
    global _closed_ticket_usage_cache, _summary_cache, _summary_cache_ts
    with _summary_compute_lock:
        _summary_cache = {}
        _summary_cache_ts = 0.0
        _closed_ticket_usage_cache = {}


def invalidate_closed_ticket_usage_cache(ticket_id: str) -> None:
    """Forget a derived fallback after late usage is persisted."""
    global _closed_ticket_usage_cache, _summary_cache, _summary_cache_ts
    with _summary_compute_lock:
        _closed_ticket_usage_cache.pop(ticket_id, None)
        _summary_cache = {}
        _summary_cache_ts = 0.0


@usage_router.get("/summary")
def get_usage_summary(request: Request):
    """Get usage summary across all tickets.

    Active tickets are read from the filtered usage-event stream. Closed
    tickets use a snapshot persisted when they close. Historical tickets
    without one are cached in memory, so this GET endpoint stays read-only.
    """
    global _closed_ticket_usage_cache, _summary_cache, _summary_cache_ts

    with _summary_compute_lock:
        now = time.monotonic()
        if _summary_cache and (now - _summary_cache_ts) < _SUMMARY_TTL:
            return _summary_cache

        event_bus = getattr(request.app.state, "event_bus", None)
        store = request.app.state.store
        tickets = store.list_tickets()

        empty_global = {
            "total_tokens": 0,
            "llm_calls": 0,
            "estimated_cost_usd": 0.0,
        }

        if event_bus is None:
            result = {"global": empty_global, "by_ticket": {}}
            _summary_cache = result
            _summary_cache_ts = now
            return result

        usage_by_ticket: dict[str, list[dict]] = defaultdict(list)
        uncached_closed: set[str] = set()
        active_ticket_ids: set[str] = set()
        cached_by_ticket: dict[str, dict] = {}
        for ticket in tickets:
            if ticket.status == TicketStatus.CLOSED:
                cached = store.get_cached_usage_summary(ticket.id)
                if cached is None:
                    cached = _closed_ticket_usage_cache.get(ticket.id)
                if cached is not None:
                    cached_by_ticket[ticket.id] = cached
                else:
                    uncached_closed.add(ticket.id)
            else:
                active_ticket_ids.add(ticket.id)

        live_ticket_ids = active_ticket_ids | uncached_closed
        for event in (
            event_bus.get_usage_events(ticket_ids=live_ticket_ids)
            if live_ticket_ids
            else []
        ):
            usage_by_ticket[event.get("ticket_id", "")].append(event)

        by_ticket = {}
        g_tokens = 0
        g_calls = 0
        g_cost = 0.0

        for ticket in tickets:
            tu = cached_by_ticket.get(ticket.id)
            if tu is None:
                tu = _summarize_usage_events(usage_by_ticket.get(ticket.id, []))
                if ticket.id in uncached_closed:
                    _closed_ticket_usage_cache[ticket.id] = tu
            if tu["llm_calls"] > 0:
                by_ticket[ticket.id] = tu
                g_tokens += tu["total_tokens"]
                g_calls += tu["llm_calls"]
                g_cost += tu["estimated_cost_usd"]

        result = {
            "global": {
                "total_tokens": g_tokens,
                "llm_calls": g_calls,
                "estimated_cost_usd": round(g_cost, 6),
            },
            "by_ticket": by_ticket,
        }
        _summary_cache = result
        _summary_cache_ts = now
        return result


@usage_router.get("/by-user")
def get_usage_by_user(
    request: Request,
    window_hours: int = Query(
        24,
        description="Rolling window in hours (default 24)",
        ge=1,
        le=168,
    ),
):
    """Get LLM usage aggregated by user from the quota ledger.

    Admin-only.  Uses the quota ledger (not per-ticket JSONL)
    for accurate cross-ticket, cross-archive aggregation.
    """
    from state_store.auth import require_admin

    principal = request.state.principal
    require_admin(principal)

    try:
        from providers.quota import UsageLedger
    except ImportError:
        return {"by_user": {}, "window_hours": window_hours}

    ledger = UsageLedger()
    window = timedelta(hours=window_hours)

    entries = ledger.read_window(window)
    ledger.close()

    by_user: dict[str, dict] = {}
    for e in entries:
        if not e.charged_to:
            continue
        if e.charged_to not in by_user:
            by_user[e.charged_to] = {
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "llm_calls": 0,
                "groups": set(),
            }
        u = by_user[e.charged_to]
        u["total_tokens"] += e.input_tokens + e.output_tokens
        u["total_cost_usd"] += e.cost_usd
        u["llm_calls"] += 1
        u["groups"].update(e.groups)

    result = {}
    for username, data in by_user.items():
        result[username] = {
            "total_tokens": data["total_tokens"],
            "total_cost_usd": round(data["total_cost_usd"], 6),
            "llm_calls": data["llm_calls"],
            "groups": sorted(data["groups"]),
        }

    return {"by_user": result, "window_hours": window_hours}
