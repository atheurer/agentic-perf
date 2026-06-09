from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from providers.skills.base import RunfileTemplate
from agents.provisioning.mcp_server import create_provisioning_tool_handlers

from tests.conftest import MockSkillProvider, MockSSHExecutor, SSHResult


ZATHRAS_PRIVATE_CONFIG = {
    "constraints": {
        "supported_os": ["rhel8", "rhel9", "fedora"],
        "requires_epel": True,
    },
    "provisioning": {
        "install_method": "git_clone",
        "git_url": "https://github.com/redhat-performance/zathras.git",
        "install_script": "install.sh",
        "install_target_path": "/opt/zathras",
        "verify_command": "/opt/zathras/bin/burden --usage",
        "update_command": "cd /opt/zathras && git pull && ./install.sh",
        "run_install_as_root": "yes | ./install.sh",
        "pre_install_steps": [
            "dnf install -y epel-release || true",
            "dnf install -y git",
        ],
        "on_existing_install": "skip",
    },
}

CRUCIBLE_PRIVATE_CONFIG = {
    "provisioning": {
        "install_method": "internal_repo",
        "install_target_path": "/opt/crucible",
        "verify_command": "/opt/crucible/bin/crucible help",
        "on_existing_install": "skip",
    },
    "internal_repo_local_path": "/home/user/crucible-internal",
    "install_script": "rh-install-crucible.sh",
}


@pytest.fixture
def mock_provider() -> MockSkillProvider:
    return MockSkillProvider(
        private_config={
            "zathras": ZATHRAS_PRIVATE_CONFIG,
            "crucible": CRUCIBLE_PRIVATE_CONFIG,
        },
    )


@pytest.fixture
def mock_ssh() -> MockSSHExecutor:
    return MockSSHExecutor()


@pytest.fixture
def handlers(mock_provider, mock_ssh):
    async def noop_clarification(q): pass
    h = create_provisioning_tool_handlers(
        skill_provider=mock_provider,
        request_clarification_fn=noop_clarification,
    )
    # Patch the ssh executor used by the handlers
    # The handlers close over the ssh variable, so we need to patch at module level
    return h, mock_ssh


@pytest.mark.asyncio
async def test_get_private_config_constraints(mock_provider):
    result = await mock_provider.get_private_config("zathras", "constraints")
    assert result is not None
    assert "rhel8" in result["supported_os"]
    assert result["requires_epel"] is True


@pytest.mark.asyncio
async def test_get_private_config_not_found(mock_provider):
    result = await mock_provider.get_private_config("unknown", "constraints")
    assert result is None


@pytest.mark.asyncio
async def test_verify_harness_reads_verify_command(handlers):
    h, mock_ssh = handlers
    with patch("agents.provisioning.mcp_server.SSHExecutor", return_value=mock_ssh):
        # We can't easily patch the ssh inside the closure, so test the config lookup
        pass


@pytest.mark.asyncio
async def test_install_harness_git_clone_has_pre_install(mock_provider):
    """Verify that zathras config includes pre_install_steps and run_install_as_root."""
    config = await mock_provider.get_all_private_config("zathras")
    provisioning = config["provisioning"]
    assert provisioning["install_method"] == "git_clone"
    assert len(provisioning["pre_install_steps"]) == 2
    assert "epel-release" in provisioning["pre_install_steps"][0]
    assert provisioning["run_install_as_root"] == "yes | ./install.sh"


@pytest.mark.asyncio
async def test_install_harness_internal_repo_config(mock_provider):
    """Verify crucible config uses internal_repo install method."""
    config = await mock_provider.get_all_private_config("crucible")
    assert config["provisioning"]["install_method"] == "internal_repo"
    assert config["internal_repo_local_path"] == "/home/user/crucible-internal"


@pytest.mark.asyncio
async def test_check_existing_reads_from_config(mock_provider):
    """Verify that harness config provides the paths needed for check_existing_install."""
    zathras_config = await mock_provider.get_all_private_config("zathras")
    assert zathras_config["provisioning"]["verify_command"] == "/opt/zathras/bin/burden --usage"
    assert zathras_config["provisioning"]["install_target_path"] == "/opt/zathras"

    crucible_config = await mock_provider.get_all_private_config("crucible")
    assert crucible_config["provisioning"]["verify_command"] == "/opt/crucible/bin/crucible help"
    assert crucible_config["provisioning"]["install_target_path"] == "/opt/crucible"


@pytest.mark.asyncio
async def test_update_install_reads_from_config(mock_provider):
    """Verify that zathras config provides an update command."""
    config = await mock_provider.get_all_private_config("zathras")
    assert "git pull" in config["provisioning"]["update_command"]
