"""Tests for the resource server post-allocation guardrail (#1128).

After reserve_resources succeeds, list_resource_providers and
check_available_resources must return an error directing the LLM
to call submit_resource_result instead of looping.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

_REAL_PERSIST_UNKNOWN_MARKER = None
_REAL_PERSIST_KNOWN_OUTCOME = None


def _mock_audited_state_store(monkeypatch):
    import httpx

    import providers.execution as execution
    import state_store.auth as auth
    from providers.execution import AuditedAsyncHTTPClient as RealAuditedAsyncHTTPClient

    requests = []
    events = []

    async def emit(event):
        events.append(event)

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})

    def client_factory(**kwargs):
        transport = httpx.MockTransport(handle)
        client = httpx.AsyncClient(
            transport=transport,
            base_url=kwargs.get("base_url"),
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout", 10.0),
        )
        return RealAuditedAsyncHTTPClient(client=client, emit=emit)

    monkeypatch.setattr(execution, "AuditedAsyncHTTPClient", client_factory)
    monkeypatch.setattr(auth, "read_token_from_file", lambda: "test-token")
    return requests, events


# ---------------------------------------------------------------------------
# Helpers to reset module-level state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_server_state(monkeypatch):
    """Reset the resource server's module-level globals before each test."""
    global _REAL_PERSIST_UNKNOWN_MARKER, _REAL_PERSIST_KNOWN_OUTCOME

    import agents.resource.server as srv

    if _REAL_PERSIST_UNKNOWN_MARKER is None:
        _REAL_PERSIST_UNKNOWN_MARKER = srv._persist_unknown_reservation_marker
    if _REAL_PERSIST_KNOWN_OUTCOME is None:
        _REAL_PERSIST_KNOWN_OUTCOME = srv._persist_known_reservation_outcome

    async def persist_unknown_in_memory(ticket_id, *, provider=None, result=None):
        fields = srv._ticket.setdefault("custom_fields", {})
        fields["resource_reservation_outcome_unknown"] = True
        if provider:
            fields["resource_provider"] = provider
        result = result or {}
        metadata = result.get("provider_metadata")
        if isinstance(metadata, dict) and metadata:
            fields["resource_provider_metadata"] = metadata
        reservation_id = (
            result.get("reservation_id")
            or result.get("lease_id")
            or result.get("assignment_id")
        )
        if reservation_id:
            fields["resource_reservation_id"] = str(reservation_id)

    async def persist_known_in_memory(ticket_id, provider, result):
        fields = srv._ticket.setdefault("custom_fields", {})
        fields["resource_reservation_outcome_unknown"] = False
        fields["resource_provider"] = provider
        metadata = result.get("provider_metadata")
        if isinstance(metadata, dict) and metadata:
            fields["resource_provider_metadata"] = metadata
        reservation_id = (
            result.get("reservation_id")
            or result.get("lease_id")
            or result.get("assignment_id")
            or result.get("resource_reservation_id")
        )
        if reservation_id:
            fields["resource_reservation_id"] = str(reservation_id)

    monkeypatch.setattr(
        srv, "_persist_unknown_reservation_marker", persist_unknown_in_memory
    )
    monkeypatch.setattr(
        srv, "_persist_known_reservation_outcome", persist_known_in_memory
    )

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
async def test_fresh_server_reads_unknown_marker_before_discovery_or_reserve(
    monkeypatch,
):
    import agents.resource.server as srv
    import paths
    import providers.resource.registry as registry_module

    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {"resource_reservation_outcome_unknown": True},
    }
    monkeypatch.setattr(
        srv, "build_ssh_from_ticket", AsyncMock(return_value=(None, ticket))
    )
    monkeypatch.setattr(srv, "build_secrets_provider", lambda: object())
    monkeypatch.setattr(paths, "get_instance_name", lambda: "test")

    provider = MagicMock()
    provider.reserve = AsyncMock()
    configured_registry = MagicMock()
    configured_registry.list_configured_providers = AsyncMock(return_value=["aws"])
    configured_registry.get_provider = AsyncMock(return_value=provider)
    monkeypatch.setattr(
        registry_module,
        "ResourceProviderRegistry",
        lambda *_args, **_kwargs: configured_registry,
    )

    results = []
    operations = (
        srv.list_resource_providers,
        lambda: srv.check_available_resources(provider="aws"),
        lambda: srv.reserve_resources(
            provider="aws",
            selection={"instance_type": "m5.xlarge", "count": 1},
            description="must stay blocked",
            ticket_id="PERF-TEST",
        ),
    )
    for operation in operations:
        # Each operation gets a fresh process view so its own _ensure_init()
        # must read and enforce the ticket marker before touching providers.
        srv._initialized = False
        srv._registry = None
        srv._reservation_uncertain = False
        srv._resources_allocated = False
        results.append(json.loads(await operation()))

    for result in results:
        assert result["allocation_unknown"] is True
        assert result["retry_blocked"] is True
    configured_registry.list_configured_providers.assert_not_awaited()
    configured_registry.get_provider.assert_not_awaited()
    provider.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_reservation_marker_uses_audited_state_store_patch(monkeypatch):
    import agents.resource.server as srv
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    requests, events = _mock_audited_state_store(monkeypatch)
    monkeypatch.setattr(
        srv, "_persist_unknown_reservation_marker", _REAL_PERSIST_UNKNOWN_MARKER
    )
    srv._ticket = {"id": "PERF-TEST", "custom_fields": {}}
    trace_token = bind_trace_context(
        new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
    )
    try:
        await srv._persist_unknown_reservation_marker("PERF-TEST")
    finally:
        reset_trace_context(trace_token)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/api/v1/tickets/PERF-TEST/fields"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["x-agentic-perf-ticket-id"] == "PERF-TEST"
    assert request.headers["traceparent"]
    assert json.loads(request.content)["fields"] == {
        "resource_reservation_outcome_unknown": True
    }
    assert srv._ticket["custom_fields"]["resource_reservation_outcome_unknown"] is True
    assert events[-1].lifecycle.state.value == "completed"


