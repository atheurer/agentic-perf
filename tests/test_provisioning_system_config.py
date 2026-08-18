"""Tests for post-flash system_config directives in provisioning agent."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.provisioning.agent import ProvisioningAgent


class TestProvisioningSystemConfig:
    """Tests for system_config post-flash provisioning configuration."""

    @pytest.fixture
    def agent(self) -> ProvisioningAgent:
        agent = ProvisioningAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
        )
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        return agent

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_apply_system_config_success(self, mock_create, agent):
        """Test happy path with write_file and run_command."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"stdout_msg", b"stderr_msg")
        mock_create.return_value = mock_proc

        config_ops = [
            {
                "action": "write_file",
                "path": "/etc/foo.conf",
                "content": "some content\n",
            },
            {
                "action": "run_command",
                "command": "systemctl daemon-reload",
                "timeout": 15,
            },
        ]
        cf = {
            "ssh_user": "testuser",
            "ssh_password": "testpassword",
        }

        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["192.168.1.10"],
            config_ops=config_ops,
            cf=cf,
        )

        # Verify ssh runs were called (mkdir, write_file, run_command)
        assert mock_create.call_count == 3

        # Verify status reports saved on ticket
        agent._update_fields.assert_called_once_with(
            "T123",
            {
                "system_config_applied": [
                    "write_file: /etc/foo.conf",
                    "run_command: systemctl daemon-reload -> stdout_msg",
                ],
                "system_config_errors": [],
            },
        )
        assert agent._add_comment.call_count == 1

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_apply_system_config_failures(self, mock_create, agent):
        """Test failing commands are logged as errors instead of crashing."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"permission denied")
        mock_create.return_value = mock_proc

        config_ops = [
            {
                "action": "run_command",
                "command": "systemctl restart missing.service",
            },
            {
                "action": "unknown_action",
            },
        ]

        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["192.168.1.10"],
            config_ops=config_ops,
            cf={},
        )

        agent._update_fields.assert_called_once_with(
            "T123",
            {
                "system_config_applied": [],
                "system_config_errors": [
                    "Op 0: run_command 'systemctl restart missing.service' failed (exit 1): permission denied",
                    "Op 1: unknown action 'unknown_action'",
                ],
            },
        )

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_apply_system_config_jumpstarter_host_resolution(
        self, mock_create, agent
    ):
        """Test resolving hosts starting with 'jumpstarter:'."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        mock_create.return_value = mock_proc

        config_ops = [
            {
                "action": "run_command",
                "command": "hostname",
            }
        ]
        cf = {
            "assigned_hardware_ips": {
                "controller": "10.0.0.5",
            }
        }

        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["jumpstarter:node-1"],
            config_ops=config_ops,
            cf=cf,
        )

        # Check that ssh was called with resolved host
        mock_create.assert_any_call(
            "sshpass",
            "-p",
            "password",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "root@10.0.0.5",
            "hostname",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_apply_system_config_invalid_types(self, agent):
        """Test that malformed datatypes for config_ops are handled gracefully."""
        # config_ops is not a list
        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["192.168.1.10"],
            config_ops="not a list",
            cf={},
        )
        agent._add_comment.assert_any_call(
            "T123",
            "**System Configuration Error:** system_config must be a list, got `str`",
        )

        # list contains elements that are not dicts
        agent._add_comment.reset_mock()
        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["192.168.1.10"],
            config_ops=["not a dict"],
            cf={},
        )
        agent._update_fields.assert_any_call(
            "T123",
            {
                "system_config_applied": [],
                "system_config_errors": [
                    "Op 0: expected dictionary configuration, got str"
                ],
            },
        )

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_apply_system_config_timeout_kill(self, mock_create, agent):
        """Test subprocess is terminated/killed on timeout to prevent connections/process leaks."""
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError
        mock_create.return_value = mock_proc

        config_ops = [
            {
                "action": "run_command",
                "command": "sleep 100",
                "timeout": 1,
            }
        ]

        await agent._apply_system_config(
            ticket_id="T123",
            hosts=["192.168.1.10"],
            config_ops=config_ops,
            cf={},
        )

        # Verify kill was called
        mock_proc.kill.assert_called_once()
        # Verify wait was called to reap/cleanup the process
        mock_proc.wait.assert_called_once()

        # Verify timeout error was logged
        agent._update_fields.assert_called_once_with(
            "T123",
            {
                "system_config_applied": [],
                "system_config_errors": ["Op 0: run_command timed out after 1s"],
            },
        )
