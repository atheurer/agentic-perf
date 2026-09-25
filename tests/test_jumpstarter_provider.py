"""Tests for the Jumpstarter resource provider.

Tests with mocked Jumpstarter API — no controller required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.resource.jumpstarter import JumpstarterResourceProvider

# --- Construction ---


class TestConstruction:
    def test_provider_name(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
        )
        assert provider.provider_name == "jumpstarter"

    def test_defaults(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            namespace="lab",
            default_selector="target=myboard",
            default_lease_duration=3600,
        )
        assert provider._namespace == "lab"
        assert provider._default_selector == "target=myboard"
        assert provider._default_duration == 3600

    def test_default_lease_duration_matches_resource_agent_guidance(self):
        provider = JumpstarterResourceProvider(client_name="test")

        assert provider._default_duration == 14_400

    @pytest.mark.asyncio
    async def test_from_secrets(self, tmp_path: Path):
        secrets_data = {
            "client_name": "test-ci",
            "namespace": "test-lab",
            "default_selector": "target=test",
            "default_lease_duration_seconds": 1800,
            "ssh_user": "testuser",
        }

        class MockSecrets:
            async def get_secret(self, path):
                if "jumpstarter" in path:
                    return json.dumps(secrets_data)
                return None

        provider = await JumpstarterResourceProvider.from_secrets(MockSecrets())
        assert provider._client_name == "test-ci"
        assert provider._namespace == "test-lab"
        assert provider._default_selector == "target=test"
        assert provider._ssh_user == "testuser"


# --- Check available ---


class TestCheckAvailable:
    @pytest.mark.asyncio
    async def test_returns_matching_devices(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="target=myboard",
        )

        # Mock the service
        mock_exporter = MagicMock()
        mock_exporter.name = "device-01"
        mock_exporter.labels = {
            "target": "myboard",
            "board-type": "qc8775",
            "pool": "open",
        }
        mock_exporter.online = True
        mock_exporter.status = "AVAILABLE"

        mock_exporter2 = MagicMock()
        mock_exporter2.name = "device-02"
        mock_exporter2.labels = {"target": "other", "pool": "open"}
        mock_exporter2.online = True
        mock_exporter2.status = "AVAILABLE"

        mock_result = MagicMock()
        mock_result.exporters = [mock_exporter, mock_exporter2]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        result = await provider.check_available({})
        assert result["available"] is True
        assert result["matching_devices"] == 1
        assert result["devices"][0]["name"] == "device-01"

    @pytest.mark.asyncio
    async def test_custom_selector(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="target=default",
        )

        mock_exporter = MagicMock()
        mock_exporter.name = "custom-01"
        mock_exporter.labels = {"target": "custom", "pool": "open"}
        mock_exporter.online = True
        mock_exporter.status = "AVAILABLE"

        mock_result = MagicMock()
        mock_result.exporters = [mock_exporter]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        result = await provider.check_available(
            {"jumpstarter_selector": "target=custom"}
        )
        assert result["matching_devices"] == 1

    @pytest.mark.asyncio
    async def test_not_available_when_insufficient(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="target=rare",
        )

        mock_result = MagicMock()
        mock_result.exporters = []

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        result = await provider.check_available({"count": 2})
        assert result["available"] is False
        assert result["matching_devices"] == 0

    @pytest.mark.asyncio
    async def test_all_excluded_requires_every_selector_match_to_be_tested(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="board-type=qc8775",
        )
        exporter = MagicMock()
        exporter.name = "board-01"
        exporter.labels = {"board-type": "qc8775", "pool": "open"}
        exporter.online = True
        exporter.status = "AVAILABLE"
        result = MagicMock()
        result.exporters = [exporter]
        service = AsyncMock()
        service.ListExporters = AsyncMock(return_value=result)
        provider._service = service

        availability = await provider.check_available({"exclude_hosts": ["board-01"]})

        assert availability["all_excluded"] is True
        assert availability["matching_devices"] == 1
        assert availability["excluded_hosts"] == ["board-01"]

    @pytest.mark.asyncio
    async def test_unavailable_untested_selector_match_is_not_exhausted(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="board-type=qc8775",
        )
        exporter = MagicMock()
        exporter.name = "board-01"
        exporter.labels = {"board-type": "qc8775", "pool": "open"}
        exporter.online = True
        exporter.status = "LEASED"
        result = MagicMock()
        result.exporters = [exporter]
        service = AsyncMock()
        service.ListExporters = AsyncMock(return_value=result)
        provider._service = service

        availability = await provider.check_available(
            {"exclude_hosts": ["unrelated-tested-board"]}
        )

        assert availability["available"] is False
        assert availability["matching_devices"] == 0
        assert "all_excluded" not in availability


# --- Name selector ---


class TestNameSelector:
    """Tests for name= selector targeting a specific device."""

    def _make_provider(self, exporters):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="board-type=qc8775",
        )
        mock_result = MagicMock()
        mock_result.exporters = exporters
        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc
        return provider

    def _make_exporter(
        self,
        name,
        board_type="qc8775",
        online=True,
        status="AVAILABLE",
        enabled=True,
        pool="open",
    ):
        e = MagicMock()
        e.name = name
        e.labels = {
            "board-type": board_type,
            "enabled": str(enabled).lower(),
            "pool": pool,
        }
        e.online = online
        e.status = status
        return e

    @pytest.mark.asyncio
    async def test_available_by_name(self):
        exp = self._make_exporter("board-01")
        provider = self._make_provider([exp])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=board-01"}
        )
        assert result["available"] is True
        assert result["exporter_name"] == "board-01"
        assert result["matching_devices"] == 1

    @pytest.mark.asyncio
    async def test_not_found(self):
        exp = self._make_exporter("board-01")
        provider = self._make_provider([exp])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=no-such-board"}
        )
        assert result["available"] is False
        assert "No device named" in result["error"]

    @pytest.mark.asyncio
    async def test_leased(self):
        exp = self._make_exporter("board-01", status="LEASED")
        provider = self._make_provider([exp])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=board-01"}
        )
        assert result["available"] is False
        assert "LEASED" in result["unavailable_reason"]

    @pytest.mark.asyncio
    async def test_offline(self):
        exp = self._make_exporter("board-01", online=False)
        provider = self._make_provider([exp])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=board-01"}
        )
        assert result["available"] is False
        assert "offline" in result["unavailable_reason"]

    @pytest.mark.asyncio
    async def test_disabled(self):
        exp = self._make_exporter("board-01", enabled=False)
        provider = self._make_provider([exp])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=board-01"}
        )
        assert result["available"] is False
        assert "disabled" in result["unavailable_reason"]

    @pytest.mark.asyncio
    async def test_suggests_alternatives(self):
        busy = self._make_exporter("board-01", status="LEASED")
        free = self._make_exporter("board-02")
        provider = self._make_provider([busy, free])
        result = await provider.check_available(
            {"jumpstarter_selector": "name=board-01"}
        )
        assert result["available"] is False
        assert "board-02" in result["alternatives"]

    @pytest.mark.asyncio
    async def test_reserve_resolves_name_selector(self):
        exp = self._make_exporter("board-01", board_type="qc8775")
        provider = self._make_provider([exp])
        # Mock CreateLease — use spec to avoid spurious attributes
        mock_lease = MagicMock(spec=["name"])
        mock_lease.name = "test-lease"
        provider._service.CreateLease = AsyncMock(
            return_value=mock_lease,
        )
        await provider.reserve(
            {"jumpstarter_selector": "name=board-01"},
            ticket_id="PERF-TEST",
        )
        # Should resolve to board-type selector + exporter_name
        call_args = provider._service.CreateLease.call_args
        assert call_args is not None
        assert "board-type=qc8775" in call_args.kwargs["selector"]
        assert call_args.kwargs["exporter_name"] == "board-01"

    @pytest.mark.asyncio
    async def test_reserve_name_not_found(self):
        exp = self._make_exporter("board-01")
        provider = self._make_provider([exp])
        result = await provider.reserve(
            {"jumpstarter_selector": "name=no-such-board"},
            ticket_id="PERF-TEST",
        )
        assert result["status"] == "rejected"
        assert "not found" in result["error"]


# --- Reserve ---


class TestReserve:
    @pytest.mark.asyncio
    async def test_creates_lease(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
            default_selector="target=myboard",
            default_lease_duration=3600,
            ssh_user="root",
        )

        mock_lease = MagicMock()
        mock_lease.name = "lease-abc123"
        mock_lease.exporter_name = "device-01"

        mock_exporter = MagicMock()
        mock_exporter.labels = {"target": "ride4_sa8775p_sx_r3"}

        mock_svc = AsyncMock()
        mock_svc.CreateLease = AsyncMock(return_value=mock_lease)
        mock_svc.GetExporter = AsyncMock(return_value=mock_exporter)
        provider._service = mock_svc

        result = await provider.reserve({}, description="test", ticket_id="PERF-TEST")
        assert result["provider"] == "jumpstarter"
        assert result["lease_id"] == "lease-abc123"
        assert result["board_target"] == "ride4_sa8775p_sx_r3"
        assert result["status"] == "active"
        assert result["ssh_user"] == "root"
        assert result["provider_metadata"] == {
            "lease_id": "lease-abc123",
            "exporter_name": "device-01",
            "board_target": "ride4_sa8775p_sx_r3",
            "selector": "target=myboard,enabled=true,pool=open",
            "duration_seconds": 3600,
        }

        # Verify CreateLease was called correctly
        mock_svc.CreateLease.assert_called_once()
        call_kwargs = mock_svc.CreateLease.call_args.kwargs
        assert call_kwargs["selector"] == "target=myboard,enabled=true,pool=open"
        assert call_kwargs["lease_id"] == "perf-test"


# --- Terminate ---


class TestTerminate:
    @pytest.mark.asyncio
    async def test_deletes_lease(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
        )

        mock_svc = AsyncMock()
        mock_svc.DeleteLease = AsyncMock()
        provider._service = mock_svc

        result = await provider.terminate("lease-abc123")
        assert result["status"] == "terminated"
        mock_svc.DeleteLease.assert_called_once_with(name="lease-abc123")

    @pytest.mark.asyncio
    async def test_handles_delete_error(self):
        provider = JumpstarterResourceProvider(
            client_name="test",
        )

        mock_svc = AsyncMock()
        mock_svc.DeleteLease = AsyncMock(side_effect=Exception("not found"))
        provider._service = mock_svc

        result = await provider.terminate("bad-lease")
        assert result["status"] == "error"
        assert "not found" in result["error"]


# --- Registry ---


class TestRegistry:
    def test_registered(self):
        from providers.resource.registry import (
            PROVIDER_REGISTRY,
        )

        assert "jumpstarter" in PROVIDER_REGISTRY
        assert (
            "JumpstarterResourceProvider" in PROVIDER_REGISTRY["jumpstarter"]["class"]
        )
        assert PROVIDER_REGISTRY["jumpstarter"]["secret"] == "jumpstarter/config.json"


class TestListTargets:
    @pytest.mark.asyncio
    async def test_returns_unique_targets(self):
        provider = JumpstarterResourceProvider(client_name="test")

        mock_exporters = []
        for i, (name, target) in enumerate(
            [
                ("sa8775p-01", "ride4_sa8775p_sx_r3"),
                ("sa8775p-02", "ride4_sa8775p_sx_r3"),
                ("rcar-s4-01", "rcar_s4"),
                ("s32g-01", "s32g_vnp_rdb3"),
            ]
        ):
            e = MagicMock()
            e.name = name
            e.labels = {"target": target, "board-type": f"type-{i}", "pool": "open"}
            e.online = True
            e.status = "AVAILABLE"
            mock_exporters.append(e)

        mock_result = MagicMock()
        mock_result.exporters = mock_exporters

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        targets = await provider.list_targets()
        assert len(targets) == 4

        # Sorted by count descending
        assert targets[0]["target"] == "type-0"
        assert targets[0]["count"] == 1
        assert targets[0]["selector"] == "board-type=type-0"

        assert targets[1]["target"] == "type-1"
        assert targets[1]["count"] == 1
        assert targets[1]["selector"] == "board-type=type-1"

        assert targets[2]["target"] == "type-2"
        assert targets[2]["count"] == 1

    @pytest.mark.asyncio
    async def test_excludes_offline(self):
        provider = JumpstarterResourceProvider(client_name="test")

        online = MagicMock()
        online.name = "dev-01"
        online.labels = {"target": "myboard", "pool": "open"}
        online.online = True
        online.status = "AVAILABLE"

        offline = MagicMock()
        offline.name = "dev-02"
        offline.labels = {"target": "myboard", "pool": "open"}
        offline.online = False
        offline.status = "OFFLINE"

        mock_result = MagicMock()
        mock_result.exporters = [online, offline]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        targets = await provider.list_targets()
        assert len(targets) == 1
        assert targets[0]["count"] == 1

    @pytest.mark.asyncio
    async def test_excludes_disabled(self):
        provider = JumpstarterResourceProvider(client_name="test")

        enabled = MagicMock()
        enabled.name = "dev-01"
        enabled.labels = {"target": "myboard", "enabled": "true", "pool": "open"}
        enabled.online = True
        enabled.status = "AVAILABLE"

        disabled = MagicMock()
        disabled.name = "dev-02"
        disabled.labels = {"target": "myboard", "enabled": "false", "pool": "open"}
        disabled.online = True
        disabled.status = "AVAILABLE"

        mock_result = MagicMock()
        mock_result.exporters = [enabled, disabled]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        targets = await provider.list_targets()
        assert len(targets) == 1
        assert targets[0]["count"] == 1

    @pytest.mark.asyncio
    async def test_excludes_leased(self):
        provider = JumpstarterResourceProvider(client_name="test")

        available = MagicMock()
        available.name = "dev-01"
        available.labels = {"target": "myboard", "pool": "open"}
        available.online = True
        available.status = "AVAILABLE"

        leased = MagicMock()
        leased.name = "dev-02"
        leased.labels = {"target": "myboard", "pool": "open"}
        leased.online = True
        leased.status = "LEASE_READY"

        mock_result = MagicMock()
        mock_result.exporters = [available, leased]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        targets = await provider.list_targets()
        assert len(targets) == 1
        assert targets[0]["count"] == 1


class TestCheckAvailableRequiresSelector:
    @pytest.mark.asyncio
    async def test_no_selector_returns_error_with_targets(self):
        provider = JumpstarterResourceProvider(client_name="test")

        mock_exporter = MagicMock()
        mock_exporter.name = "device-01"
        mock_exporter.labels = {"target": "myboard", "board-type": "x", "pool": "open"}
        mock_exporter.online = True
        mock_exporter.status = "AVAILABLE"

        mock_result = MagicMock()
        mock_result.exporters = [mock_exporter]

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        result = await provider.check_available({})
        assert result["available"] is False
        assert "error" in result
        assert "jumpstarter_selector" in result["error"]
        assert len(result["available_targets"]) == 1
        assert result["available_targets"][0]["selector"] == "board-type=x"


class TestNamedDeviceEscalation:
    """Verify auto-escalation to HITL for unavailable named devices."""

    @pytest.mark.asyncio
    async def test_regular_availability_query_does_not_create_an_escalation(self):
        """Only an unavailable ``name=`` selector may mutate ticket state."""
        from unittest.mock import patch

        from tests.conftest import make_resource_handlers

        provider = MagicMock()
        provider.provider_name = "jumpstarter"
        provider.check_available = AsyncMock(
            return_value={"available": True, "selector": "board-type=arm"}
        )
        registry = MagicMock()
        registry.get_provider = AsyncMock(return_value=provider)
        handlers = make_resource_handlers(registry=registry)

        with patch(
            "agents.resource.server._auto_escalate_named_device", new=AsyncMock()
        ) as escalate:
            result = await handlers["check_available_resources"](
                provider="jumpstarter",
                requirements={"jumpstarter_selector": "board-type=arm"},
            )

        assert result["available"] is True
        escalate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_escalate_posts_transition(self):
        from unittest.mock import patch

        from agents.resource.server import _auto_escalate_named_device
        from providers.tracing import (
            TraceContext,
            bind_trace_context,
            current_trace_context,
            reset_trace_context,
        )

        result = {
            "available": False,
            "selector": "name=board-01",
            "error": "Device 'board-01' exists but is unavailable (status: LEASED).",
            "alternatives": ["board-02", "board-03"],
        }

        async def immediate_thread_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.dict(
                "os.environ",
                {
                    "TICKET_ID": "PERF-TEST",
                    "STATE_STORE_URL": "http://localhost:8090",
                    "AGENTIC_PERF_API_TOKEN": "test-token",
                },
            ),
            patch("providers.tracing.client.TraceClient") as mock_trace_client_cls,
            patch(
                "agents.resource.server.asyncio.to_thread", new=immediate_thread_call
            ),
        ):
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            observed: dict[str, object] = {}

            class FakeHTTPClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                async def post(self, url, **kwargs):
                    observed["url"] = url
                    observed["body"] = kwargs["json"]
                    observed["trace"] = current_trace_context()
                    return mock_response

            with patch(
                "providers.execution.AuditedAsyncHTTPClient",
                side_effect=lambda **_kwargs: FakeHTTPClient(),
            ):
                registry = mock_trace_client_cls.return_value
                registry.operation_acquire.return_value = {
                    "status": "acquired",
                    "operation": {"fencing_generation": 7},
                }
                trace_token = bind_trace_context(TraceContext(ticket_id="PERF-TEST"))
                try:
                    await _auto_escalate_named_device(result)
                finally:
                    reset_trace_context(trace_token)

            assert "transition" in str(observed["url"])
            body = observed["body"]
            assert body["status"] == "awaiting_customer_guidance"
            assert "board-01" in body["comment"]
            assert "board-02" in body["comment"]
            escalation_trace = observed["trace"]
            assert escalation_trace.idempotency_key == (
                "named-device-escalation:PERF-TEST:name=board-01"
            )
            assert escalation_trace.parent_action_id is not None
            assert [
                call.args[1] for call in registry.operation_transition.call_args_list
            ] == [
                "prepared",
                "side-effect-started",
                "complete",
            ]

    @pytest.mark.asyncio
    async def test_completed_escalation_is_not_reposted_on_replay(self):
        """The operation registry, not the whole read-only tool, owns replay."""
        from unittest.mock import patch

        from agents.resource.server import _auto_escalate_named_device
        from providers.tracing import (
            TraceContext,
            bind_trace_context,
            reset_trace_context,
        )

        async def immediate_thread_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.dict(
                "os.environ",
                {
                    "TICKET_ID": "PERF-TEST",
                    "STATE_STORE_URL": "http://localhost:8090",
                    "AGENTIC_PERF_API_TOKEN": "test-token",
                },
            ),
            patch("providers.execution.AuditedAsyncHTTPClient") as mock_client_cls,
            patch("providers.tracing.client.TraceClient") as mock_trace_client_cls,
            patch(
                "agents.resource.server.asyncio.to_thread", new=immediate_thread_call
            ),
        ):
            registry = mock_trace_client_cls.return_value
            registry.operation_acquire.return_value = {
                "status": "terminal",
                "operation": {"fencing_generation": 7},
            }
            trace_token = bind_trace_context(TraceContext(ticket_id="PERF-TEST"))
            try:
                await _auto_escalate_named_device({"selector": "name=board-01"})
            finally:
                reset_trace_context(trace_token)

        mock_client_cls.assert_not_called()
        registry.operation_transition.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_escalation_becomes_indeterminate(self):
        """Cancellation after the POST starts must require reconciliation."""
        from unittest.mock import patch

        from agents.resource.server import _auto_escalate_named_device
        from providers.tracing import (
            TraceContext,
            bind_trace_context,
            reset_trace_context,
        )

        class CancelledHTTPClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, **_kwargs):
                raise asyncio.CancelledError()

        async def immediate_thread_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.dict(
                "os.environ",
                {
                    "TICKET_ID": "PERF-TEST",
                    "STATE_STORE_URL": "http://localhost:8090",
                    "AGENTIC_PERF_API_TOKEN": "test-token",
                },
            ),
            patch(
                "providers.execution.AuditedAsyncHTTPClient",
                side_effect=lambda **_kwargs: CancelledHTTPClient(),
            ),
            patch(
                "agents.resource.server.asyncio.to_thread", new=immediate_thread_call
            ),
            patch("providers.tracing.client.TraceClient") as mock_trace_client_cls,
        ):
            registry = mock_trace_client_cls.return_value
            registry.operation_acquire.return_value = {
                "status": "acquired",
                "operation": {"fencing_generation": 7},
            }
            trace_token = bind_trace_context(TraceContext(ticket_id="PERF-TEST"))
            try:
                with pytest.raises(asyncio.CancelledError):
                    await _auto_escalate_named_device({"selector": "name=board-01"})
            finally:
                reset_trace_context(trace_token)

        transitions = registry.operation_transition.call_args_list
        assert [call.args[1] for call in transitions] == [
            "prepared",
            "side-effect-started",
            "indeterminate",
        ]
        assert transitions[-1].kwargs["descriptor"] == {
            "outcome": "cancelled_after_transition_start"
        }

    @pytest.mark.asyncio
    async def test_no_escalate_without_ticket_id(self):
        from unittest.mock import patch

        from agents.resource.server import _auto_escalate_named_device

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("providers.execution.AuditedAsyncHTTPClient") as mock_client_cls,
        ):
            await _auto_escalate_named_device({"selector": "name=x"})
            mock_client_cls.assert_not_called()
