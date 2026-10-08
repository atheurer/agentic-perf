"""Tests for the resource server post-allocation guardrail (#1128).

After reserve_resources succeeds, list_resource_providers and
check_available_resources must return an error directing the LLM
to call submit_resource_result instead of looping.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers to reset module-level state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset the resource server's module-level globals before each test."""
    import agents.resource.server as srv

    srv._resources_allocated = False
    srv._reservation_uncertain = False
    srv._reservation_failures = 0
    srv._initialized = False
    srv._registry = None
    srv._ssh = None
    srv._ticket = {}
    srv._fleet_next_device = None
    srv._last_reservation = {}
    srv._host_inventory = {}
    yield
    srv._resources_allocated = False
    srv._reservation_uncertain = False
    srv._reservation_failures = 0


# ---------------------------------------------------------------------------
# Tests: discovery calls blocked after allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_resource_providers_blocked_after_allocation():
    """list_resource_providers returns an error when resources are allocated."""
    import agents.resource.server as srv

    srv._resources_allocated = True

    raw = await srv.list_resource_providers()
    result = json.loads(raw)

    assert result["already_allocated"] is True
    assert "submit_resource_result" in result["error"]


@pytest.mark.asyncio
async def test_check_available_resources_blocked_after_allocation():
    """check_available_resources returns an error when resources are allocated."""
    import agents.resource.server as srv

    srv._resources_allocated = True

    raw = await srv.check_available_resources(provider="jumpstarter")
    result = json.loads(raw)

    assert result["already_allocated"] is True
    assert "submit_resource_result" in result["error"]


@pytest.mark.asyncio
async def test_discovery_allowed_before_allocation():
    """Discovery calls work normally before reserve_resources succeeds."""
    import agents.resource.server as srv

    mock_registry = MagicMock()
    mock_registry.list_configured_providers = AsyncMock(
        return_value=[{"name": "jumpstarter", "type": "bare_metal"}]
    )

    srv._initialized = True
    srv._registry = mock_registry

    raw = await srv.list_resource_providers()
    result = json.loads(raw)

    assert "already_allocated" not in result
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# Tests: reserve_resources sets the guardrail flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_resources_sets_allocated_flag():
    """A successful reserve_resources call sets _resources_allocated."""
    import agents.resource.server as srv

    mock_provider = AsyncMock()
    mock_provider.reserve = AsyncMock(
        return_value={
            "reservation_id": "lease-123",
            "hosts": [],
            "provider_metadata": {},
        }
    )

    mock_registry = MagicMock()
    mock_registry.get_provider = AsyncMock(return_value=mock_provider)

    srv._initialized = True
    srv._registry = mock_registry
    srv._ticket = {"custom_fields": {}}

    assert srv._resources_allocated is False

    await srv.reserve_resources(
        provider="jumpstarter",
        selection={"jumpstarter_selector": "board=test"},
        description="test reservation",
    )

    assert srv._resources_allocated is True


@pytest.mark.asyncio
async def test_reserve_resources_error_does_not_set_flag():
    """A failed reserve_resources call does NOT set _resources_allocated."""
    import agents.resource.server as srv

    mock_provider = AsyncMock()
    mock_provider.reserve = AsyncMock(
        return_value={
            "error": "No devices available",
        }
    )

    mock_registry = MagicMock()
    mock_registry.get_provider = AsyncMock(return_value=mock_provider)

    srv._initialized = True
    srv._registry = mock_registry
    srv._ticket = {"custom_fields": {}}

    await srv.reserve_resources(
        provider="jumpstarter",
        selection={"jumpstarter_selector": "board=test"},
        description="test reservation",
    )

    assert srv._resources_allocated is False


@pytest.mark.asyncio
async def test_status_only_reservation_failure_does_not_set_flag():
    """A provider's failed status is failure even without an error field."""
    import agents.resource.server as srv

    mock_provider = AsyncMock()
    mock_provider.reserve = AsyncMock(
        return_value={
            "status": "failed",
            "reservation_id": "",
            "provider_metadata": {},
            "message": "No capacity",
        }
    )
    mock_registry = MagicMock()
    mock_registry.get_provider = AsyncMock(return_value=mock_provider)

    srv._initialized = True
    srv._registry = mock_registry
    srv._ticket = {"custom_fields": {}}

    raw = await srv.reserve_resources(
        provider="quads",
        selection={"hostnames": ["host-01"]},
        description="test",
        ticket_id="PERF-TEST",
    )
    result = json.loads(raw)

    assert result["status"] == "failed"
    assert srv._resources_allocated is False
    assert srv._reservation_failures == 1


# ---------------------------------------------------------------------------
# Tests: reserve_resources still works after flag is set (multi-call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_resources_allowed_after_first_allocation():
    """reserve_resources is NOT blocked by the guardrail (multi-call support)."""
    import agents.resource.server as srv

    mock_provider = AsyncMock()
    mock_provider.reserve = AsyncMock(
        return_value={
            "reservation_id": "lease-456",
            "hosts": ["10.0.0.2"],
            "provider_metadata": {},
        }
    )

    mock_registry = MagicMock()
    mock_registry.get_provider = AsyncMock(return_value=mock_provider)

    srv._initialized = True
    srv._registry = mock_registry
    srv._ticket = {"custom_fields": {}}
    srv._resources_allocated = True  # Already allocated

    raw = await srv.reserve_resources(
        provider="aws",
        selection={"instance_type": "m5.xlarge", "count": 1},
        description="second reservation",
    )
    result = json.loads(raw)

    # Should succeed, not be blocked
    assert "already_allocated" not in result
    assert result["reservation_id"] == "lease-456"


