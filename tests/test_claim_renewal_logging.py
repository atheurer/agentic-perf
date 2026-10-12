"""Tests for claim renewal rejection logging and cancellation reason messages.

Covers:
- renew_claim logs HTTP status and response body on non-200
- CancelledError handler distinguishes user stop, deposed, and unknown causes
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.dispatcher import Dispatcher


def _make_dispatcher(**kwargs) -> Dispatcher:
    d = Dispatcher(
        state_store_url="http://store",
        llm_provider=MagicMock(),
        skill_provider=MagicMock(),
        **kwargs,
    )
    return d


# ---------------------------------------------------------------------------
# 1. renew_claim logs HTTP status + body on rejection
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def post(self, url, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_renew_claim_logs_rejection_reason(caplog):
    d = _make_dispatcher()
    d._claim_ids["T-1"] = "claim-1"
    response = _FakeResponse(409, "Conflict: claim owned by other instance")

    with patch(
        "providers.execution.AuditedAsyncHTTPClient",
        return_value=_FakeClient(response),
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator.dispatcher"):
            result = await d.renew_claim("T-1")

    assert result is False
    assert "HTTP 409" in caplog.text
    assert "Conflict: claim owned by other instance" in caplog.text
    assert "T-1" in caplog.text


@pytest.mark.asyncio
async def test_renew_claim_no_warning_on_success(caplog):
    d = _make_dispatcher()
    d._claim_ids["T-1"] = "claim-1"
    response = _FakeResponse(200)

    with patch(
        "providers.execution.AuditedAsyncHTTPClient",
        return_value=_FakeClient(response),
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator.dispatcher"):
            result = await d.renew_claim("T-1")

    assert result is True
    assert "Claim renewal rejected" not in caplog.text


# ---------------------------------------------------------------------------
# 2. stop_agent tracks stopped tickets; was_stopped_by_user works
# ---------------------------------------------------------------------------


def test_was_stopped_by_user_after_hard_stop():
    d = _make_dispatcher()
    # Create a dummy task
    loop = asyncio.new_event_loop()
    task = loop.create_task(asyncio.sleep(100))
    d._tasks["T-1"] = task

    d.stop_agent("T-1", mode="hard")

    assert d.was_stopped_by_user("T-1") is True
    assert d.was_stopped_by_user("T-999") is False

    task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


# ---------------------------------------------------------------------------
# 3. CancelledError handler produces correct cancel_reason messages
# ---------------------------------------------------------------------------


def test_cancel_reason_user_stop():
    """The production classifier reports user initiated cancellation."""
    from orchestrator.main import _cancellation_reason

    d = _make_dispatcher()
    d._stopped_tickets.add("T-1")

    assert _cancellation_reason(d, "T-1") == "Agent stopped by user request"


def test_cancel_reason_claim_lost():
    """A lost control-plane claim is distinguished from generic cancellation."""
    from orchestrator.main import _cancellation_reason

    d = _make_dispatcher()
    d.mark_deposed()

    assert _cancellation_reason(d, "T-1") == "Agent stopped: orchestrator claim lost"


@pytest.mark.asyncio
async def test_cancel_reason_shutdown_is_not_claim_loss():
    """Normal dispatcher shutdown is a generic cancellation, not claim loss."""
    from orchestrator.main import _cancellation_reason

    d = _make_dispatcher()
    await d.shutdown()

    assert d.is_deposed() is True
    assert d.has_lost_claim() is False
    assert _cancellation_reason(d, "T-1") == "Agent stopped: task cancelled"
