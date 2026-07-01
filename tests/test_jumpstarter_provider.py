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

        mock_exporter2 = MagicMock()
        mock_exporter2.name = "device-02"
        mock_exporter2.labels = {"target": "other"}
        mock_exporter2.online = True

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
