from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from state_store.models import AcquireOrchestratorLeaseRequest
from state_store.store import OrchestratorLeaseHeld, TicketStore


def _request(session_id=None, *, instance_name="shared"):
    return AcquireOrchestratorLeaseRequest(
        session_id=session_id or uuid4(),
        instance_name=instance_name,
        host="host-a",
        pid=123,
        process_start_id="boot:123",
        ttl_seconds=10,
    )


def test_same_session_acquire_is_idempotent_and_other_session_is_rejected():
    with TemporaryDirectory() as directory:
        store = TicketStore(persist_dir=directory)
        first = _request()
        lease = store.acquire_orchestrator_lease(first)
        assert store.acquire_orchestrator_lease(first).epoch == lease.epoch
        with pytest.raises(OrchestratorLeaseHeld) as error:
            store.acquire_orchestrator_lease(
                _request(instance_name=first.instance_name)
            )
        assert error.value.holder.session_id == first.session_id
        assert error.value.remaining_seconds > 0


def test_expiry_takeover_fences_old_session_and_survives_restart():
    now = [datetime.now(timezone.utc)]
    with TemporaryDirectory() as directory:
        first_store = TicketStore(persist_dir=directory, clock=lambda: now[0])
        first = _request()
        lease = first_store.acquire_orchestrator_lease(first)
        restarted = TicketStore(persist_dir=directory, clock=lambda: now[0])
        assert restarted.get_orchestrator_lease().session_id == first.session_id
        now[0] += timedelta(seconds=11)
        second = _request()
        replacement = restarted.acquire_orchestrator_lease(second)
        assert replacement.epoch > lease.epoch
        assert not restarted.release_orchestrator_lease(first.session_id, lease.epoch)
        with pytest.raises(PermissionError):
            restarted.renew_orchestrator_lease(first.session_id, lease.epoch, 10)
