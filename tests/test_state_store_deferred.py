"""Regression tests for on-demand loading of terminal tickets."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    CreateTicketRequest,
    TicketStatus,
)
from state_store.store import TicketNotFound, TicketStore


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


def test_claim_reconciliation_pages_bounded_deferred_scan(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    claimed: set[str] = set()
    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test-orchestrator",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )

    for index in range(8):
        ticket = store.create_ticket(
            CreateTicketRequest(summary=f"closed {index}", description="d")
        )
        if index % 2:
            store.claim_ticket(
                ticket.id,
                f"owner-{index}",
                session_id=session_id,
                epoch=lease.epoch,
                claim_id=f"claim-{index}",
            )
            with store._lock:
                stored = store._tickets[ticket.id]
                stored.status = TicketStatus.CLOSED
                store._global_seq += 1
                stored.transition_seq = store._global_seq
                store._persist_ticket(stored)
            claimed.add(ticket.id)
        else:
            store.force_close(ticket.id)

    restarted = TicketStore(persist_dir=persist_dir)
    assert not restarted._tickets
    seen: list[str] = []
    after: str | None = None
    for _ in range(20):
        page, after, read_errors = restarted.list_ticket_claim_reconciliation_page(
            after_id=after,
            limit=1,
            scan_limit=2,
        )
        assert read_errors is False
        assert len(page) <= 1
        assert all(set(ticket["custom_fields"]) == {"claim"} for ticket in page)
        seen.extend(ticket["id"] for ticket in page)
        if after is None:
            break
    else:
        raise AssertionError("claim reconciliation pagination did not finish")

    assert seen == sorted(claimed)
    assert not restarted._tickets
    assert set(restarted._deferred_paths) == {
        path.stem for path in persist_dir.glob("PERF-*.json")
    }


def test_claim_reconciliation_surfaces_deferred_read_errors(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    restarted = TicketStore(persist_dir=persist_dir)
    (persist_dir / f"{ticket.id}.json").write_text("{", encoding="utf-8")

    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is True


def test_claim_reconciliation_surfaces_startup_read_errors(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    (persist_dir / f"{ticket.id}.json").write_text("{", encoding="utf-8")

    restarted = TicketStore(persist_dir=persist_dir)
    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is True
    assert restarted._unreadable_ticket_paths[ticket.id] == (
        persist_dir / f"{ticket.id}.json"
    )


def test_claim_reconciliation_rejects_filename_id_mismatch(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    ticket_path = persist_dir / f"{ticket.id}.json"
    document = json.loads(ticket_path.read_text(encoding="utf-8"))
    document["id"] = "PERF-MISMATCH"
    ticket_path.write_text(json.dumps(document), encoding="utf-8")
    restarted = TicketStore(persist_dir=persist_dir)

    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is True


def test_deferred_loader_rejects_filename_id_mismatch(tmp_path: Path) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    closed = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(closed.id)
    target = store.create_ticket(
        CreateTicketRequest(summary="must survive", description="d")
    )
    closed_path = persist_dir / f"{closed.id}.json"
    document = json.loads(closed_path.read_text(encoding="utf-8"))
    document["id"] = target.id
    closed_path.write_text(json.dumps(document), encoding="utf-8")
    restarted = TicketStore(persist_dir=persist_dir)

    with pytest.raises(TicketNotFound):
        restarted.get_ticket(closed.id)

    assert restarted.get_ticket(target.id).summary == "must survive"
    assert restarted._unreadable_ticket_paths[closed.id] == closed_path


def test_claim_reconciliation_rejects_non_object_custom_fields(
    tmp_path: Path,
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    ticket_path = persist_dir / f"{ticket.id}.json"
    document = json.loads(ticket_path.read_text(encoding="utf-8"))
    document["custom_fields"] = []
    ticket_path.write_text(json.dumps(document), encoding="utf-8")

    restarted = TicketStore(persist_dir=persist_dir)
    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is True


def test_claim_reconciliation_projects_unneeded_claim_fields(
    tmp_path: Path,
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test-orchestrator",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.claim_ticket(
        ticket.id,
        "owner",
        session_id=session_id,
        epoch=lease.epoch,
        claim_id="claim-1",
    )
    with store._lock:
        stored = store._tickets[ticket.id]
        stored.status = TicketStatus.CLOSED
        store._global_seq += 1
        stored.transition_seq = store._global_seq
        store._persist_ticket(stored)
    ticket_path = persist_dir / f"{ticket.id}.json"
    document = json.loads(ticket_path.read_text(encoding="utf-8"))
    document["custom_fields"]["claim"]["padding"] = "x" * 1_000_000
    ticket_path.write_text(json.dumps(document), encoding="utf-8")
    restarted = TicketStore(persist_dir=persist_dir)

    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert read_errors is False
    assert next_cursor is None
    assert len(page) == 1
    assert "padding" not in page[0]["custom_fields"]["claim"]
    assert len(json.dumps(page)) < 2_000


def test_claim_reconciliation_bounds_aggregate_page_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test-orchestrator",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    for index in range(5):
        ticket = store.create_ticket(
            CreateTicketRequest(summary=f"closed {index}", description="d")
        )
        store.claim_ticket(
            ticket.id,
            f"owner-{index}",
            session_id=session_id,
            epoch=lease.epoch,
            claim_id=f"claim-{index}",
        )
        with store._lock:
            stored = store._tickets[ticket.id]
            stored.status = TicketStatus.CLOSED
            store._global_seq += 1
            stored.transition_seq = store._global_seq
            store._persist_ticket(stored)
        ticket_path = persist_dir / f"{ticket.id}.json"
        document = json.loads(ticket_path.read_text(encoding="utf-8"))
        document["custom_fields"]["claim"]["claim_id"] = "c" * 500
        ticket_path.write_text(json.dumps(document), encoding="utf-8")

    monkeypatch.setattr(TicketStore, "MAX_CLAIM_RECONCILIATION_FIELD_CHARS", 512)
    monkeypatch.setattr(TicketStore, "MAX_CLAIM_RECONCILIATION_PAGE_BYTES", 1_000)
    restarted = TicketStore(persist_dir=persist_dir)
    seen: list[str] = []
    after: str | None = None
    pages = 0
    for _ in range(20):
        page, after, read_errors = restarted.list_ticket_claim_reconciliation_page(
            after_id=after,
            limit=10,
            scan_limit=100,
        )
        assert read_errors is False
        assert page
        pages += 1
        seen.extend(ticket["id"] for ticket in page)
        if after is None:
            break
    else:
        raise AssertionError("claim reconciliation pagination did not finish")

    assert pages > 1
    assert seen == sorted(seen)
    assert len(seen) == 5


def test_claim_reconciliation_bounds_aggregate_scan_bytes_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test-orchestrator",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    ticket_ids: list[str] = []
    for index in range(3):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="closed", description="d")
        )
        ticket_ids.append(ticket.id)
        store.claim_ticket(
            ticket.id,
            "owner",
            session_id=session_id,
            epoch=lease.epoch,
            claim_id=f"claim-{index}",
        )
        with store._lock:
            stored = store._tickets[ticket.id]
            stored.status = TicketStatus.CLOSED
            store._global_seq += 1
            stored.transition_seq = store._global_seq
            store._persist_ticket(stored)
        ticket_path = persist_dir / f"{ticket.id}.json"
        document = json.loads(ticket_path.read_text(encoding="utf-8"))
        document["custom_fields"]["claim"]["padding"] = "x" * 1_000
        ticket_path.write_text(json.dumps(document), encoding="utf-8")

    sizes = [
        (persist_dir / f"{ticket_id}.json").stat().st_size for ticket_id in ticket_ids
    ]
    assert sizes[0] == sizes[1] == sizes[2]
    monkeypatch.setattr(
        TicketStore,
        "MAX_CLAIM_RECONCILIATION_SCAN_BYTES",
        sizes[0] * 2,
    )
    monkeypatch.setattr(
        TicketStore,
        "MAX_CLAIM_RECONCILIATION_RECORD_BYTES",
        sizes[0],
    )
    restarted = TicketStore(persist_dir=persist_dir)
    expected_ids = sorted(ticket_ids)

    first_page, cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=10, scan_limit=100
    )

    assert [ticket["id"] for ticket in first_page] == expected_ids[:2]
    assert cursor == expected_ids[1]
    assert read_errors is False

    second_page, cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        after_id=cursor, limit=10, scan_limit=100
    )

    assert [ticket["id"] for ticket in second_page] == expected_ids[2:]
    assert cursor is None
    assert read_errors is False


def test_claim_reconciliation_bounds_deferred_record_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    restarted = TicketStore(persist_dir=persist_dir)
    ticket_path = persist_dir / f"{ticket.id}.json"
    monkeypatch.setattr(
        TicketStore,
        "MAX_CLAIM_RECONCILIATION_RECORD_BYTES",
        ticket_path.stat().st_size - 1,
    )

    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is True


def test_claim_reconciliation_reads_deferred_records_outside_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_dir = tmp_path / "tickets"
    store = TicketStore(persist_dir=persist_dir)
    ticket = store.create_ticket(CreateTicketRequest(summary="closed", description="d"))
    store.force_close(ticket.id)
    restarted = TicketStore(persist_dir=persist_dir)
    ticket_path = persist_dir / f"{ticket.id}.json"
    observed_lock_states: list[bool] = []
    original_open = Path.open

    def tracked_open(path: Path, *args, **kwargs):
        if path == ticket_path:
            observed_lock_states.append(restarted._lock.locked())
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    page, next_cursor, read_errors = restarted.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=10
    )

    assert page == []
    assert next_cursor is None
    assert read_errors is False
    assert observed_lock_states == [False]


def test_claim_reconciliation_does_not_iterate_all_tickets(tmp_path: Path) -> None:
    class NoIterationDict(dict):
        def __iter__(self):
            raise AssertionError("claim reconciliation scanned the full ticket map")

    store = TicketStore(persist_dir=tmp_path / "tickets")
    for index in range(3):
        store.create_ticket(
            CreateTicketRequest(summary=f"ticket {index}", description="d")
        )
    store._tickets = NoIterationDict(store._tickets)

    page, next_cursor, read_errors = store.list_ticket_claim_reconciliation_page(
        limit=1, scan_limit=1
    )

    assert page == []
    assert next_cursor == store._ticket_ids[0]
    assert read_errors is False