@pytest.mark.asyncio
async def test_known_outcome_clears_marker_with_verified_id_and_metadata(monkeypatch):
    import agents.resource.server as srv
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    requests, _events = _mock_audited_state_store(monkeypatch)
    monkeypatch.setattr(
        srv, "_persist_known_reservation_outcome", _REAL_PERSIST_KNOWN_OUTCOME
    )
    srv._ticket = {
        "id": "PERF-TEST",
        "custom_fields": {"resource_reservation_outcome_unknown": True},
    }
    result = {
        "status": "success",
        "reservation_id": "i-verified",
        "provider_metadata": {
            "instance_ids": ["i-verified"],
            "region": "us-east-1",
        },
    }
    trace_token = bind_trace_context(
        new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
    )
    try:
        await srv._persist_known_reservation_outcome("PERF-TEST", "aws", result)
    finally:
        reset_trace_context(trace_token)

    assert len(requests) == 1
    fields = json.loads(requests[0].content)["fields"]
    assert fields == {
        "resource_reservation_outcome_unknown": False,
        "resource_provider": "aws",
        "resource_provider_metadata": {
            "instance_ids": ["i-verified"],
            "region": "us-east-1",
        },
        "resource_reservation_id": "i-verified",
    }
    assert srv._ticket["custom_fields"] == fields


