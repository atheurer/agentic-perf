"""Tests for deterministic Arcaflow deployer config injection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agents.benchmark.agent import (
    _ARCAFLOW_CONNECTION_NAME,
    _build_deployer_config,
    _install_arcaflow_hook,
)


def _ticket(
    controller_ip: str = "10.26.28.69",
    ssh_user: str = "root",
    ssh_key: str = "~/.ssh/id_ed25519",
    workflow_source: str = "https://example.com/repo.git",
) -> dict:
    return {
        "id": "PERF-TEST",
        "custom_fields": {
            "assigned_hardware_ips": {"controller": controller_ip},
            "ssh_user": ssh_user,
            "ssh_key_path": ssh_key,
            "directives": {
                "workflow_source": workflow_source,
                "workflow_name": "workflow-fio",
            },
        },
    }


class TestBuildDeployerConfig:
    def test_builds_podman_config(self):
        cfg = _build_deployer_config(_ticket())
        assert cfg is not None
        image = cfg["deployers"]["image"]
        assert image["deployer_name"] == "podman"
        assert image["podman"]["connectionName"] == _ARCAFLOW_CONNECTION_NAME
        assert image["podman"]["path"] == "/usr/bin/podman"

    def test_returns_none_without_controller_ip(self):
        t = _ticket(controller_ip="")
        assert _build_deployer_config(t) is None

    def test_returns_none_without_ips(self):
        t = {"custom_fields": {}}
        assert _build_deployer_config(t) is None


class TestInstallArcaflowHook:
    def test_skips_non_arcaflow_ticket(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        t = {"custom_fields": {"directives": {}}}
        _install_arcaflow_hook(mcp, t)
        # No hook installed — pre_call_hook stays None.
        assert mcp.pre_call_hook is None

    def test_installs_hook_for_arcaflow_ticket(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())
        assert mcp.pre_call_hook is not None

    @pytest.mark.asyncio
    async def test_hook_ignores_non_execute_calls(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())
        result = await mcp.pre_call_hook(
            "workflow_load", {"source": {}}
        )
        assert result is None

    @pytest.mark.asyncio
    @patch(
        "agents.benchmark.agent._ensure_podman_connection",
        new_callable=AsyncMock,
        return_value=True,
    )
    async def test_hook_injects_deployer_config(self, mock_conn):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())

        args = {"source": {}, "deployer_config": {"wrong": True}}
        result = await mcp.pre_call_hook("workflow_execute", args)
        # Returns None (continue with modified args).
        assert result is None
        # deployer_config replaced.
        cfg = args["deployer_config"]
        assert cfg["deployers"]["image"]["deployer_name"] == "podman"
        assert (
            cfg["deployers"]["image"]["podman"]["connectionName"]
            == _ARCAFLOW_CONNECTION_NAME
        )

    @pytest.mark.asyncio
    @patch(
        "agents.benchmark.agent._ensure_podman_connection",
        new_callable=AsyncMock,
        return_value=False,
    )
    async def test_hook_returns_error_on_connection_failure(
        self, mock_conn,
    ):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())

        args = {"source": {}, "deployer_config": {}}
        result = await mcp.pre_call_hook("workflow_execute", args)
        assert result is not None
        err = json.loads(result)
        assert "error" in err

    @pytest.mark.asyncio
    @patch(
        "agents.benchmark.agent._ensure_podman_connection",
        new_callable=AsyncMock,
        return_value=True,
    )
    async def test_hook_chains_existing_hook(self, mock_conn):
        mcp = AsyncMock()
        existing_called = []

        async def existing_hook(name, arguments):
            existing_called.append(name)
            return None

        mcp.pre_call_hook = existing_hook
        _install_arcaflow_hook(mcp, _ticket())

        args = {"source": {}, "deployer_config": {}}
        await mcp.pre_call_hook("workflow_execute", args)
        assert "workflow_execute" in existing_called

    @pytest.mark.asyncio
    @patch(
        "agents.benchmark.agent._ensure_podman_connection",
        new_callable=AsyncMock,
        return_value=True,
    )
    async def test_connection_setup_called_once(self, mock_conn):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())

        args = {"source": {}, "deployer_config": {}}
        await mcp.pre_call_hook("workflow_execute", args)
        await mcp.pre_call_hook("workflow_execute", args)
        # Connection set up only once.
        assert mock_conn.call_count == 1
