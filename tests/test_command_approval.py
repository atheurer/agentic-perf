"""Tests for the runtime command approval flow.

Covers: binary extraction, per-ticket approval lookup, approval request
polling, execute_command integration with approval, and CLI approve/deny.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.infra.server import _extract_binary


class TestExtractBinary:
    def test_simple_command(self):
        assert _extract_binary("ls /tmp") == "ls"

    def test_absolute_path(self):
        assert _extract_binary("/usr/bin/python3 script.py") == "python3"

    def test_env_prefix(self):
        assert _extract_binary("FOO=bar python3 script.py") == "python3"

    def test_empty_command(self):
        assert _extract_binary("") == ""


class TestGetTicketApprovals:
    @pytest.mark.asyncio
    async def test_returns_approvals(self):
        from agents.infra import server

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "custom_fields": {
                "command_approvals": ["python3", "dnf"],
            },
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        old_url = server._state_store_url
        old_tid = server._ticket_id
        try:
            server._state_store_url = "http://localhost:8090"
            server._ticket_id = "PERF-TEST"
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await server._get_ticket_approvals()
                assert result == ["python3", "dnf"]
        finally:
            server._state_store_url = old_url
            server._ticket_id = old_tid

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_ticket(self):
        from agents.infra import server

        old_tid = server._ticket_id
        try:
            server._ticket_id = None
            result = await server._get_ticket_approvals()
            assert result == []
        finally:
            server._ticket_id = old_tid


class TestRequestApproval:
    @pytest.mark.asyncio
    async def test_approved_once(self):
        from agents.infra import server

        call_count = 0
        captured_approval_id = None

        mock_resp_patch = MagicMock()
        mock_resp_patch.raise_for_status = MagicMock()

        def patch_side_effect(*args, **kwargs):
            nonlocal captured_approval_id
            body = kwargs.get("json", {})
            pa = body.get("fields", {}).get("pending_approval", {})
            captured_approval_id = pa.get("id")
            return mock_resp_patch

        mock_resp_get = MagicMock()
        mock_resp_get.raise_for_status = MagicMock()

        def get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mock_resp_get.json.return_value = {
                    "custom_fields": {
                        "pending_approval": {
                            "id": captured_approval_id,
                            "status": "approved_once",
                        },
                    },
                }
            else:
                mock_resp_get.json.return_value = {
                    "custom_fields": {
                        "pending_approval": {
                            "id": captured_approval_id,
                            "status": "pending",
                        },
                    },
                }
            return mock_resp_get

        async def async_patch(*args, **kwargs):
            return patch_side_effect(*args, **kwargs)

        async def async_get(*args, **kwargs):
            return get_side_effect(*args, **kwargs)

        mock_client = AsyncMock()
        mock_client.patch = async_patch
        mock_client.get = async_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        old_url = server._state_store_url
        old_tid = server._ticket_id
        old_agent = server._agent_name
        old_interval = server._APPROVAL_POLL_INTERVAL
        try:
            server._state_store_url = "http://localhost:8090"
            server._ticket_id = "PERF-TEST"
            server._agent_name = "test-agent"
            server._APPROVAL_POLL_INTERVAL = 0.01

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await server._request_approval(
                    "python3 script.py", "python3", "10.0.0.1"
                )
                assert result == "approved_once"
        finally:
            server._state_store_url = old_url
            server._ticket_id = old_tid
            server._agent_name = old_agent
            server._APPROVAL_POLL_INTERVAL = old_interval

    @pytest.mark.asyncio
    async def test_denied_when_no_ticket(self):
        from agents.infra import server

        old_tid = server._ticket_id
        try:
            server._ticket_id = None
            result = await server._request_approval(
                "python3 script.py", "python3", "10.0.0.1"
            )
            assert result == "denied"
        finally:
            server._ticket_id = old_tid


class TestExecuteCommandApproval:
    """Test that execute_command routes to approval for allowlist misses."""

    @pytest.mark.asyncio
    async def test_safety_pattern_hard_rejects(self):
        """Global safety patterns never get an approval prompt."""
        from agents.infra import server
        from agents.infra.command_policy import CommandPolicy

        old_policy = server._policy
        old_ssh = server._ssh
        try:
            server._policy = CommandPolicy(
                agent_name="test",
                allowed_binaries={"ls", "cat"},
            )
            server._ssh = MagicMock()

            result_str = await server.execute_command("10.0.0.1", "reboot", timeout=10)
            result = json.loads(result_str)
            assert result["blocked"] is True
            assert "blocked by policy" in result["stderr"].lower()
        finally:
            server._policy = old_policy
            server._ssh = old_ssh

    @pytest.mark.asyncio
    async def test_pre_approved_binary_executes(self):
        """Binary in command_approvals skips the approval prompt."""
        from agents.infra import server
        from agents.infra.command_policy import CommandPolicy
        from providers.ssh import SSHResult

        old_policy = server._policy
        old_ssh = server._ssh
        try:
            server._policy = CommandPolicy(
                agent_name="test",
                allowed_binaries={"ls"},
            )
            mock_ssh = AsyncMock()
            mock_ssh.run.return_value = SSHResult(stdout="ok", stderr="", exit_code=0)
            server._ssh = mock_ssh

            with patch.object(
                server,
                "_get_ticket_approvals",
                return_value=["python3"],
            ):
                result_str = await server.execute_command(
                    "10.0.0.1", "python3 test.py", timeout=10
                )
                result = json.loads(result_str)
                assert result["exit_code"] == 0
                assert result["stdout"] == "ok"
        finally:
            server._policy = old_policy
            server._ssh = old_ssh

    @pytest.mark.asyncio
    async def test_denied_approval_blocks(self):
        """When user denies, command is blocked."""
        from agents.infra import server
        from agents.infra.command_policy import CommandPolicy

        old_policy = server._policy
        old_ssh = server._ssh
        try:
            server._policy = CommandPolicy(
                agent_name="test",
                allowed_binaries={"ls"},
            )
            server._ssh = MagicMock()

            with (
                patch.object(
                    server,
                    "_get_ticket_approvals",
                    return_value=[],
                ),
                patch.object(
                    server,
                    "_request_approval",
                    return_value="denied",
                ),
            ):
                result_str = await server.execute_command(
                    "10.0.0.1", "python3 test.py", timeout=10
                )
                result = json.loads(result_str)
                assert result["blocked"] is True
                assert "denied by user" in result["stderr"].lower()
        finally:
            server._policy = old_policy
            server._ssh = old_ssh

    @pytest.mark.asyncio
    async def test_allowed_command_executes_normally(self):
        """Commands in the allowlist execute without approval."""
        from agents.infra import server
        from agents.infra.command_policy import CommandPolicy
        from providers.ssh import SSHResult

        old_policy = server._policy
        old_ssh = server._ssh
        try:
            server._policy = CommandPolicy(
                agent_name="test",
                allowed_binaries={"ls"},
            )
            mock_ssh = AsyncMock()
            mock_ssh.run.return_value = SSHResult(
                stdout="file1\nfile2", stderr="", exit_code=0
            )
            server._ssh = mock_ssh

            result_str = await server.execute_command("10.0.0.1", "ls /tmp", timeout=10)
            result = json.loads(result_str)
            assert result["exit_code"] == 0
        finally:
            server._policy = old_policy
            server._ssh = old_ssh


class TestCLIApproval:
    """Test approve/deny CLI commands via the state store."""

    def test_approve_once(self):
        from cli import cmd_approve

        mock_client = MagicMock()

        ticket_data = {
            "custom_fields": {
                "pending_approval": {
                    "id": "appr-test",
                    "agent": "benchmark-agent",
                    "command": "python3 parse.py",
                    "binary": "python3",
                    "host": "10.0.0.1",
                    "status": "pending",
                },
            },
        }

        mock_get = MagicMock()
        mock_get.json.return_value = ticket_data
        mock_get.raise_for_status = MagicMock()

        mock_patch = MagicMock()
        mock_patch.raise_for_status = MagicMock()

        mock_client.get.return_value = mock_get
        mock_client.patch.return_value = mock_patch

        args = MagicMock()
        args.ticket_id = "PERF-TEST"
        args.ticket = False

        with patch("cli.get_client", return_value=(mock_client, "")):
            cmd_approve(args)

        call_args = mock_client.patch.call_args
        fields = call_args.kwargs.get("json", call_args[1].get("json", {}))
        pa = fields["fields"]["pending_approval"]
        assert pa["status"] == "approved_once"
        assert "command_approvals" not in fields["fields"]

    def test_approve_ticket(self):
        from cli import cmd_approve

        mock_client = MagicMock()

        ticket_data = {
            "custom_fields": {
                "pending_approval": {
                    "id": "appr-test",
                    "agent": "benchmark-agent",
                    "command": "python3 parse.py",
                    "binary": "python3",
                    "host": "10.0.0.1",
                    "status": "pending",
                },
            },
        }

        mock_get = MagicMock()
        mock_get.json.return_value = ticket_data
        mock_get.raise_for_status = MagicMock()

        mock_patch = MagicMock()
        mock_patch.raise_for_status = MagicMock()

        mock_client.get.return_value = mock_get
        mock_client.patch.return_value = mock_patch

        args = MagicMock()
        args.ticket_id = "PERF-TEST"
        args.ticket = True

        with patch("cli.get_client", return_value=(mock_client, "")):
            cmd_approve(args)

        call_args = mock_client.patch.call_args
        fields = call_args.kwargs.get("json", call_args[1].get("json", {}))
        pa = fields["fields"]["pending_approval"]
        assert pa["status"] == "approved_ticket"
        assert "python3" in fields["fields"]["command_approvals"]

    def test_deny(self):
        from cli import cmd_deny

        mock_client = MagicMock()

        ticket_data = {
            "custom_fields": {
                "pending_approval": {
                    "id": "appr-test",
                    "agent": "benchmark-agent",
                    "command": "python3 parse.py",
                    "binary": "python3",
                    "host": "10.0.0.1",
                    "status": "pending",
                },
            },
        }

        mock_get = MagicMock()
        mock_get.json.return_value = ticket_data
        mock_get.raise_for_status = MagicMock()

        mock_patch = MagicMock()
        mock_patch.raise_for_status = MagicMock()

        mock_client.get.return_value = mock_get
        mock_client.patch.return_value = mock_patch

        args = MagicMock()
        args.ticket_id = "PERF-TEST"

        with patch("cli.get_client", return_value=(mock_client, "")):
            cmd_deny(args)

        call_args = mock_client.patch.call_args
        fields = call_args.kwargs.get("json", call_args[1].get("json", {}))
        pa = fields["fields"]["pending_approval"]
        assert pa["status"] == "denied"

    def test_no_pending_approval(self):
        from cli import cmd_approve

        mock_client = MagicMock()
        mock_get = MagicMock()
        mock_get.json.return_value = {"custom_fields": {}}
        mock_get.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_get

        args = MagicMock()
        args.ticket_id = "PERF-TEST"
        args.ticket = False

        with patch("cli.get_client", return_value=(mock_client, "")):
            cmd_approve(args)

        mock_client.patch.assert_not_called()
