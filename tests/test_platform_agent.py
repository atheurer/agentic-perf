"""Tests for the platform agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.platform.agent import PlatformAgent


class TestPlatformAgent:
    """Test platform agent setup and routing."""

    def _make_agent(self):
        return PlatformAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
        )

    def test_system_prompt(self):
        agent = self._make_agent()
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "provision_platform" in prompt
        assert "submit_platform_result" in prompt

    def test_build_messages_jumpstarter(self):
        agent = self._make_agent()
        ticket = {
            "id": "T-1",
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {
                    "exporter_name": "rcar-s4-05",
                    "lease_id": "perf-123",
                },
                "jumpstarter_flash": {
                    "flash_command": "j storage flash img.xz",
                },
            },
        }
        msgs = agent._build_messages(ticket)
        assert len(msgs) == 1
        assert "jumpstarter" in msgs[0]["content"]
        assert "rcar-s4-05" in msgs[0]["content"]
        assert "provision_platform" in msgs[0]["content"]

    def test_build_messages_ready_host(self):
        agent = self._make_agent()
        ticket = {
            "id": "T-1",
            "custom_fields": {
                "resource_provider": "aws",
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.2"],
                },
            },
        }
        msgs = agent._build_messages(ticket)
        assert "already provisioned" in msgs[0]["content"]

    def test_build_messages_flash_error(self):
        agent = self._make_agent()
        ticket = {
            "id": "T-1",
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {},
                "jumpstarter_flash": {
                    "error": "No images found",
                },
            },
        }
        msgs = agent._build_messages(ticket)
        assert "Image Resolution Error" in msgs[0]["content"]
        assert "platform_ready=false" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_handle_completion_success(self):
        agent = self._make_agent()
        response = AsyncMock()
        response.text = ""
        response.tool_calls = [
            MagicMock(
                name="submit_platform_result",
                input={
                    "platform_ready": True,
                    "hosts_provisioned": ["10.0.0.1"],
                    "ssh_user": "root",
                    "board_name": "rcar-05",
                },
            )
        ]

        with (
            patch.object(
                agent, "_update_fields", new_callable=AsyncMock
            ) as mock_fields,
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                return_value=False,
            ),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
        ):
            await agent._handle_completion("T-1", response)
            fields = mock_fields.call_args[0][1]
            assert fields["platform_ready"] is True
            assert fields["hosts_provisioned"] == ["10.0.0.1"]
            agent._transition_ticket.assert_called_once()
            assert agent._transition_ticket.call_args[0][1] == "awaiting_provision"

    @pytest.mark.asyncio
    async def test_handle_completion_failure(self):
        agent = self._make_agent()
        response = AsyncMock()
        response.text = ""
        response.tool_calls = [
            MagicMock(
                name="submit_platform_result",
                input={
                    "platform_ready": False,
                    "diagnostics": "Flash failed",
                },
            )
        ]

        with (
            patch.object(agent, "_update_fields", new_callable=AsyncMock),
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={"custom_fields": {}},
            ),
        ):
            await agent._handle_completion("T-1", response)
            assert (
                agent._transition_ticket.call_args[0][1] == "awaiting_customer_guidance"
            )

    async def test_handle_completion_failure_fleet(self):
        """Fleet failure routes to coordinating_fleet, not HITL."""
        agent = self._make_agent()
        response = AsyncMock()
        response.text = ""
        response.tool_calls = [
            MagicMock(
                name="submit_platform_result",
                input={
                    "platform_ready": False,
                    "diagnostics": "Flash failed",
                    "board_name": "board-03",
                },
            )
        ]

        with (
            patch.object(agent, "_update_fields", new_callable=AsyncMock),
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "fleet_investigation": {
                            "enabled": True,
                            "tested_hosts": [],
                        },
                    },
                },
            ),
        ):
            await agent._handle_completion("T-1", response)
            assert agent._transition_ticket.call_args[0][1] == "coordinating_fleet"


class TestProvisionJumpstarter:
    """Test the deterministic provisioning function."""

    @pytest.mark.asyncio
    async def test_provision_result_defaults(self):
        """ProvisionResult has sensible defaults."""
        from providers.resource.jumpstarter_provision import ProvisionResult

        r = ProvisionResult()
        assert not r.success
        assert r.ip == ""
        assert r.ssh_user == "root"
        assert r.diagnostics == []

    @pytest.mark.asyncio
    async def test_provision_result_success(self):
        """ProvisionResult with success data."""
        from providers.resource.jumpstarter_provision import ProvisionResult

        r = ProvisionResult(
            success=True,
            ip="10.0.0.1",
            board_name="rcar-s4-05",
            flash_duration_s=120.5,
        )
        assert r.success
        assert r.ip == "10.0.0.1"
        assert r.flash_duration_s == 120.5


class TestProvisionJumpstarterSDK:
    """Test provision_jumpstarter with mocked SDK."""

    @pytest.mark.asyncio
    async def test_flash_success_full_sequence(self):
        """Happy path through the SDK."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        # Mock the client with driver methods
        client = MagicMock()
        client.storage.flash = MagicMock()
        client.power.on = MagicMock()
        client.power.cycle = MagicMock()
        client.tcp.address = MagicMock(return_value="10.0.0.1:22")

        ssh_result = MagicMock()
        ssh_result.return_code = 0
        ssh_result.stdout = "SSH_OK"
        ssh_result.stderr = ""
        client.ssh.run = MagicMock(return_value=ssh_result)

        result = ProvisionResult(board_name="test-board")
        diag = []

        mock_socket = MagicMock()
        with (
            patch(
                "providers.resource.jumpstarter_provision.asyncio.sleep",
                return_value=None,
            ),
            patch.dict(
                "sys.modules",
                {
                    "jumpstarter_driver_ssh": MagicMock(),
                    "jumpstarter_driver_ssh.client": MagicMock(
                        SSHCommandRunOptions=MagicMock(),
                    ),
                },
            ),
            patch(
                "socket.create_connection",
                return_value=mock_socket,
            ),
        ):
            r = await _run_provision_steps(
                client,
                "https://image.xz",
                "ssh-rsa AAAA",
                result,
                diag,
            )

        assert r.success
        assert r.ip == "10.0.0.1"
        assert any("SSH port 22 reachable" in d for d in r.diagnostics)
        client.storage.flash.assert_called_once_with("https://image.xz")
        # Called twice: pre-flash power cycle + Step 2 power on
        assert client.power.on.call_count == 2
        assert client.ssh.run.call_count == 2  # inject + verify

    @pytest.mark.asyncio
    async def test_flash_failure_retries(self):
        """Flash fails twice — returns failure."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        client = MagicMock()
        client.storage.flash = MagicMock(side_effect=RuntimeError("U-Boot timeout"))

        result = ProvisionResult(board_name="test-board")
        diag = []

        r = await _run_provision_steps(
            client,
            "https://image.xz",
            "",
            result,
            diag,
        )

        assert not r.success
        assert client.storage.flash.call_count == 2  # initial + retry
        assert any("U-Boot" in d for d in r.diagnostics)

    @pytest.mark.asyncio
    async def test_ip_discovery_failure_retries(self):
        """TCP address fails, power cycles, retries."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        client = MagicMock()
        client.storage.flash = MagicMock()
        client.power.on = MagicMock()
        client.power.cycle = MagicMock()
        client.tcp.address = MagicMock(side_effect=RuntimeError("no address"))

        result = ProvisionResult(board_name="test-board")
        diag = []

        with patch(
            "providers.resource.jumpstarter_provision.asyncio.sleep",
            return_value=None,
        ):
            r = await _run_provision_steps(
                client,
                "https://image.xz",
                "",
                result,
                diag,
            )

        assert not r.success
        assert client.tcp.address.call_count >= 2
        client.power.cycle.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_ip_rejected(self):
        """Non-IP from tcp.address is rejected."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        client = MagicMock()
        client.storage.flash = MagicMock()
        client.power.on = MagicMock()
        client.tcp.address = MagicMock(return_value="board-name:22")

        result = ProvisionResult(board_name="test-board")
        diag = []

        with patch(
            "providers.resource.jumpstarter_provision.asyncio.sleep",
            return_value=None,
        ):
            r = await _run_provision_steps(
                client,
                "https://image.xz",
                "",
                result,
                diag,
            )

        assert not r.success
        assert any("Invalid IP" in d for d in r.diagnostics)

    @pytest.mark.asyncio
    async def test_ssh_injection_failure(self):
        """SSH key injection fails."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        client = MagicMock()
        client.storage.flash = MagicMock()
        client.power.on = MagicMock()
        client.tcp.address = MagicMock(return_value="10.0.0.1:22")

        ssh_fail = MagicMock()
        ssh_fail.return_code = 255
        ssh_fail.stderr = "Connection refused"
        client.ssh.run = MagicMock(return_value=ssh_fail)

        result = ProvisionResult(board_name="test-board")
        diag = []

        with (
            patch(
                "providers.resource.jumpstarter_provision.asyncio.sleep",
                return_value=None,
            ),
            patch.dict(
                "sys.modules",
                {
                    "jumpstarter_driver_ssh": MagicMock(),
                    "jumpstarter_driver_ssh.client": MagicMock(
                        SSHCommandRunOptions=MagicMock(),
                    ),
                },
            ),
            patch(
                "socket.create_connection",
                return_value=MagicMock(),
            ),
        ):
            r = await _run_provision_steps(
                client,
                "https://image.xz",
                "ssh-rsa AAAA",
                result,
                diag,
            )

        assert not r.success
        assert any("SSH key injection failed" in d for d in r.diagnostics)


class TestSSHValidation:
    """Test post-flash SSH connectivity validation."""

    @pytest.mark.asyncio
    async def test_ssh_unreachable_fails_provision(self):
        """Board with unreachable SSH should not be declared ready."""
        from unittest.mock import MagicMock

        from providers.resource.jumpstarter_provision import (
            ProvisionResult,
            _run_provision_steps,
        )

        client = MagicMock()
        client.storage.flash = MagicMock()
        client.power.on = MagicMock()
        client.tcp.address = MagicMock(return_value="10.99.99.99:22")

        result = ProvisionResult(board_name="bad-board")
        diag = []

        with (
            patch(
                "providers.resource.jumpstarter_provision.asyncio.sleep",
                return_value=None,
            ),
            patch(
                "socket.create_connection",
                side_effect=OSError("Connection timed out"),
            ),
        ):
            r = await _run_provision_steps(
                client,
                "https://image.xz",
                "",
                result,
                diag,
            )

        assert not r.success
        assert r.ip == "10.99.99.99"
        assert any("unreachable" in d for d in r.diagnostics)
