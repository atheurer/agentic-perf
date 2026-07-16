from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

_HEARTBEAT_INTERVAL = 15.0
_POLL_INTERVAL = 1.0


@router.get("/events/stream")
async def event_stream(
    request: Request,
    ticket_id: str | None = Query(
        None,
        description=(
            "Comma-separated ticket IDs to follow. Omit to follow all active tickets."
        ),
    ),
    event_type: str | None = Query(
        None,
        description="Comma-separated event types to include",
    ),
    since: int | None = Query(
        None,
        description=(
            "Seq cursor; only honored for a single ticket_id. "
            "Returns events with seq > this value."
        ),
    ),
) -> StreamingResponse:
    event_bus = getattr(request.app.state, "event_bus", None)
    store = request.app.state.store

    ticket_ids: list[str] | None = None
    if ticket_id:
        ticket_ids = [t.strip() for t in ticket_id.split(",") if t.strip()]

    type_filter: set[str] | None = None
    if event_type:
        type_filter = {t.strip() for t in event_type.split(",") if t.strip()}

    initial_since = (
        since if since is not None and ticket_ids and len(ticket_ids) == 1 else None
    )

    async def generate() -> Any:
        cursors: dict[str, int] = {}
        if initial_since is not None and ticket_ids:
            cursors[ticket_ids[0]] = initial_since

        seconds_since_heartbeat = 0.0

        while True:
            if await request.is_disconnected():
                return

            ids_to_poll = ticket_ids
            if ids_to_poll is None:
                ids_to_poll = _active_ticket_ids(store)

            new_events = await asyncio.to_thread(
                _poll_events,
                event_bus,
                ids_to_poll,
                cursors,
                type_filter,
            )

            for evt in new_events:
                tid = evt.get("ticket_id", "")
                seq = evt.get("seq", 0)
                etype = evt.get("event_type", "message")
                data_line = json.dumps(evt, default=str)
                yield f"id: {tid}:{seq}\nevent: {etype}\ndata: {data_line}\n\n"
                seconds_since_heartbeat = 0.0

            if not new_events:
                seconds_since_heartbeat += _POLL_INTERVAL
                if seconds_since_heartbeat >= _HEARTBEAT_INTERVAL:
                    yield ": keepalive\n\n"
                    seconds_since_heartbeat = 0.0

            await asyncio.sleep(_POLL_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _active_ticket_ids(store: Any) -> list[str]:
    """Return IDs of non-closed tickets."""
    try:
        tickets = store.list_tickets()
        return [t.id for t in tickets if t.status.value != "closed"]
    except Exception:
        return []


def _poll_events(
    event_bus: Any,
    ticket_ids: list[str],
    cursors: dict[str, int],
    type_filter: set[str] | None,
) -> list[dict[str, Any]]:
    """Read new events from the bus for the given tickets.

    Updates cursors in place. Called via asyncio.to_thread
    since EventBus file reads are blocking I/O.
    """
    if event_bus is None:
        return []

    results: list[dict[str, Any]] = []
    for tid in ticket_ids:
        since = cursors.get(tid, 0)
        events = event_bus.get_events(tid, since=since, limit=200)
        for evt in events:
            evt["ticket_id"] = tid
            seq = evt.get("seq", 0)
            if type_filter and evt.get("event_type") not in type_filter:
                if seq > cursors.get(tid, 0):
                    cursors[tid] = seq
                continue
            results.append(evt)
            if seq > cursors.get(tid, 0):
                cursors[tid] = seq

    results.sort(key=lambda e: (e.get("timestamp", ""), e.get("seq", 0)))
    return results
