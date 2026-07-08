"""Tests for the Jumpstarter resource provider.

Tests with mocked Jumpstarter API — no controller required.
"""

from __future__ import annotations

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
        }
        mock_exporter.online = True
        mock_exporter.status = "AVAILABLE"

        mock_exporter2 = MagicMock()
        mock_exporter2.name = "device-02"
        mock_exporter2.labels = {"target": "other"}
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
        mock_exporter.labels = {"target": "custom"}
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

        mock_svc = AsyncMock()
        mock_svc.CreateLease = AsyncMock(return_value=mock_lease)
        provider._service = mock_svc

        result = await provider.reserve({}, description="test", ticket_id="PERF-TEST")
        assert result["provider"] == "jumpstarter"
        assert result["lease_id"] == "lease-abc123"
        assert result["status"] == "active"
        assert result["ssh_user"] == "root"

        # Verify CreateLease was called correctly
        mock_svc.CreateLease.assert_called_once()
        call_kwargs = mock_svc.CreateLease.call_args.kwargs
        assert call_kwargs["selector"] == "target=myboard"
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
            e.labels = {"target": target, "board-type": f"type-{i}"}
            e.online = True
            e.status = "AVAILABLE"
            mock_exporters.append(e)

        mock_result = MagicMock()
        mock_result.exporters = mock_exporters

        mock_svc = AsyncMock()
        mock_svc.ListExporters = AsyncMock(return_value=mock_result)
        provider._service = mock_svc

        targets = await provider.list_targets()
        assert len(targets) == 3

        # Sorted by count descending
        assert targets[0]["target"] == "ride4_sa8775p_sx_r3"
        assert targets[0]["count"] == 2
        assert targets[0]["selector"] == "target=ride4_sa8775p_sx_r3"

        assert targets[1]["target"] == "rcar_s4"
        assert targets[1]["count"] == 1
        assert targets[1]["selector"] == "target=rcar_s4"

        assert targets[2]["target"] == "s32g_vnp_rdb3"
        assert targets[2]["count"] == 1

    @pytest.mark.asyncio
    async def test_excludes_offline(self):
        provider = JumpstarterResourceProvider(client_name="test")

        online = MagicMock()
        online.name = "dev-01"
        online.labels = {"target": "myboard"}
        online.online = True
        online.status = "AVAILABLE"

        offline = MagicMock()
        offline.name = "dev-02"
        offline.labels = {"target": "myboard"}
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
        enabled.labels = {"target": "myboard", "enabled": "true"}
        enabled.online = True
        enabled.status = "AVAILABLE"

        disabled = MagicMock()
        disabled.name = "dev-02"
        disabled.labels = {"target": "myboard", "enabled": "false"}
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
        available.labels = {"target": "myboard"}
        available.online = True
        available.status = "AVAILABLE"

        leased = MagicMock()
        leased.name = "dev-02"
        leased.labels = {"target": "myboard"}
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
        mock_exporter.labels = {"target": "myboard", "board-type": "x"}
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
        assert "list_jumpstarter_targets" in result["error"]
        assert len(result["available_targets"]) == 1
        assert result["available_targets"][0]["selector"] == "target=myboard"
