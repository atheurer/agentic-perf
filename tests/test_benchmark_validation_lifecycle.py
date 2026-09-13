from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException

from agents.benchmark.server import (
    _execution_intent_digest,
    _execution_plan_fingerprint,
    _get_validated_runfile,
    _runfile_fingerprint,
    _validation_output_descriptor,
)
from providers.redaction import get_shared_redactor
from state_store.api.router import api_router
from state_store.api.validations import (
    create_validation,
    get_validation,
    issue_capability,
)
from state_store.auth import Principal, make_auth_dependency
from state_store.main import _set_audit_actor
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
        "validation_output": {
            "size_bytes": 2,
            "original_size_bytes": 2,
            "redacted_size_bytes": 2,
            "media_type": "text/plain",
            "digest": "a" * 64,
            "digest_kind": "sha256",
            "preview": "ok",
            "truncated": False,
            "redaction_applied": False,
        },
    }


def _ticket(store: TicketStore) -> str:
    return store.create_ticket(
        CreateTicketRequest(summary="validate", description="validate")
    ).id


def test_validation_output_uses_shared_redactor_for_all_persisted_surfaces():
    ticket_id = "PERF-shared-validation-redaction"
    sentinel = "arbitrary-non-regex-secret-821"
    get_shared_redactor().register(ticket_id, "provider/value", sentinel)
    descriptor = _validation_output_descriptor(ticket_id, sentinel * 300)
    encoded = str(descriptor)
    assert sentinel not in encoded
    assert "REDACTED" in encoded
    assert descriptor["blob_ref"]
    from paths import TRACE_PAYLOAD_DIR

    blob = TRACE_PAYLOAD_DIR / descriptor["blob_ref"].split(":", 1)[1]
    assert sentinel.encode() not in blob.read_bytes()


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
    assert len(winners) == 2
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
    with pytest.raises(ValueError, match="immutable"):
        store.update_fields(ticket_id, {"benchmark_validation_manifest": {}})


