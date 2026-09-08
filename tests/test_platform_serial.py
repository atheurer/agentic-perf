"""Tests for serial capture during Jumpstarter provisioning."""

from __future__ import annotations

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


class TestProvisionSerialCapture:
    """Test serial capture in the Jumpstarter provision provider."""

    @pytest.mark.asyncio
    async def test_serial_started_when_enabled(self, tmp_path):
        """Serial subprocess starts when serial_capture=True."""
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.terminate = lambda: None
        mock_proc.wait = AsyncMock()
        mock_proc.kill = lambda: None

        with (
            patch(
                "providers.resource.jumpstarter_provision._provision_sync",
                return_value=FakeProvisionResult(success=True, ip="10.0.0.1"),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ) as mock_exec,
        ):
            result = await provision_jumpstarter(
                lease_name="test-lease-123",
                flash_url="http://example.com/image.raw.xz",
                ssh_public_key="ssh-rsa AAAA",
                serial_capture=True,
                artifact_dir=str(tmp_path),
            )

            # Verify jmp shell was called with serial pipe
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert "jmp" in call_args
            assert "serial" in call_args
            assert "pipe" in call_args
            assert "--lease=test-lease-123" in call_args
            assert result.serial_log_path != ""

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
    async def test_serial_diagnostics_on_failure(self, tmp_path):
        """Serial output included in diagnostics on failure."""
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        serial_content = (
            "U-Boot 2024.01\nStarting kernel...\n"
            "Kernel panic - not syncing: Fatal exception\n"
        )

        def fake_provision(*args, **kwargs):
            # Simulate serial output written during provisioning
            serial_log = tmp_path / "serial-capture.log"
            serial_log.write_text(serial_content)
            return FakeProvisionResult(
                success=False,
                diagnostics=["Flash failed: timeout"],
            )

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.terminate = lambda: None
        mock_proc.wait = AsyncMock()
        mock_proc.kill = lambda: None

        with (
            patch(
                "providers.resource.jumpstarter_provision._provision_sync",
                side_effect=fake_provision,
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
        ):
            result = await provision_jumpstarter(
                lease_name="test-lease-789",
                flash_url="http://example.com/image.raw.xz",
                ssh_public_key="ssh-rsa AAAA",
                serial_capture=True,
                artifact_dir=str(tmp_path),
            )

            assert not result.success
            serial_diag = [d for d in result.diagnostics if "Serial output" in d]
            assert len(serial_diag) == 1
            assert "Kernel panic" in serial_diag[0]

    @pytest.mark.asyncio
    async def test_serial_cleaned_up_on_exception(self, tmp_path):
        """Serial subprocess terminated on provisioning exception."""
        from providers.resource.jumpstarter_provision import (
            provision_jumpstarter,
        )

        mock_proc = AsyncMock()
        mock_proc.pid = 99999
        mock_proc.terminate = lambda: None
        mock_proc.wait = AsyncMock()
        mock_proc.kill = lambda: None

        with (
            patch(
                "providers.resource.jumpstarter_provision._provision_sync",
                side_effect=RuntimeError("unexpected crash"),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
        ):
            result = await provision_jumpstarter(
                lease_name="test-lease-crash",
                flash_url="http://example.com/image.raw.xz",
                ssh_public_key="ssh-rsa AAAA",
                serial_capture=True,
                artifact_dir=str(tmp_path),
            )

            # Should return a failure, not raise
            assert not result.success
            assert any("exception" in d.lower() for d in result.diagnostics)

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
            "ticket_id": "PERF-TEST123",
            "resource_provider": "jumpstarter",
            "resource_provider_metadata": {
                "lease_id": "test-lease-123",
                "exporter_name": "board-1",
                "selector": "board-type=test",
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
        from agents.platform.server import _provision_jumpstarter

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
        ):
            await _provision_jumpstarter(cf_with_serial)

            mock_provision.assert_called_once()
            kwargs = mock_provision.call_args
            # Check serial params were passed
            assert kwargs.kwargs.get("serial_capture") is True or (
                len(kwargs.args) > 7 and kwargs.args[7] is True
            )