class TestReservationFailureTracking:
    """Track consecutive reservation failures and guide the agent."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        import agents.resource.server as srv

        srv._resources_allocated = False
        srv._reservation_uncertain = False
        srv._reservation_failures = 0
        srv._initialized = True
        srv._ticket = {"custom_fields": {}}
        yield
        srv._resources_allocated = False
        srv._reservation_uncertain = False
        srv._reservation_failures = 0

    def _setup_registry(self, reserve_fn):
        import agents.resource.server as srv

        mock_provider = AsyncMock()
        mock_provider.reserve = AsyncMock(side_effect=reserve_fn)
        mock_registry = MagicMock()
        mock_registry.get_provider = AsyncMock(return_value=mock_provider)
        srv._registry = mock_registry

    @pytest.mark.asyncio
    async def test_single_failure_suggests_retry(self):
        """First failure should suggest trying a different board."""
        import agents.resource.server as srv

        async def mock_reserve(*a, **kw):
            return {"error": "Device unavailable"}

        self._setup_registry(mock_reserve)
        result = json.loads(
            await srv.reserve_resources(
                provider="jumpstarter",
                selection={"jumpstarter_selector": "board=test"},
                description="test",
                ticket_id="PERF-TEST",
            )
        )
        assert "retry_suggestion" in result
        assert srv._reservation_failures == 1
        assert not srv._resources_allocated

    @pytest.mark.asyncio
    async def test_repeated_failures_signal_exhaustion(self):
        """After N consecutive failures, signal repeated_failure."""
        import agents.resource.server as srv

        async def mock_reserve(*a, **kw):
            return {"error": "Device unavailable"}

        self._setup_registry(mock_reserve)
        for _ in range(srv._MAX_RESERVATION_FAILURES):
            result = json.loads(
                await srv.reserve_resources(
                    provider="jumpstarter",
                    selection={"jumpstarter_selector": "board=test"},
                    description="test",
                    ticket_id="PERF-TEST",
                )
            )
        assert result.get("repeated_failure") is True
        assert "submit_resource_result" in result.get("message", "")
        assert srv._reservation_failures == srv._MAX_RESERVATION_FAILURES

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        """A successful reservation should reset the failure counter."""
        import agents.resource.server as srv

        call_count = 0

        async def mock_reserve(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return {"error": "Device unavailable"}
            return {
                "status": "active",
                "lease_id": "test-123",
                "provider_metadata": {},
            }

        self._setup_registry(mock_reserve)
        # Two failures
        await srv.reserve_resources(
            provider="jumpstarter",
            selection={"jumpstarter_selector": "board=test"},
            description="test",
            ticket_id="PERF-TEST",
        )
        await srv.reserve_resources(
            provider="jumpstarter",
            selection={"jumpstarter_selector": "board=test"},
            description="test",
            ticket_id="PERF-TEST",
        )
        assert srv._reservation_failures == 2
        # Then success
        await srv.reserve_resources(
            provider="jumpstarter",
            selection={"jumpstarter_selector": "board=test"},
            description="test",
            ticket_id="PERF-TEST",
        )
        assert srv._reservation_failures == 0
        assert srv._resources_allocated is True

    @pytest.mark.asyncio
    async def test_provider_exception_marks_outcome_unknown_and_blocks_retry(self):
        """An exception after side effects must not trigger duplicate allocation."""
        import agents.resource.server as srv

        side_effects = []

        async def reserve_then_raise(*_args, **_kwargs):
            side_effects.append("provider allocation started")
            raise RuntimeError("post-allocation SSH setup failed")

        self._setup_registry(reserve_then_raise)
        srv._last_reservation.update(
            {
                "provider": "aws",
                "reservation_id": "i-previous",
                "provider_metadata": {"instance_ids": ["i-previous"]},
            }
        )

        result = json.loads(
            await srv.reserve_resources(
                provider="aws",
                selection={"instance_type": "m5.xlarge", "count": 1},
                description="test uncertain allocation",
                ticket_id="PERF-TEST",
            )
        )

        assert result["status"] == "unknown"
        assert result["allocation_unknown"] is True
        assert result["retry_blocked"] is True
        assert "may have allocated" in result["message"]
        assert result["provider_metadata"]["instance_ids"] == ["i-previous"]
        assert "retry_suggestion" not in result
        assert side_effects == ["provider allocation started"]
        assert srv._reservation_failures == 1
        assert srv._reservation_uncertain is True
        assert srv._resources_allocated is True

        listing = json.loads(await srv.list_resource_providers())
        discovery = json.loads(await srv.check_available_resources(provider="aws"))
        retry = json.loads(
            await srv.reserve_resources(
                provider="aws",
                selection={"instance_type": "m5.xlarge", "count": 1},
                description="must not duplicate",
                ticket_id="PERF-TEST",
            )
        )
        metadata = json.loads(await srv.get_accumulated_metadata())

        assert listing["allocation_unknown"] is True
        assert discovery["allocation_unknown"] is True
        assert retry["allocation_unknown"] is True
        assert retry["retry_blocked"] is True
        assert side_effects == ["provider allocation started"]
        assert metadata["allocation_unknown"] is True
        assert metadata["instance_ids"] == ["i-previous"]
