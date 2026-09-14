"""Tests for deterministic Arcaflow deployer config injection."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.benchmark.agent import (
    _build_deployer_config,
    _connection_name,
    _install_arcaflow_hook,
)


def _ticket(
    controller_ip: str = "10.26.28.69",
    ssh_user: str = "root",
    ssh_key: str = "~/.ssh/id_ed25519",
    workflow_source: str = "https://example.com/repo.git",
    ticket_id: str = "PERF-TEST",
) -> dict:
    return {
        "id": ticket_id,
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


class TestConnectionName:
    def test_per_ticket(self):
        assert _connection_name("PERF-ABC") == "arcaflow-perf-abc"

    def test_different_tickets(self):
        assert _connection_name("PERF-A") != _connection_name("PERF-B")


class TestBuildDeployerConfig:
    def test_builds_podman_config(self):
        cfg = _build_deployer_config(_ticket())
        assert cfg is not None
        image = cfg["deployers"]["image"]
        assert image["deployer_name"] == "podman"
        conn = image["podman"]["connectionName"]
        assert conn == _connection_name("PERF-TEST")
        assert image["podman"]["path"] == "/usr/bin/podman"

    def test_returns_none_without_controller_ip(self):
        t = _ticket(controller_ip="")
        assert _build_deployer_config(t) is None

    def test_returns_none_without_ips(self):
        t = {"custom_fields": {}}
        assert _build_deployer_config(t) is None

    def test_per_ticket_connection_name(self):
        t1 = _ticket(ticket_id="PERF-AAA")
        t2 = _ticket(ticket_id="PERF-BBB")
        c1 = _build_deployer_config(t1)
        c2 = _build_deployer_config(t2)
        assert c1 is not None and c2 is not None
        name1 = c1["deployers"]["image"]["podman"]["connectionName"]
        name2 = c2["deployers"]["image"]["podman"]["connectionName"]
        assert name1 != name2


class TestInstallArcaflowHook:
    def test_skips_non_arcaflow_ticket(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        t = {"custom_fields": {"directives": {}}}
        _install_arcaflow_hook(mcp, t)
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
        result = await mcp.pre_call_hook("workflow_load", {"source": {}})
        assert result is None

    @pytest.mark.asyncio
    async def test_hook_injects_deployer_config(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        _install_arcaflow_hook(mcp, _ticket())

        args = {"source": {}, "deployer_config": {"wrong": True}}
        result = await mcp.pre_call_hook("workflow_execute", args)
        assert result is None
        cfg = args["deployer_config"]
        assert cfg["deployers"]["image"]["deployer_name"] == "podman"
        assert cfg["deployers"]["image"]["podman"][
            "connectionName"
        ] == _connection_name("PERF-TEST")

    @pytest.mark.asyncio
    async def test_hook_chains_existing_hook(self):
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
    async def test_hook_returns_none_without_controller(self):
        mcp = AsyncMock()
        mcp.pre_call_hook = None
        t = _ticket(controller_ip="")
        _install_arcaflow_hook(mcp, t)

        args = {"source": {}, "deployer_config": {}}
        result = await mcp.pre_call_hook("workflow_execute", args)
        # No deployer config to inject, returns None (passes through).
        assert result is None
        # deployer_config unchanged.
        assert args["deployer_config"] == {}


class TestSetupArcaflowEnv:
    @pytest.mark.asyncio
    async def test_returns_none_for_non_arcaflow(self):
        from agents.benchmark.agent import _setup_arcaflow_env

        t = {"id": "PERF-X", "custom_fields": {"directives": {}}}
        result = await _setup_arcaflow_env(t)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_controller(self):
        from agents.benchmark.agent import _setup_arcaflow_env

        t = _ticket(controller_ip="")
        result = await _setup_arcaflow_env(t)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_env_with_xdg_keys(self):
        """Verify env dict structure when podman succeeds."""
        from unittest.mock import patch

        from agents.benchmark.agent import _setup_arcaflow_env

        async def _mock_subprocess(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_mock_subprocess,
        ):
            result = await _setup_arcaflow_env(_ticket())
            assert result is not None
            assert "XDG_CONFIG_HOME" in result
            assert "XDG_RUNTIME_DIR" in result
            # Per-ticket dir
            assert "PERF-TEST" in result["XDG_CONFIG_HOME"]
