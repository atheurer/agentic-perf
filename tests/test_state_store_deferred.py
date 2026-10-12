"""Regression tests for on-demand loading of terminal tickets."""

from __future__ import annotations

import json
from pathlib import Path

from state_store.models import CreateTicketRequest
from state_store.store import TicketStore


def test_get_tickets_since_includes_deferred_terminal_tickets(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    closed = store.force_close(ticket.id)
    newer_ticket = store.create_ticket(
        CreateTicketRequest(summary="newer closed", description="d")
    )
    newer_closed = store.force_close(newer_ticket.id)

    restarted = TicketStore(persist_dir=persist_dir)
    assert closed.id in restarted._deferred_paths
    assert closed.id not in restarted._tickets

    changed = restarted.get_tickets_since(closed.transition_seq)

    assert [item.id for item in changed] == [newer_closed.id]
    assert closed.id not in restarted._tickets
    assert closed.id in restarted._deferred_paths
    assert newer_closed.id in restarted._tickets
    assert newer_closed.id not in restarted._deferred_paths


def test_invalidate_usage_summary_loads_and_persists_deferred_ticket(
    tmp_path: Path,
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="cached usage",
            description="d",
            custom_fields={"_usage_summary": {"total_tokens": 123}},
        )
    )
    store.force_close(ticket.id)
    ticket_path = persist_dir / f"{ticket.id}.json"

    restarted = TicketStore(persist_dir=persist_dir)
    assert ticket.id in restarted._deferred_paths

    restarted.invalidate_cached_usage_summary(ticket.id)

    assert ticket.id in restarted._tickets
    assert restarted.get_cached_usage_summary(ticket.id) is None
    persisted = json.loads(ticket_path.read_text(encoding="utf-8"))
    assert "_usage_summary" not in persisted["custom_fields"]


def test_startup_finds_root_metadata_after_large_nested_fields(
    tmp_path: Path,
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="large closed", description="d")
    )
    closed = store.force_close(ticket.id)
    ticket_path = persist_dir / f"{ticket.id}.json"
    document = json.loads(ticket_path.read_text(encoding="utf-8"))

    # Place large nested data and misleading nested metadata before the real
    # root fields. The startup scan must stay lazy and read the root values.
    document["custom_fields"]["large_payload"] = {
        "status": "new",
        "transition_seq": closed.transition_seq + 1000,
        "payload": "x" * 10_000,
    }
    ordered = {
        "id": document["id"],
        "summary": document["summary"],
        "description": document["description"],
        "custom_fields": document["custom_fields"],
    }
    ordered.update(
        (key, value) for key, value in document.items() if key not in ordered
    )
    ticket_path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")

    restarted = TicketStore(persist_dir=persist_dir)

    assert closed.id in restarted._deferred_paths
    assert closed.id not in restarted._tickets
    assert restarted._deferred_transition_seqs[closed.id] == closed.transition_seq
    assert restarted._global_seq == closed.transition_seq
