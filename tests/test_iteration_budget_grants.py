"""Iteration grants survive retries, phase changes, and store restarts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from providers.iteration_budget import ITERATION_BUDGET_FIELD
from state_store.api.router import api_router
from state_store.api.tickets import _iteration_limits as resolve_iteration_limits
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.models import (
    ApprovalRequest,
    CreateTicketRequest,
    TicketStatus,
    TransitionRequest,
)
from state_store.store import TicketStore

AGENT_LIMITS = {"benchmark": 20, "review": 20}


def _benchmark_ticket(
    store: TicketStore, *, fields: dict | None = None, owners: list[str] | None = None
) -> str:
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="Iteration allowance",
            description="Repeatable benchmark",
            custom_fields=fields or {},
            owners=owners,
        ),
        owners=owners,
    )
    for status in (
        TicketStatus.TRIAGE_PENDING,
        TicketStatus.AWAITING_HARDWARE,
        TicketStatus.AWAITING_PROVISION,
        TicketStatus.EXECUTING_BENCHMARK,
    ):
        store.transition_ticket(ticket.id, TransitionRequest(status=status))
    return ticket.id


def _grant(
    store: TicketStore,
    ticket_id: str,
    additional: int,
    request_id: str,
    **kwargs,
):
    return store.extend_iterations(
        ticket_id,
        additional,
        request_id,
        configured_agent_limits=kwargs.pop("configured_agent_limits", AGENT_LIMITS),
        configured_global_limit=kwargs.pop("configured_global_limit", 100),
        **kwargs,
    )


def test_multiple_grants_are_cumulative_and_stay_with_their_agents(tmp_path: Path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "benchmark-first")

    # A second grant while paused must resolve back to the same agent.
    store.transition_ticket(
        ticket_id,
        TransitionRequest(status=TicketStatus.AWAITING_CUSTOMER_GUIDANCE),
    )
    _grant(store, ticket_id, 20, "benchmark-second")
    store.transition_ticket(
        ticket_id, TransitionRequest(status=TicketStatus.EXECUTING_BENCHMARK)
    )
    store.transition_ticket(
        ticket_id, TransitionRequest(status=TicketStatus.AWAITING_REVIEW)
    )
    result = _grant(store, ticket_id, 10, "review-first")

    budget = result.custom_fields[ITERATION_BUDGET_FIELD]
    assert budget["agent_limits"] == {"benchmark": 70, "review": 30}
    assert result.custom_fields["global_max_iterations_override"] == 160
    assert result.custom_fields["max_iterations_override"] == 60
    assert set(budget["receipts"]) == {
        "benchmark-first",
        "benchmark-second",
        "review-first",
    }


def test_replay_after_restart_and_phase_change_retains_original_target(tmp_path: Path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "once")
    store.transition_ticket(
        ticket_id, TransitionRequest(status=TicketStatus.AWAITING_REVIEW)
    )
    # A later manual ceiling remains authoritative when a client retries an
    # older grant whose response was lost.
    store.update_fields(ticket_id, {"global_max_iterations_override": 250})

    restarted = TicketStore(persist_dir=tmp_path)
    before = restarted.get_ticket(ticket_id)
    result = _grant(restarted, ticket_id, 30, "once")

    assert result.custom_fields == before.custom_fields
    assert result.updated_at == before.updated_at
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 50
    }
    assert result.custom_fields["global_max_iterations_override"] == 250


@pytest.mark.parametrize("additional,agent_type", [(31, None), (30, "review")])
def test_request_id_cannot_be_reused_for_a_different_grant(
    tmp_path: Path, additional: int, agent_type: str | None
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "same-id")
    before = store.get_ticket(ticket_id).model_dump()

    with pytest.raises(ValueError, match="request_id"):
        _grant(store, ticket_id, additional, "same-id", agent_type=agent_type)

    assert store.get_ticket(ticket_id).model_dump() == before


def test_concurrent_grants_and_duplicate_requests_do_not_lose_or_repeat_credit(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    # Synchronous API handlers can reach the store concurrently. Each receipt
    # appears twice to model overlapping retries as well as independent grants.
    requests = [(f"grant-{index}", 1) for index in range(20)] * 2

    def apply(request: tuple[str, int]) -> None:
        request_id, delta = request
        _grant(store, ticket_id, delta, request_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(apply, requests))

    result = TicketStore(persist_dir=tmp_path).get_ticket(ticket_id)
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 40
    }
    assert result.custom_fields["global_max_iterations_override"] == 120
    assert result.custom_fields["max_iterations_override"] == 20


@pytest.mark.parametrize("pending_legacy", [None, 10])
def test_failed_grant_persistence_does_not_publish_receipt_or_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending_legacy: int | None
):
    store = TicketStore(persist_dir=tmp_path)
    fields = (
        {"max_iterations_override": pending_legacy}
        if pending_legacy is not None
        else {}
    )
    ticket_id = _benchmark_ticket(store, fields=fields)
    before = store.get_ticket(ticket_id).model_dump()

    def fail_persist(*_args, **_kwargs):
        raise OSError("simulated full disk")

    with monkeypatch.context() as failure:
        failure.setattr(store, "_persist_ticket", fail_persist)
        with pytest.raises(OSError, match="full disk"):
            _grant(store, ticket_id, 30, "retry-after-write-failure")
        assert store.get_ticket(ticket_id).model_dump() == before

    assert (
        TicketStore(persist_dir=tmp_path).get_ticket(ticket_id).model_dump() == before
    )
    _grant(store, ticket_id, 30, "retry-after-write-failure")
    result = _grant(store, ticket_id, 30, "retry-after-write-failure")
    total = 30 + (pending_legacy or 0)
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 20 + total
    }
    assert result.custom_fields["global_max_iterations_override"] == 100 + total
    assert result.custom_fields["max_iterations_override"] == total
    assert list(result.custom_fields[ITERATION_BUDGET_FIELD]["receipts"]) == [
        "retry-after-write-failure"
    ]
    assert TicketStore(persist_dir=tmp_path).get_ticket(ticket_id).custom_fields == (
        result.custom_fields
    )


def test_failed_legacy_setter_persistence_rolls_back_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    before = store.get_ticket(ticket_id).model_dump()

    def set_total():
        return store.update_fields(
            ticket_id,
            {"max_iterations_override": 30},
            iteration_limits=(AGENT_LIMITS, 100),
        )

    def fail_persist(*_args, **_kwargs):
        raise OSError("simulated full disk")

    with monkeypatch.context() as failure:
        failure.setattr(store, "_persist_ticket", fail_persist)
        with pytest.raises(OSError, match="full disk"):
            set_total()
        assert store.get_ticket(ticket_id).model_dump() == before

    set_total()
    result = set_total()
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 50
    }
    assert result.custom_fields["global_max_iterations_override"] == 130
    assert result.custom_fields["max_iterations_override"] == 30
    assert TicketStore(persist_dir=tmp_path).get_ticket(ticket_id).custom_fields == (
        result.custom_fields
    )


def test_post_write_failure_cannot_let_a_later_grant_overwrite_committed_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    persist = store._persist_ticket
    first_write = True

    def persist_then_fail_once(*args, **kwargs):
        nonlocal first_write
        persist(*args, **kwargs)
        if first_write:
            first_write = False
            raise OSError("response failed after ticket was written")

    monkeypatch.setattr(store, "_persist_ticket", persist_then_fail_once)
    with pytest.raises(OSError, match="after ticket was written"):
        _grant(store, ticket_id, 30, "grant-a")

    _grant(store, ticket_id, 20, "grant-b")
    result = _grant(store, ticket_id, 30, "grant-a")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 70
    }
    assert result.custom_fields["global_max_iterations_override"] == 150
    assert result.custom_fields["max_iterations_override"] == 50
    assert set(result.custom_fields[ITERATION_BUDGET_FIELD]["receipts"]) == {
        "grant-a",
        "grant-b",
    }
    assert TicketStore(persist_dir=tmp_path).get_ticket(ticket_id).custom_fields == (
        result.custom_fields
    )


def test_post_write_legacy_failure_cannot_be_overwritten_by_the_next_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    persist = store._persist_ticket
    first_write = True

    def persist_then_fail_once(*args, **kwargs):
        nonlocal first_write
        persist(*args, **kwargs)
        if first_write:
            first_write = False
            raise OSError("legacy response failed after ticket was written")

    def set_total(value: int):
        return store.update_fields(
            ticket_id,
            {"max_iterations_override": value},
            iteration_limits=(AGENT_LIMITS, 100),
        )

    monkeypatch.setattr(store, "_persist_ticket", persist_then_fail_once)
    with pytest.raises(OSError, match="after ticket was written"):
        set_total(30)
    result = _grant(store, ticket_id, 20, "after-legacy")
    # The legacy setter has no request ID. A retry superseded by a later
    # cumulative value must be rejected without subtracting either grant.
    with pytest.raises(ValueError, match="cannot decrease"):
        set_total(30)
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 70
    }
    assert result.custom_fields["global_max_iterations_override"] == 150
    assert result.custom_fields["max_iterations_override"] == 50
    assert TicketStore(persist_dir=tmp_path).get_ticket(ticket_id).custom_fields == (
        result.custom_fields
    )


@pytest.mark.parametrize("legacy_setter", [False, True])
def test_grant_operations_require_the_filesystem_write_to_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_setter: bool
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    before = store.get_ticket(ticket_id).model_dump()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated filesystem write failure")

    monkeypatch.setattr(
        store, "_filesystem", lambda _ticket_id: SimpleNamespace(write=fail_write)
    )
    # Exercise the real persistence helper: its legacy best-effort behavior
    # must not acknowledge a grant whose receipt and credit were not written.
    with pytest.raises(OSError, match="filesystem write failure"):
        if legacy_setter:
            store.update_fields(
                ticket_id,
                {"max_iterations_override": 30},
                iteration_limits=(AGENT_LIMITS, 100),
            )
        else:
            _grant(store, ticket_id, 30, "must-be-durable")
    assert store.get_ticket(ticket_id).model_dump() == before


def test_configured_and_explicit_ticket_limits_are_used_as_initial_ceilings(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store, fields={"global_max_iterations_override": 230})
    result = _grant(
        store,
        ticket_id,
        30,
        "custom",
        configured_agent_limits={"benchmark": 80, "review": 50},
        configured_global_limit=200,
    )
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 110
    }
    assert result.custom_fields["global_max_iterations_override"] == 260


@pytest.mark.parametrize("agent_base,global_base", [(0, 100), (20, 0), (0, 0)])
def test_unlimited_ceilings_remain_unlimited(
    tmp_path: Path, agent_base: int, global_base: int
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    result = _grant(
        store,
        ticket_id,
        30,
        "unlimited",
        configured_agent_limits={"benchmark": agent_base},
        configured_global_limit=global_base,
    )
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"][
        "benchmark"
    ] == (agent_base + 30 if agent_base else 0)
    assert result.custom_fields["global_max_iterations_override"] == (
        global_base + 30 if global_base else 0
    )


@pytest.mark.parametrize("context_field", ["investigation_ledger", "anomaly_context"])
def test_ticket_unlimited_policy_is_preserved_when_a_grant_is_added(
    tmp_path: Path, context_field: str
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store, fields={context_field: {"active": True}})
    result = _grant(store, ticket_id, 30, "investigation")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 0
    }
    assert result.custom_fields["global_max_iterations_override"] == 130


def test_existing_explicit_agent_ceiling_survives_later_unlimited_ticket_policy(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "explicit-ceiling")
    store.update_fields(ticket_id, {"investigation_ledger": {"active": True}})
    result = _grant(store, ticket_id, 10, "extend-explicit-ceiling")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 60
    }
    assert result.custom_fields["global_max_iterations_override"] == 140


def test_legacy_setter_adds_only_the_new_delta_and_cannot_erase_grants(tmp_path: Path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)

    def set_total(value: int | None):
        return store.update_fields(
            ticket_id,
            {"max_iterations_override": value},
            iteration_limits=(AGENT_LIMITS, 100),
        )

    set_total(30)
    set_total(30)
    set_total(50)
    set_total(None)  # Cleanup from an older orchestrator must not erase credit.
    store.transition_ticket(
        ticket_id, TransitionRequest(status=TicketStatus.AWAITING_REVIEW)
    )
    result = set_total(60)

    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 70,
        "review": 30,
    }
    assert result.custom_fields["global_max_iterations_override"] == 160
    assert result.custom_fields["max_iterations_override"] == 60
    with pytest.raises(ValueError, match="cannot decrease"):
        set_total(30)


def test_pending_legacy_scalar_is_materialized_once_before_a_new_grant(tmp_path: Path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store, fields={"max_iterations_override": 30})
    result = _grant(store, ticket_id, 0, "materialize")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 50
    }
    assert result.custom_fields["global_max_iterations_override"] == 130

    reloaded = TicketStore(persist_dir=tmp_path)
    _grant(reloaded, ticket_id, 0, "materialize-again")
    result = _grant(reloaded, ticket_id, 20, "new-grant")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 70
    }
    assert result.custom_fields["global_max_iterations_override"] == 150


def test_pending_legacy_zero_keeps_agent_unlimited_without_global_credit(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store, fields={"max_iterations_override": 0})
    result = _grant(store, ticket_id, 0, "materialize-zero")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 0
    }
    assert "global_max_iterations_override" not in result.custom_fields


@pytest.mark.parametrize("pending_legacy", [30, "invalid-pending-value"])
def test_legacy_clear_without_ledger_discards_pending_value_before_migration(
    tmp_path: Path, pending_legacy: object
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(
        store, fields={"max_iterations_override": pending_legacy}
    )
    result = store.update_fields(ticket_id, {"max_iterations_override": None})
    assert result.custom_fields.get("max_iterations_override") is None
    assert ITERATION_BUDGET_FIELD not in result.custom_fields

    # A subsequent explicit grant starts at the configured base and cannot
    # revive a stale legacy allowance that an operator already cleared.
    result = _grant(store, ticket_id, 20, "after-clear")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 40
    }
    assert result.custom_fields["global_max_iterations_override"] == 120


def test_legacy_zero_can_enable_unlimited_without_erasing_existing_grants(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "finite-grant")
    store.update_fields(
        ticket_id,
        {"max_iterations_override": 0},
        iteration_limits=(AGENT_LIMITS, 100),
    )
    result = _grant(store, ticket_id, 20, "after-unlimited")
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 0
    }
    assert result.custom_fields["global_max_iterations_override"] == 150
    assert result.custom_fields["max_iterations_override"] == 50


def test_unlimited_operation_targets_one_agent_and_replays_after_phase_change(
    tmp_path: Path,
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 30, "finite")
    result = _grant(store, ticket_id, 0, "unlimited", make_unlimited=True)
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 0
    }
    assert result.custom_fields["global_max_iterations_override"] == 130
    assert result.custom_fields["max_iterations_override"] == 30

    store.transition_ticket(
        ticket_id, TransitionRequest(status=TicketStatus.AWAITING_REVIEW)
    )
    _grant(store, ticket_id, 10, "review")
    result = _grant(store, ticket_id, 0, "unlimited", make_unlimited=True)
    assert result.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 0,
        "review": 30,
    }
    assert result.custom_fields["global_max_iterations_override"] == 140
    assert result.custom_fields["max_iterations_override"] == 40


@pytest.mark.parametrize("first_mode", [False, True])
def test_receipt_cannot_change_between_migration_and_unlimited_mode(
    tmp_path: Path, first_mode: bool
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    _grant(store, ticket_id, 0, "one-mode", make_unlimited=first_mode)
    before = store.get_ticket(ticket_id).model_dump()
    with pytest.raises(ValueError, match="request_id"):
        _grant(store, ticket_id, 0, "one-mode", make_unlimited=not first_mode)
    assert store.get_ticket(ticket_id).model_dump() == before


def test_unlimited_mode_rejects_a_positive_allowance_without_mutation(tmp_path: Path):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    before = store.get_ticket(ticket_id).model_dump()
    with pytest.raises(ValueError):
        _grant(store, ticket_id, 30, "ambiguous-mode", make_unlimited=True)
    assert store.get_ticket(ticket_id).model_dump() == before


@pytest.mark.parametrize("legacy_value", [0, 30])
def test_mixed_legacy_and_global_patch_is_rejected_before_mutation(
    tmp_path: Path, legacy_value: int
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    before = store.get_ticket(ticket_id).model_dump()
    with pytest.raises(ValueError):
        store.update_fields(
            ticket_id,
            {
                "max_iterations_override": legacy_value,
                "global_max_iterations_override": 200,
                "unrelated_note": "must not be published",
            },
            iteration_limits=(AGENT_LIMITS, 100),
        )
    assert store.get_ticket(ticket_id).model_dump() == before


def test_iteration_budget_receipts_cannot_be_forged_through_custom_fields(
    tmp_path: Path,
):
    with pytest.raises(ValueError, match="iteration budget"):
        CreateTicketRequest(
            summary="Forged",
            description="test",
            custom_fields={ITERATION_BUDGET_FIELD: {}},
        )

    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store)
    with pytest.raises(ValueError, match="iteration budget"):
        store.update_fields(ticket_id, {ITERATION_BUDGET_FIELD: {"receipts": {}}})
    assert ITERATION_BUDGET_FIELD not in store.get_ticket(ticket_id).custom_fields


@pytest.fixture
def grant_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    users = UserStore(tmp_path / "users.json")
    _, alice_token = users.create_user("alice")
    _, bob_token = users.create_user("bob")
    store = TicketStore(persist_dir=tmp_path / "tickets")
    ticket_id = _benchmark_ticket(store, owners=["alice"])
    app = FastAPI()
    app.state.store = store
    app.state.multi_user = True
    app.include_router(
        api_router,
        dependencies=[
            Depends(
                make_auth_dependency("test-service", multi_user=True, user_store=users)
            )
        ],
    )
    monkeypatch.setattr(
        "state_store.api.tickets._iteration_limits", lambda: (AGENT_LIMITS, 100)
    )
    return app, store, ticket_id, alice_token, bob_token


@pytest.mark.asyncio
async def test_grant_api_requires_authentication_and_ticket_ownership(grant_api):
    app, store, ticket_id, alice_token, bob_token = grant_api
    path = f"/api/v1/tickets/{ticket_id}/iteration-extensions"
    body = {"additional_iterations": 30, "request_id": "owner-grant"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post(path, json=body)
        denied = await client.post(
            path, json=body, headers={"Authorization": f"Bearer {bob_token}"}
        )
        allowed = await client.post(
            path, json=body, headers={"Authorization": f"Bearer {alice_token}"}
        )

    assert missing.status_code == 401
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert (
        store.get_ticket(ticket_id).custom_fields["global_max_iterations_override"]
        == 130
    )


@pytest.mark.asyncio
async def test_grant_api_uses_configuration_for_initial_agent_and_global_limits(
    grant_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app, _, ticket_id, alice_token, _ = grant_api
    config_file = tmp_path / "iteration-config.json"
    config_file.write_text(
        json.dumps(
            {"agent_iterations": {"benchmark": 80}, "global_max_iterations": 200}
        )
    )
    monkeypatch.setattr("orchestrator.config.CONFIG_PATH", config_file)
    monkeypatch.delenv("GLOBAL_MAX_ITERATIONS", raising=False)
    monkeypatch.setattr(
        "state_store.api.tickets._iteration_limits", resolve_iteration_limits
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={"additional_iterations": 30, "request_id": "configured-grant"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 200
    fields = result.json()["custom_fields"]
    assert fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {"benchmark": 110}
    assert fields["global_max_iterations_override"] == 230


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_setter", [False, True])
async def test_api_post_write_failure_preserves_credit_for_following_grant(
    grant_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_setter: bool
):
    app, store, ticket_id, alice_token, _ = grant_api
    extension_path = f"/api/v1/tickets/{ticket_id}/iteration-extensions"
    fields_path = f"/api/v1/tickets/{ticket_id}/fields"
    persist = store._persist_ticket
    first_write = True

    def persist_then_fail_once(*args, **kwargs):
        nonlocal first_write
        persist(*args, **kwargs)
        if first_write:
            first_write = False
            raise OSError("response lost after commit")

    monkeypatch.setattr(store, "_persist_ticket", persist_then_fail_once)
    headers = {"Authorization": f"Bearer {alice_token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        if legacy_setter:
            first = await client.patch(
                fields_path,
                json={"fields": {"max_iterations_override": 30}},
                headers=headers,
            )
        else:
            first = await client.post(
                extension_path,
                json={"additional_iterations": 30, "request_id": "api-grant-a"},
                headers=headers,
            )
        assert first.status_code >= 500
        second = await client.post(
            extension_path,
            json={"additional_iterations": 20, "request_id": "api-grant-b"},
            headers=headers,
        )
        assert second.status_code == 200
        if legacy_setter:
            replay = await client.patch(
                fields_path,
                json={"fields": {"max_iterations_override": 30}},
                headers=headers,
            )
            assert replay.status_code == 422
        else:
            replay = await client.post(
                extension_path,
                json={"additional_iterations": 30, "request_id": "api-grant-a"},
                headers=headers,
            )
            assert replay.status_code == 200

    fields = (
        TicketStore(persist_dir=tmp_path / "tickets")
        .get_ticket(ticket_id)
        .custom_fields
    )
    assert fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {"benchmark": 70}
    assert fields["global_max_iterations_override"] == 150
    assert fields["max_iterations_override"] == 50


@pytest.mark.asyncio
async def test_unlimited_api_sets_only_its_explicit_target_without_global_credit(
    grant_api,
):
    app, store, ticket_id, alice_token, _ = grant_api
    _grant(store, ticket_id, 30, "finite-api-baseline")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={
                "additional_iterations": 0,
                "request_id": "unlimited-review",
                "agent_type": "review",
                "make_unlimited": True,
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 200
    fields = result.json()["custom_fields"]
    assert fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 50,
        "review": 0,
    }
    assert fields["global_max_iterations_override"] == 130
    assert fields["max_iterations_override"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [1, "true", None])
async def test_unlimited_api_requires_a_boolean_mode(grant_api, mode):
    app, store, ticket_id, alice_token, _ = grant_api
    before = store.get_ticket(ticket_id).model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={
                "additional_iterations": 0,
                "request_id": "invalid-mode",
                "make_unlimited": mode,
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 422
    assert store.get_ticket(ticket_id).model_dump() == before


@pytest.mark.asyncio
async def test_unlimited_api_rejects_a_positive_addition(grant_api):
    app, store, ticket_id, alice_token, _ = grant_api
    before = store.get_ticket(ticket_id).model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={
                "additional_iterations": 30,
                "request_id": "incompatible-mode",
                "make_unlimited": True,
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 422
    assert store.get_ticket(ticket_id).model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_value", [0, 30])
async def test_api_rejects_mixed_legacy_and_global_patch_without_mutation(
    grant_api, legacy_value: int
):
    app, store, ticket_id, alice_token, _ = grant_api
    before = store.get_ticket(ticket_id).model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {
                    "max_iterations_override": legacy_value,
                    "global_max_iterations_override": 200,
                    "unrelated_note": "must not be published",
                }
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 422
    assert store.get_ticket(ticket_id).model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, "30", -1])
async def test_grant_api_rejects_non_integer_or_negative_allowance(grant_api, invalid):
    app, store, ticket_id, alice_token, _ = grant_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={"additional_iterations": invalid, "request_id": "invalid"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert result.status_code == 422
    assert ITERATION_BUDGET_FIELD not in store.get_ticket(ticket_id).custom_fields


@pytest.mark.asyncio
async def test_grant_api_rejects_stale_internal_fence_without_changing_limits(
    grant_api,
):
    app, store, ticket_id, _, _ = grant_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/api/v1/tickets/{ticket_id}/iteration-extensions",
            json={"additional_iterations": 30, "request_id": "stale-fence"},
            headers={
                "Authorization": "Bearer test-service",
                "X-Agentic-Perf-Orchestrator-Session": str(uuid4()),
                "X-Agentic-Perf-Orchestrator-Epoch": "1",
                "X-Agentic-Perf-Claim-Id": "no-active-claim",
            },
        )
    assert result.status_code == 409
    assert ITERATION_BUDGET_FIELD not in store.get_ticket(ticket_id).custom_fields


@pytest.mark.parametrize("operation", ["expire", "cancel", "usage"])
def test_bulk_mutations_cannot_overwrite_an_unresolved_committed_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
):
    store = TicketStore(persist_dir=tmp_path)
    ticket_id = _benchmark_ticket(store, fields={"_usage_summary": {"llm_calls": 12}})
    approval_id = "apr-" + "a" * 32
    approval = ApprovalRequest(
        approval_request_id=approval_id,
        ticket_id=ticket_id,
        validation_id="validation-fixture",
        presented_run_file_digest="a" * 64,
        execution_intent_digest="b" * 64,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    # Build an expired pending fixture without involving unrelated validation
    # setup; these are the records the lease-maintenance paths must retire.
    store._tickets[ticket_id].custom_fields["approval_requests"] = {
        approval_id: approval.model_dump(mode="json")
    }
    store._persist_ticket(store._tickets[ticket_id])
    persist = store._persist_ticket
    read_text = Path.read_text
    ticket_path = tmp_path / f"{ticket_id}.json"

    def persist_then_fail(*args, **kwargs):
        persist(*args, **kwargs)
        raise OSError("response failed after grant replacement")

    def unreadable_ticket(path, *args, **kwargs):
        if path == ticket_path:
            raise OSError("temporarily unreadable grant outcome")
        return read_text(path, *args, **kwargs)

    def mutate():
        if operation == "usage":
            store.invalidate_cached_usage_summary(ticket_id)
            return
        with store._lock:
            if operation == "expire":
                store._expire_all_pending_approvals_unlocked()
            else:
                store._cancel_pending_approvals_unlocked_all(reason="lease takeover")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_persist_ticket", persist_then_fail)
        patch.setattr(Path, "read_text", unreadable_ticket)
        with pytest.raises(OSError, match="after grant replacement"):
            _grant(store, ticket_id, 30, "committed-before-error")
        patch.setattr(store, "_persist_ticket", persist)
        with pytest.raises(OSError, match="temporarily unreadable"):
            mutate()

    # Restored readability must recover the committed candidate, including its
    # receipt, before maintenance can write that ticket again.
    recovered = store.get_ticket(ticket_id)
    assert recovered.custom_fields[ITERATION_BUDGET_FIELD]["agent_limits"] == {
        "benchmark": 50
    }
    assert (
        "committed-before-error"
        in recovered.custom_fields[ITERATION_BUDGET_FIELD]["receipts"]
    )
    assert recovered.custom_fields["global_max_iterations_override"] == 130
    assert (
        recovered.custom_fields["approval_requests"][approval_id]["status"] == "pending"
    )
    assert "_usage_summary" in recovered.custom_fields
    mutate()
    # Bulk cancellation changes the cached approval records; a subsequent
    # ordinary ticket write must retain the recovered allowance as well.
    store.update_fields(ticket_id, {"maintenance_complete": True})
    reloaded = TicketStore(persist_dir=tmp_path).get_ticket(ticket_id)
    assert reloaded.custom_fields["global_max_iterations_override"] == 130
    assert (
        "committed-before-error"
        in reloaded.custom_fields[ITERATION_BUDGET_FIELD]["receipts"]
    )
    if operation == "usage":
        assert "_usage_summary" not in reloaded.custom_fields
    else:
        expected = "expired" if operation == "expire" else "cancelled"
        assert (
            reloaded.custom_fields["approval_requests"][approval_id]["status"]
            == expected
        )
