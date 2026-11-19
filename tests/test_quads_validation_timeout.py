"""Tests for QUADS assignment validation polling (issue #1299).

QUADS validation can exceed one hour and may report a transient failure
before later succeeding, so ``poll_until_validated`` must wait up to two
hours by default. Successful validation returns immediately; exhaustion of
the (overridable) timeout remains an explicit ``TimeoutError``. Sleeps are
mocked so the tests do not wait in real time.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from providers.quads import QuadsClient


def _client_with_status_sequence(statuses: list[dict]) -> QuadsClient:
    client = QuadsClient.__new__(QuadsClient)
    client.get_assignment_status = AsyncMock(side_effect=statuses)
    return client


@pytest.mark.asyncio
async def test_default_timeout_is_two_hours():
    """The reservation wait must cover the observed >1h validation window."""
    signature = inspect.signature(QuadsClient.poll_until_validated)
    assert signature.parameters["timeout"].default == 7200
    assert signature.parameters["interval"].default == 120


@pytest.mark.asyncio
async def test_successful_validation_returns_immediately(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(
        asyncio, "sleep", AsyncMock(side_effect=lambda s: slept.append(s))
    )
    client = _client_with_status_sequence(
        [
            {"validated": False, "provisioned": False},
            {"validated": False, "provisioned": True},
            {"validated": True, "provisioned": True},
        ]
    )
    status = await client.poll_until_validated(42, interval=120, timeout=7200)
    assert status == {"validated": True, "provisioned": True}
    # Two failed polls slept twice before the third returned.
    assert slept == [120, 120]
    assert client.get_assignment_status.await_count == 3


@pytest.mark.asyncio
async def test_transient_failure_then_success_within_window(monkeypatch):
    """QUADS may report a transient failure before later succeeding."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = _client_with_status_sequence(
        [
            {"validated": False, "provisioned": False},
            {"validated": False, "provisioned": False},
            {"validated": True, "provisioned": True},
        ]
    )
    status = await client.poll_until_validated(7, interval=120, timeout=7200)
    assert status["validated"] is True


@pytest.mark.asyncio
async def test_timeout_exhaustion_raises_after_configured_window(monkeypatch):
    """An explicit small timeout still raises TimeoutError at exhaustion."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    never_validated = {"validated": False, "provisioned": False}
    client = _client_with_status_sequence([])  # side_effect exhausted -> keep value
    client.get_assignment_status = AsyncMock(return_value=never_validated)
    with pytest.raises(TimeoutError, match="not validated after 240s"):
        await client.poll_until_validated(9, interval=120, timeout=240)
    # One poll per interval before the window is exhausted.
    assert client.get_assignment_status.await_count == 2