@pytest.mark.asyncio
async def test_cancelled_reservation_persists_marker_before_reraising(monkeypatch):
    import agents.resource.server as srv
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    requests, _events = _mock_audited_state_store(monkeypatch)
    monkeypatch.setattr(
        srv, "_persist_unknown_reservation_marker", _REAL_PERSIST_UNKNOWN_MARKER
    )
    side_effects = []

    async def reserve_then_cancel(*_args, **_kwargs):
        assert len(requests) == 1
        assert (
            json.loads(requests[0].content)["fields"][
                "resource_reservation_outcome_unknown"
            ]
            is True
        )
        side_effects.append("allocation started")
        raise asyncio.CancelledError

    self = TestReservationFailureTracking()
    self._setup_registry(reserve_then_cancel)
    srv._initialized = True
    srv._ticket = {"id": "PERF-TEST", "custom_fields": {}}
    trace_token = bind_trace_context(
        new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await srv.reserve_resources(
                provider="aws",
                selection={"instance_type": "m5.xlarge", "count": 1},
                description="cancel after allocation starts",
                ticket_id="PERF-TEST",
            )

        retry = json.loads(
            await srv.reserve_resources(
                provider="aws",
                selection={"instance_type": "m5.xlarge", "count": 1},
                description="must not retry",
                ticket_id="PERF-TEST",
            )
        )
    finally:
        reset_trace_context(trace_token)

    assert side_effects == ["allocation started"]
    assert srv._reservation_uncertain is True
    assert srv._reservation_failures == 1
    assert srv._ticket["custom_fields"]["resource_reservation_outcome_unknown"] is True
    assert len(requests) == 2
    assert all(
        json.loads(request.content)["fields"]["resource_reservation_outcome_unknown"]
        is True
        for request in requests
    )
    assert retry["allocation_unknown"] is True
    assert retry["retry_blocked"] is True


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
    async def test_provider_exception_marks_outcome_unknown_and_blocks_retry(
        self, monkeypatch
    ):
        """An exception after side effects must not trigger duplicate allocation."""
        import agents.resource.server as srv
        from providers.tracing import (
            bind_trace_context,
            new_trace_context,
            reset_trace_context,
        )

        requests, _events = _mock_audited_state_store(monkeypatch)
        monkeypatch.setattr(
            srv, "_persist_unknown_reservation_marker", _REAL_PERSIST_UNKNOWN_MARKER
        )
        side_effects = []

        async def reserve_then_raise(*_args, **_kwargs):
            assert len(requests) == 1
            assert (
                json.loads(requests[0].content)["fields"][
                    "resource_reservation_outcome_unknown"
                ]
                is True
            )
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

        trace_token = bind_trace_context(
            new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
        )
        try:
            result = json.loads(
                await srv.reserve_resources(
                    provider="aws",
                    selection={"instance_type": "m5.xlarge", "count": 1},
                    description="test uncertain allocation",
                    ticket_id="PERF-TEST",
                )
            )
        finally:
            reset_trace_context(trace_token)

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
        assert (
            srv._ticket["custom_fields"]["resource_reservation_outcome_unknown"] is True
        )
        assert result["marker_persisted"] is True
        assert len(requests) == 2
        assert all(
            json.loads(request.content)["fields"][
                "resource_reservation_outcome_unknown"
            ]
            is True
            for request in requests
        )

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

    @pytest.mark.asyncio
    async def test_failed_response_with_allocation_id_keeps_marker_and_identity(
        self, monkeypatch
    ):
        import agents.resource.server as srv
        from providers.tracing import (
            bind_trace_context,
            new_trace_context,
            reset_trace_context,
        )

        requests, _events = _mock_audited_state_store(monkeypatch)
        monkeypatch.setattr(
            srv, "_persist_unknown_reservation_marker", _REAL_PERSIST_UNKNOWN_MARKER
        )

        async def failed_after_allocate(*_args, **_kwargs):
            return {
                "status": "failed",
                "error": "SSH setup failed after launch",
                "reservation_id": "i-partial",
                "provider_metadata": {"instance_ids": ["i-partial"]},
            }

        self._setup_registry(failed_after_allocate)
        trace_token = bind_trace_context(
            new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
        )
        try:
            result = json.loads(
                await srv.reserve_resources(
                    provider="aws",
                    selection={"instance_type": "m5.xlarge", "count": 1},
                    description="failed after allocation",
                    ticket_id="PERF-TEST",
                )
            )
        finally:
            reset_trace_context(trace_token)

        assert result["allocation_unknown"] is True
        assert result["retry_blocked"] is True
        assert srv._reservation_uncertain is True
        assert (
            srv._ticket["custom_fields"]["resource_reservation_outcome_unknown"] is True
        )
        assert srv._ticket["custom_fields"]["resource_reservation_id"] == "i-partial"
        assert srv._ticket["custom_fields"]["resource_provider_metadata"] == {
            "instance_ids": ["i-partial"]
        }
        assert len(requests) == 2
        assert json.loads(requests[0].content)["fields"] == {
            "resource_reservation_outcome_unknown": True,
            "resource_provider": "aws",
        }
        assert json.loads(requests[1].content)["fields"] == {
            "resource_reservation_outcome_unknown": True,
            "resource_provider": "aws",
            "resource_provider_metadata": {"instance_ids": ["i-partial"]},
            "resource_reservation_id": "i-partial",
        }

    @pytest.mark.asyncio
    async def test_multi_call_aws_success_persists_cumulative_identity_atomically(
        self, monkeypatch
    ):
        import agents.resource.server as srv
        from providers.tracing import (
            bind_trace_context,
            new_trace_context,
            reset_trace_context,
        )

        requests, _events = _mock_audited_state_store(monkeypatch)
        monkeypatch.setattr(
            srv, "_persist_unknown_reservation_marker", _REAL_PERSIST_UNKNOWN_MARKER
        )
        monkeypatch.setattr(
            srv, "_persist_known_reservation_outcome", _REAL_PERSIST_KNOWN_OUTCOME
        )
        responses = iter(
            [
                {
                    "status": "success",
                    "reservation_id": "i-first",
                    "instance_ids": ["i-first"],
                    "provider_metadata": {
                        "instance_ids": ["i-first"],
                        "public_ips": ["198.51.100.1"],
                    },
                },
                {
                    "status": "success",
                    "reservation_id": "i-second",
                    "instance_ids": ["i-second"],
                    "provider_metadata": {
                        "instance_ids": ["i-second"],
                        "public_ips": ["198.51.100.2"],
                    },
                },
            ]
        )
        self._setup_registry(lambda *_args, **_kwargs: next(responses))
        trace_token = bind_trace_context(
            new_trace_context(ticket_id="PERF-TEST", agent_id="resource-agent")
        )
        try:
            for _ in range(2):
                result = json.loads(
                    await srv.reserve_resources(
                        provider="aws",
                        selection={"instance_type": "m5.xlarge", "count": 1},
                        description="multi-call allocation",
                        ticket_id="PERF-TEST",
                    )
                )
        finally:
            reset_trace_context(trace_token)

        assert result["reservation_id"] == "i-first,i-second"
        assert result["provider_metadata"]["instance_ids"] == [
            "i-first",
            "i-second",
        ]
        assert result["provider_metadata"]["public_ips"] == [
            "198.51.100.1",
            "198.51.100.2",
        ]
        assert len(requests) == 4
        final_fields = json.loads(requests[-1].content)["fields"]
        assert final_fields == {
            "resource_reservation_outcome_unknown": False,
            "resource_provider": "aws",
            "resource_provider_metadata": {
                "instance_ids": ["i-first", "i-second"],
                "public_ips": ["198.51.100.1", "198.51.100.2"],
            },
            "resource_reservation_id": "i-first,i-second",
        }
        assert srv._ticket["custom_fields"] == final_fields

    @pytest.mark.asyncio
    async def test_success_without_reservation_identity_is_marked_unknown(
        self, monkeypatch
    ):
        """A success-shaped response without cleanup identity must fail closed."""
        import agents.resource.server as srv

        async def success_without_identity(*_args, **_kwargs):
            return {"status": "success", "hosts": [], "provider_metadata": {}}

        self._setup_registry(success_without_identity)
        persist = AsyncMock()
        monkeypatch.setattr(srv, "_persist_unknown_reservation_marker", persist)

        result = json.loads(
            await srv.reserve_resources(
                provider="aws",
                selection={"instance_type": "m5.xlarge", "count": 1},
                description="provider omitted its reservation identity",
                ticket_id="PERF-TEST",
            )
        )

        assert result["status"] == "unknown"
        assert result["allocation_unknown"] is True
        assert result["retry_blocked"] is True
        assert "no verifiable reservation ID" in result["error"]
        assert persist.await_count == 2
        assert all(call.args == ("PERF-TEST",) for call in persist.await_args_list)
        assert persist.await_args_list[0].kwargs == {"provider": "aws"}
        assert persist.await_args_list[1].kwargs["provider"] == "aws"
        assert persist.await_args_list[1].kwargs["result"]["status"] == "unknown"
        assert srv._reservation_uncertain is True
