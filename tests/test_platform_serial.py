"""Tests for serial capture behavior during Jumpstarter provisioning.

Serial capture during flash/provisioning is intentionally skipped
because the serial pipe subprocess conflicts with the flash tool's
pexpect serial connection (causes EOF).  Boot-phase serial capture
is the benchmark agent's responsibility.

These tests verify:
- Serial subprocess is NOT started during provisioning (even when requested)
- Provisioning still succeeds without serial capture
- Cancellation propagates correctly
- ProvisionResult dataclass has serial_log_path field
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest


@dataclass
class FakeProvisionResult:
    success: bool = False
    ip: str = ""
    board_name: str = "test-board"
    ssh_user: str = "root"
    ssh_key_path: str = ""
    diagnostics: list[str] = field(default_factory=list)
    flash_duration_s: float = 10.0
    boot_duration_s: float = 30.0
    serial_log_path: str = ""
    infrastructure_error: bool = False


class TestProvisionSerialCapture:
    """Test serial capture is skipped during Jumpstarter provisioning."""

    @pytest.mark.asyncio
    async def test_serial_not_started_even_when_enabled(self, tmp_path):
        """Serial subprocess must NOT start during provisioning.

        The flash tool requires exclusive serial port access through
        the gRPC tunnel.  A concurrent 'j serial pipe' subprocess
        causes pexpect EOF on the U-Boot prompt.
        """
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        with (
            patch(
                "providers.resource.jumpstarter_provision._provision_sync",
                return_value=FakeProvisionResult(success=True, ip="10.0.0.1"),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            result = await provision_jumpstarter(
                lease_name="test-lease-123",
                flash_url="http://example.com/image.raw.xz",
                ssh_public_key="ssh-rsa AAAA",
                serial_capture=True,
                artifact_dir=str(tmp_path),
            )

            # Serial subprocess must not be spawned during provisioning
            mock_exec.assert_not_called()
            assert result.serial_log_path == ""

    @pytest.mark.asyncio
    async def test_serial_not_started_when_disabled(self):
        """No serial subprocess when serial_capture=False."""
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        with (
            patch(
                "providers.resource.jumpstarter_provision._provision_sync",
                return_value=FakeProvisionResult(success=True, ip="10.0.0.1"),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            result = await provision_jumpstarter(
                lease_name="test-lease-456",
                flash_url="http://example.com/image.raw.xz",
                ssh_public_key="ssh-rsa AAAA",
                serial_capture=False,
            )

            mock_exec.assert_not_called()
            assert result.serial_log_path == ""

    @pytest.mark.asyncio
    async def test_provisioning_cancellation_propagates(self):
        """Cancellation must not be converted to a failed provision result."""
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        with patch(
            "providers.resource.jumpstarter_provision._provision_sync",
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await provision_jumpstarter(
                    lease_name="test-lease-cancelled",
                    flash_url="http://example.com/image.raw.xz",
                    ssh_public_key="ssh-rsa AAAA",
                )

    def test_provision_result_has_serial_field(self):
        """ProvisionResult dataclass includes serial_log_path."""
        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
        )

        r = ProvisionResult()
        assert r.serial_log_path == ""
        r.serial_log_path = "/tmp/serial.log"
        assert r.serial_log_path == "/tmp/serial.log"


class TestPlatformServerSerialPassthrough:
    """Test that platform server passes serial params correctly."""

    @pytest.fixture
    def cf_with_serial(self, tmp_path):
        return {
            "resource_provider": "jumpstarter",
            "resource_provider_metadata": {
                "lease_id": "test-lease-123",
                "exporter_name": "board-1",
                "selector": "board-type=test",
                "duration_seconds": 28_800,
            },
            "directives": {"serial_capture": True},
            "jumpstarter_flash": {
                "flash_targets": [
                    {"url": "http://example.com/image.raw.xz"},
                ],
                "ssh_public_key": "ssh-rsa AAAA...",
                "ssh_key_path": str(tmp_path / "key"),
            },
        }

    @pytest.mark.asyncio
    async def test_server_passes_serial_params(self, cf_with_serial, tmp_path):
        """Platform server passes serial_capture and artifact_dir."""
        from agents.platform import server

        fake_result = FakeProvisionResult(success=True, ip="10.0.0.1")

        with (
            patch(
                "providers.resource.jumpstarter_provision.provision_jumpstarter",
                new_callable=AsyncMock,
                return_value=fake_result,
            ) as mock_provision,
            patch(
                "paths.create_artifact_dir",
                return_value=tmp_path,
            ),
            patch.object(
                server,
                "_ticket",
                {"id": "PERF-TEST123", "custom_fields": cf_with_serial},
            ),
            patch.object(server, "_ensure_init", new_callable=AsyncMock),
        ):
            await server.provision_platform()

            mock_provision.assert_called_once()
            kwargs = mock_provision.call_args
            # serial_capture is passed through (provisioning skips it internally)
            assert kwargs.kwargs.get("serial_capture") is True or (
                len(kwargs.args) > 7 and kwargs.args[7] is True
            )
            assert kwargs.kwargs["lease_duration_seconds"] == 28_800

    @pytest.mark.asyncio
    async def test_server_uses_artifact_dir_when_serial_capture_is_disabled(
        self, tmp_path
    ):
        """Older tickets fall back to the provisioning default duration."""
        from agents.platform import server

        cf = {
            "resource_provider": "jumpstarter",
            "resource_provider_metadata": {
                "lease_id": "test-lease-123",
                "exporter_name": "board-1",
            },
            "jumpstarter_flash": {
                "flash_targets": [{"url": "http://example.com/image.raw.xz"}],
            },
        }
        fake_result = FakeProvisionResult(success=True, ip="10.0.0.1")

        with (
            patch(
                "providers.resource.jumpstarter_provision.provision_jumpstarter",
                new_callable=AsyncMock,
                return_value=fake_result,
            ) as mock_provision,
            patch("paths.create_artifact_dir", return_value=tmp_path) as create_dir,
            patch.object(
                server,
                "_ticket",
                {"id": "PERF-TEST123", "custom_fields": cf},
            ),
            patch.object(server, "_ensure_init", new_callable=AsyncMock),
        ):
            await server.provision_platform()

        assert "lease_duration_seconds" not in mock_provision.call_args.kwargs
        assert mock_provision.call_args.kwargs["serial_capture"] is False
        assert mock_provision.call_args.kwargs["artifact_dir"] == str(tmp_path)
        create_dir.assert_called_once_with("PERF-TEST123", "platform-provision")

    @pytest.mark.asyncio
    async def test_no_ticket_without_serial_capture_keeps_artifact_dir_empty(self):
        """No-ticket calls do not allocate an unused temporary artifact directory."""
        from agents.platform import server

        cf = {
            "resource_provider": "jumpstarter",
            "resource_provider_metadata": {"lease_id": "scratch-lease"},
            "directives": {"serial_capture": False},
            "jumpstarter_flash": {
                "flash_targets": [{"url": "http://example.com/image.raw.xz"}],
            },
        }
        fake_result = FakeProvisionResult(success=True, ip="10.0.0.1")

        with (
            patch(
                "providers.resource.jumpstarter_provision.provision_jumpstarter",
                new_callable=AsyncMock,
                return_value=fake_result,
            ) as mock_provision,
            patch("paths.create_artifact_dir") as create_dir,
            patch.object(server, "_ticket", {"id": "", "custom_fields": cf}),
            patch.object(server, "_ensure_init", new_callable=AsyncMock),
        ):
            await server.provision_platform()

        create_dir.assert_not_called()
        assert mock_provision.call_args.kwargs["ticket_id"] == ""
        assert mock_provision.call_args.kwargs["artifact_dir"] == ""
        assert mock_provision.call_args.kwargs["serial_capture"] is False