def test_ticket_creation_cannot_preseed_validation_manifest(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        store.create_ticket(
            CreateTicketRequest(
                summary="forged",
                description="forged",
                custom_fields={"benchmark_validations": {}},
            )
        )


def test_user_cannot_forge_controller_validation_post(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    record = _record("val-" + "a" * 32)
    body_record = dict(record)
    body_record.pop("creator")
    body_record.pop("server_pid")
    body = CreateValidationRequest(record=body_record, expected_version=0)
    request = SimpleNamespace(
        state=SimpleNamespace(principal=Principal("user", "writer", False)),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(store=store, multi_user=False)),
    )
    with pytest.raises(HTTPException) as error:
        create_validation(ticket_id, body, request)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("harness", "evil"),
        ("controller", "evil.controller"),
        ("params_fingerprint", "b" * 64),
        ("execution_plan_fingerprint", "b" * 64),
        ("run_command", "evil --steal-token"),
        ("validator_command", "evil-validator"),
        ("validator_version", "evil-version"),
        ("creator", {"invocation_id": "evil", "request_id": "evil"}),
        (
            "validation_output",
            {
                "size_bytes": 2,
                "original_size_bytes": 2,
                "redacted_size_bytes": 2,
                "media_type": "text/plain",
                "digest": "b" * 64,
                "digest_kind": "sha256",
                "preview": "ok",
                "truncated": False,
                "redaction_applied": False,
            },
        ),
    ],
)
def test_duplicate_validation_id_requires_canonical_whole_record_match(
    tmp_path, field, value
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _ticket(store)
    original = _record("val-" + "a" * 32)
    store.create_validation(ticket_id, original, 0)
    changed = dict(original)
    changed[field] = value
    _, conflict = store.create_validation(ticket_id, changed, 0)
    assert conflict is not None
    assert (
        store.get_ticket(ticket_id).custom_fields["benchmark_validations"]["version"]
        == 1
    )


def test_validation_get_requires_ticket_access_for_multi_user(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = store.create_ticket(
        CreateTicketRequest(summary="owned", description="owned"),
        owners=["alice"],
    ).id
    record = _record("val-" + "b" * 32)
    store.create_validation(ticket_id, record, 0)

    def request(principal):
        return SimpleNamespace(
            state=SimpleNamespace(principal=principal),
            app=SimpleNamespace(state=SimpleNamespace(store=store, multi_user=True)),
        )

    assert (
        get_validation(
            ticket_id,
            record["validation_id"],
            request(Principal("user", "alice", False)),
        )["record"]["validation_id"]
        == record["validation_id"]
    )
    with pytest.raises(HTTPException, match="not an owner"):
        get_validation(
            ticket_id, record["validation_id"], request(Principal("user", "bob", False))
        )
    assert (
        get_validation(
            ticket_id,
            record["validation_id"],
            request(Principal("user", "admin", True)),
        )["record"]["validation_id"]
        == record["validation_id"]
    )
    with pytest.raises(HTTPException, match="Anonymous"):
        get_validation(
            ticket_id,
            record["validation_id"],
            request(Principal("anonymous", "anonymous", False)),
        )


def test_validation_route_constructs_server_authoritative_creator(tmp_path):
    app = _app(tmp_path)
    ticket_id = _ticket(app.state.store)
    headers = {
        "X-Agentic-Perf-Benchmark-Validator": "validator",
        "X-Agentic-Perf-Agent-Id": "benchmark",
        "X-Agentic-Perf-Invocation-Id": "invocation",
        "X-Agentic-Perf-Action-Id": "action-1",
        "X-Agentic-Perf-Request-Id": "request-1",
    }
    request = SimpleNamespace(
        state=SimpleNamespace(principal=Principal("service", "deployment", True)),
        headers=headers,
        app=app,
    )
    capability = issue_capability(ticket_id, request)["capability"]
    record = _record("val-" + "c" * 32)
    record.pop("creator")
    record.pop("server_pid")
    body = CreateValidationRequest(record=record, expected_version=0)
    request.headers = headers | {"X-Agentic-Perf-Validation-Capability": capability}
    result = create_validation(ticket_id, body, request)
    assert result["record"]["creator"] == {
        "agent_id": "benchmark",
        "invocation_id": "invocation",
        "action_id": "action-1",
        "request_id": "request-1",
    }


def _app(tmp_path) -> FastAPI:
    class _Trace:
        def insert_event_result(self, event):
            return None

        def put_payload_descriptor(self, descriptor):
            return None

    app = FastAPI()
    app.state.store = TicketStore(tmp_path / "tickets", trace_store=_Trace())
    # Keep the HTTP authorization test focused and avoid blocking Python 3.14's
    # worker thread on fsync for this synthetic in-memory application.
    app.state.store._persist_ticket = lambda ticket: None
    app.state.multi_user = False
    app.state.benchmark_validator_token = "validator"
    app.state.benchmark_validation_capabilities = {}
    app.include_router(
        api_router,
        dependencies=[
            Depends(make_auth_dependency("service")),
            Depends(_set_audit_actor),
        ],
    )
    return app


async def test_validation_http_capability_is_bound_one_time_and_idempotent(tmp_path):
    """Exercise real HTTP auth and capability binding without TestClient."""
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        auth = {"Authorization": "Bearer service"}
        ticket_one = (
            await client.post(
                "/api/v1/tickets",
                json={"summary": "one", "description": "one"},
                headers=auth,
            )
        ).json()["id"]
        ticket_two = (
            await client.post(
                "/api/v1/tickets",
                json={"summary": "two", "description": "two"},
                headers=auth,
            )
        ).json()["id"]
        missing_action = await client.post(
            f"/api/v1/tickets/{ticket_one}/validations/capability",
            headers=auth
            | {
                "X-Agentic-Perf-Benchmark-Validator": "validator",
                "X-Agentic-Perf-Agent-Id": "benchmark",
                "X-Agentic-Perf-Invocation-Id": "invocation",
            },
        )
        assert missing_action.status_code == 403

        def headers(
            invocation: str = "invocation",
            action: str = "action-1",
            agent: str = "benchmark",
        ) -> dict[str, str]:
            return auth | {
                "X-Agentic-Perf-Benchmark-Validator": "validator",
                "X-Agentic-Perf-Agent-Id": agent,
                "X-Agentic-Perf-Invocation-Id": invocation,
                "X-Agentic-Perf-Action-Id": action,
                "X-Agentic-Perf-Request-Id": "request-1",
            }

        async def capability(ticket_id: str, invocation: str = "invocation") -> str:
            response = await client.post(
                f"/api/v1/tickets/{ticket_id}/validations/capability",
                headers=headers(invocation),
            )
            assert response.status_code == 200
            return response.json()["capability"]

        async def create(
            ticket_id: str,
            record: dict,
            cap: str,
            invocation: str = "invocation",
            agent: str = "benchmark",
            action: str = "action-1",
        ):
            client_record = dict(record)
            client_record.pop("creator", None)
            client_record.pop("server_pid", None)
            return await client.post(
                f"/api/v1/tickets/{ticket_id}/validations",
                json={"record": client_record, "expected_version": 0},
                headers=headers(invocation, action, agent)
                | {
                    "X-Agentic-Perf-Validation-Capability": cap,
                },
            )

        first, second = _record("val-" + "1" * 32), _record("val-" + "2" * 32, value=2)
        one, two = (
            await create(ticket_one, first, await capability(ticket_one)),
            await create(ticket_one, second, await capability(ticket_one)),
        )
        assert one.status_code == two.status_code == 200
        assert (
            await client.get(
                f"/api/v1/tickets/{ticket_one}/validations/{first['validation_id']}",
                headers=auth,
            )
        ).json()["record"]["validation_id"] == first["validation_id"]
        persisted = (
            await client.get(
                f"/api/v1/tickets/{ticket_one}/validations/{first['validation_id']}",
                headers=auth,
            )
        ).json()["record"]
        assert persisted["creator"] == {
            "agent_id": "benchmark",
            "invocation_id": "invocation",
            "action_id": "action-1",
            "request_id": "request-1",
        }
        # Concurrent distinct records append without losing either exact ID.
        third = _record("val-" + "3" * 32, value=3)
        fourth = _record("val-" + "4" * 32, value=4)
        caps = await asyncio.gather(
            capability(ticket_one, "concurrent-a"),
            capability(ticket_one, "concurrent-b"),
        )
        results = await asyncio.gather(
            create(ticket_one, third, caps[0], "concurrent-a"),
            create(ticket_one, fourth, caps[1], "concurrent-b"),
        )
        assert [response.status_code for response in results] == [200, 200]
        for record in (third, fourth):
            response = await client.get(
                f"/api/v1/tickets/{ticket_one}/validations/{record['validation_id']}",
                headers=auth,
            )
            assert response.status_code == 200
            assert response.json()["record"]["validation_id"] == record["validation_id"]
        # Same ID and immutable hashes are idempotent; changing the output hash conflicts.
        same = await create(ticket_one, first, await capability(ticket_one))
        assert same.status_code == 200
        altered = _record(first["validation_id"])
        altered["validation_output"]["digest"] = "b" * 64
        mismatch = await create(ticket_one, altered, await capability(ticket_one))
        assert mismatch.status_code == 409
        # A consumed capability cannot be replayed, and is ticket/agent/invocation bound.
        cap = await capability(ticket_one)
        assert (await create(ticket_two, first, cap)).status_code == 403
        # Invalid scope attempts do not burn a still-valid capability.
        assert (await create(ticket_one, first, cap)).status_code == 200
        assert (await create(ticket_one, first, cap)).status_code == 403
        cap = await capability(ticket_one)
        assert (await create(ticket_one, first, cap, agent="other")).status_code == 403
        cap = await capability(ticket_one)
        assert (
            await create(ticket_one, first, cap, action="action-2")
        ).status_code == 403
        cap = await capability(ticket_one)
        app.state.benchmark_validation_capabilities[cap]["expires_at"] = 0
        assert (await create(ticket_one, first, cap)).status_code == 403
