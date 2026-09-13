from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.store import InvalidTransition, TicketDispatchBlocked, TicketStore

_SPEC = importlib.util.spec_from_file_location(
    "dev_instance_identity",
    Path(__file__).parents[1] / "scripts/dev_instance_identity.py",
)
assert _SPEC and _SPEC.loader
identity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(identity)


def _instance(root: Path, name: str, port: int) -> tuple[Path, Path]:
    home = root / name
    worktree = root / f"{name}-repo"
    worktree.mkdir()
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "instance_name": name,
                "state_store": {
                    "url": f"http://localhost:{port}",
                    "port": port,
                },
            }
        )
    )
    identity.create_manifest(str(home), str(worktree), name, port)
    return home, worktree


def test_manifest_and_config_identity_are_checked(tmp_path: Path) -> None:
    home, worktree = _instance(tmp_path, "one", 18101)

    manifest = identity.validate(str(home), str(worktree), "one")

    assert manifest["schema_version"] == 1
    assert manifest["runtime_home"] == str(home.resolve())
    assert manifest["worktree"] == str(worktree.resolve())

    config = json.loads((home / "config.json").read_text())
    config["instance_name"] = "copied-default"
    (home / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="config instance_name"):
        identity.validate(str(home), str(worktree), "one")


def test_manifest_digest_rejects_identity_edits(tmp_path: Path) -> None:
    home, worktree = _instance(tmp_path, "one", 18102)
    manifest_path = home / identity.MANIFEST
    manifest_path.chmod(0o644)
    manifest_path.write_text(manifest_path.read_text().replace('"one"', '"other"'))
    with pytest.raises(ValueError, match="modified"):
        identity.validate(str(home), str(worktree), "one")


def test_import_is_dry_run_by_default_and_sanitizes_fixture(tmp_path: Path) -> None:
    source, _ = _instance(tmp_path, "source", 18103)
    destination, _ = _instance(tmp_path, "destination", 18104)
    tickets = source / "tickets"
    tickets.mkdir()
    (tickets / "PERF-1.json").write_text(
        json.dumps(
            {
                "id": "PERF-1",
                "status": "executing_benchmark",
                "custom_fields": {
                    "claim": {"owner": "source"},
                    "pending_approval": True,
                },
            }
        )
    )

    assert identity.import_state(str(source), str(destination), ["PERF-1"], False) == 1
    assert not (destination / "tickets/PERF-1.json").exists()
    identity.import_state(str(source), str(destination), ["PERF-1"], True)
    imported = json.loads((destination / "tickets/PERF-1.json").read_text())
    assert imported["status"] == "awaiting_customer_guidance"
    assert "claim" not in imported["custom_fields"]
    assert "pending_approval" not in imported["custom_fields"]
    assert imported["custom_fields"]["imported_fixture"] is True

    with pytest.raises(ValueError, match="destination ticket collision"):
        identity.import_state(str(source), str(destination), ["PERF-1"], False)


@pytest.mark.parametrize(
    "ticket_id", ["../PERF-1", "nested/PERF-1", "\\PERF-1", ".", ".."]
)
def test_import_rejects_path_traversal_ticket_ids(
    tmp_path: Path, ticket_id: str
) -> None:
    source, _ = _instance(tmp_path, "source", 18105)
    destination, _ = _instance(tmp_path, "destination", 18106)

    with pytest.raises(ValueError, match="invalid ticket ID"):
        identity.import_state(str(source), str(destination), [ticket_id], False)


def test_import_rejects_record_id_mismatch(tmp_path: Path) -> None:
    source, _ = _instance(tmp_path, "source", 18107)
    destination, _ = _instance(tmp_path, "destination", 18108)
    tickets = source / "tickets"
    tickets.mkdir()
    (tickets / "PERF-1.json").write_text(json.dumps({"id": "PERF-2"}))

    with pytest.raises(ValueError, match="source ticket ID mismatch"):
        identity.import_state(str(source), str(destination), ["PERF-1"], False)


def test_imported_fixture_requires_review_before_dispatch_or_resume(
    tmp_path: Path,
) -> None:
    store = TicketStore(persist_dir=tmp_path / "store")
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="fixture",
            description="fixture",
            custom_fields={"imported_fixture": True},
        )
    )
    stored = store._tickets[ticket.id]
    stored.status = TicketStatus.AWAITING_CUSTOMER_GUIDANCE
    stored.previous_status = None
    store._persist_ticket(stored)

    with pytest.raises(InvalidTransition, match="reviewed_resume"):
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
    with pytest.raises(TicketDispatchBlocked, match="non-dispatchable"):
        store.claim_ticket(ticket.id, "test-owner")

    resumed = store.transition_ticket(
        ticket.id,
        TransitionRequest(status="triage_pending", reviewed_resume=True),
        triggered_by="reviewer",
        reviewer_authorized=True,
    )
    assert (
        resumed.custom_fields["imported_fixture_reviewed"]["reviewed_by"] == "reviewer"
    )
    assert store.claim_ticket(ticket.id, "test-owner")["owner"] == "test-owner"


@pytest.mark.parametrize(
    "fields",
    [
        {"imported_fixture": False},
        {"imported_fixture_reviewed": {"reviewed_by": "attacker"}},
        {"import_provenance": {}},
        {"custom_fields": {"imported_fixture": False}},
        {"importedFixture": False},
    ],
)
def test_generic_updates_cannot_mutate_fixture_controls(
    tmp_path: Path, fields: dict
) -> None:
    store = TicketStore(persist_dir=tmp_path / "store")
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="fixture",
            description="fixture",
            custom_fields={"imported_fixture": True},
        )
    )

    with pytest.raises(ValueError, match="fixture control"):
        store.update_fields(ticket.id, fields)
    assert store.get_ticket(ticket.id).custom_fields == {"imported_fixture": True}
    with pytest.raises(TicketDispatchBlocked, match="non-dispatchable"):
        store.claim_ticket(ticket.id, "attacker")
