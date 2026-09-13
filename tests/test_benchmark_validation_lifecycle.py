from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.benchmark.server import (
    _execution_intent_digest,
    _execution_plan_fingerprint,
    _get_validated_runfile,
    _runfile_fingerprint,
)
from state_store.api.validations import create_validation
from state_store.auth import Principal
from state_store.models import CreateTicketRequest, CreateValidationRequest
from state_store.store import TicketStore


def _record(validation_id: str, *, value: int = 1) -> dict:
    run_file = {"benchmarks": [{"value": value}]}
    plan_digest = _execution_plan_fingerprint({})
    runfile_digest = _runfile_fingerprint(run_file)
    return {
        "validation_id": validation_id,
        "run_file": run_file,
        "runfile_fingerprint": runfile_digest,
        "params_fingerprint": "no-plan",
        "harness": "crucible",
        "controller": "controller.example",
        "creator": {"invocation_id": "invocation-1", "request_id": "request-1"},
        "server_pid": 123,
        "execution_plan_fingerprint": plan_digest,
        "execution_intent_digest": _execution_intent_digest(
            runfile_digest,
            plan_digest,
            "crucible",
            "controller.example",
            "crucible run",
        ),
        "run_command": "crucible run",
        "validator_command": "crucible validate",
        "validator_version": "test",
        "validation_output_digest": "a" * 64,
        "validation_output_summary": "ok",
    }


def _ticket(store: TicketStore) -> str:
    return store.create_ticket(
        CreateTicketRequest(summary="validate", description="validate")
    ).id


def test_sequential_validations_are_immutable_and_exact_id_addressable(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    _, conflict = store.create_validation(ticket_id, _record("val-one"), 0)
    assert conflict is None
    _, conflict = store.create_validation(ticket_id, _record("val-two", value=2), 1)
    assert conflict is None

    ticket = store.get_ticket(ticket_id).model_dump(mode="json")
    manifest = ticket["custom_fields"]["benchmark_validations"]
    assert manifest["active_validation_id"] == "val-two"
    assert set(manifest["records"]) == {"val-one", "val-two"}
    # The first token remains executable; latest is only a convenience pointer.
    runfile, error = _get_validated_runfile(
        "val-one", "controller.example", "crucible", ticket
    )
    assert error is None
    assert runfile == _record("val-one")["run_file"]


def test_concurrent_validation_cas_has_one_winner_and_no_lost_record(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    results: list[tuple[str, dict | None]] = []
    gate = threading.Barrier(2)

    def create(validation_id: str) -> None:
        gate.wait()
        _, conflict = store.create_validation(ticket_id, _record(validation_id), 0)
        results.append((validation_id, conflict))

    threads = [threading.Thread(target=create, args=(f"val-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [value for value, conflict in results if conflict is None]
    conflicts = [conflict for _, conflict in results if conflict is not None]
    assert len(winners) == 1
    assert conflicts == [{"current_version": 1, "active_validation_id": winners[0]}]
    records = store.get_ticket(ticket_id).custom_fields["benchmark_validations"][
        "records"
    ]
    assert set(records) == set(winners)


def test_explicit_supersession_preserves_record_and_rejects_token(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    store.create_validation(ticket_id, _record("val-one"), 0)
    store.create_validation(ticket_id, _record("val-two", value=2), 1)
    _, conflict = store.supersede_validation(
        ticket_id, "val-one", "val-two", "relevant parameters changed", 2
    )
    assert conflict is None
    ticket = store.get_ticket(ticket_id).model_dump(mode="json")
    _, error = _get_validated_runfile(
        "val-one", "controller.example", "crucible", ticket
    )
    assert "superseded" in error
    assert "replacement_validation_id=val-two" in error
    records = ticket["custom_fields"]["benchmark_validations"]["records"]
    assert records["val-one"]["run_file"] == _record("val-one")["run_file"]
    assert any(
        item.get("reason") == "relevant parameters changed" for item in records.values()
    )


def test_unrelated_field_update_and_restart_do_not_revoke_validation(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    store.create_validation(ticket_id, _record("val-one"), 0)
    store.update_fields(ticket_id, {"unrelated": "value"})
    restarted = TicketStore(persist_dir=tmp_path)
    ticket = restarted.get_ticket(ticket_id).model_dump(mode="json")
    runfile, error = _get_validated_runfile(
        "val-one", "controller.example", "crucible", ticket
    )
    assert error is None
    assert runfile == _record("val-one")["run_file"]


def test_legacy_validation_migrates_deterministically(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    ticket = store._tickets[ticket_id]
    ticket.custom_fields["benchmark_validation"] = _record("val-legacy")
    store._persist_ticket(ticket)
    migrated = TicketStore(persist_dir=tmp_path)
    manifest = migrated.get_ticket(ticket_id).custom_fields["benchmark_validations"]
    assert manifest["version"] == 0
    assert manifest["active_validation_id"] == "val-legacy"
    assert (
        manifest["records"]["val-legacy"]["creator"] == _record("val-legacy")["creator"]
    )
    _, error = _get_validated_runfile(
        "val-legacy",
        "controller.example",
        "crucible",
        migrated.get_ticket(ticket_id).model_dump(mode="json"),
    )
    assert "legacy/unapproved" in error


def test_execution_rejects_runfile_or_plan_tampering(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    store.create_validation(ticket_id, _record("val-one"), 0)
    ticket = store.get_ticket(ticket_id).model_dump(mode="json")
    ticket["custom_fields"]["benchmark_validations"]["records"]["val-one"][
        "run_file"
    ] = {"tampered": True}
    _, error = _get_validated_runfile(
        "val-one", "controller.example", "crucible", ticket
    )
    assert "invalid runfile fingerprint" in error


def test_generic_field_updates_cannot_replace_validation_manifest(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    store.create_validation(ticket_id, _record("val-one"), 0)
    with pytest.raises(ValueError, match="immutable"):
        store.update_fields(ticket_id, {"benchmark_validations": {}})


def test_user_cannot_forge_controller_validation_post(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    record = _record("val-" + "a" * 32)
    body = CreateValidationRequest(record=record, expected_version=0)
    request = SimpleNamespace(
        state=SimpleNamespace(principal=Principal("user", "writer", False)),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(store=store, multi_user=False)),
    )
    with pytest.raises(HTTPException) as error:
        create_validation(ticket_id, body, request)
    assert error.value.status_code == 403
