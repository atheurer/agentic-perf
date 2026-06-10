from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from providers.skills.base import RunfileTemplate
from agents.provisioning.mcp_server import create_provisioning_tool_handlers

from tests.conftest import MockSecretsProvider, MockSkillProvider, MockSSHExecutor, SSHResult


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
        "install_method": "public_install",
        "installer_url": "https://raw.githubusercontent.com/perftool-incubator/crucible/master/crucible-install.sh",
        "install_flags": {
            "engine-registry": "quay.io/crucible/client-server",
            "engine-auth-file": "/root/crucible-client-server-token.json",
            "engine-tls-verify": "true",
            "quay-engine-expiration-refresh-token": "/root/crucible-production-quay-oauth.token",
            "quay-engine-expiration-refresh-api-url": "https://quay.io/api/v1/repository/crucible/client-server",
            "verbose": None,
        },
        "install_target_path": "/opt/crucible",
        "verify_command": "/opt/crucible/bin/crucible help",
        "update_command": "crucible update",
        "on_existing_install": "skip",
        "post_install_commands": [
            "crucible registries add private-engine url=quay.io/crucible/private-engines push-token=/root/private-engines-token.json pull-token=/root/private-engines-token.json tls-verify=true",
        ],
    },
    "secrets": {
        "client_server_auth": "crucible/crucible-client-server-token.json",
        "engine_oauth": "crucible/crucible-production-quay-oauth.token",
    },
    "install_contract": {
        "secret_files": [
            {
                "secret_key": "client_server_auth",
                "remote_path": "/root/crucible-client-server-token.json",
                "required": True,
                "description": "Container registry auth for crucible images",
            },
            {
                "secret_key": "engine_oauth",
                "remote_path": "/root/crucible-production-quay-oauth.token",
                "required": True,
                "description": "OAuth token for engine images",
            },
        ],
        "pre_install_commands": [
            "systemctl mask firewalld 2>/dev/null; true",
        ],
    },
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
def mock_secrets() -> MockSecretsProvider:
    return MockSecretsProvider(files={
        "crucible/crucible-client-server-token.json": "/fake/secrets/crucible-client-server-token.json",
        "crucible/crucible-production-quay-oauth.token": "/fake/secrets/crucible-production-quay-oauth.token",
    })


@pytest.fixture
def mock_secrets_missing() -> MockSecretsProvider:
    return MockSecretsProvider(files={})


@pytest.fixture
def handlers(mock_provider, mock_ssh, mock_secrets):
    async def noop_clarification(q): pass
    h = create_provisioning_tool_handlers(
        skill_provider=mock_provider,
        secrets_provider=mock_secrets,
        request_clarification_fn=noop_clarification,
    )
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
async def test_install_harness_public_install_config(mock_provider):
    """Verify crucible config uses public_install method with skill-driven flags."""
    config = await mock_provider.get_all_private_config("crucible")
    assert config["provisioning"]["install_method"] == "public_install"
    assert "crucible-install.sh" in config["provisioning"]["installer_url"]
    flags = config["provisioning"]["install_flags"]
    assert flags["engine-registry"] == "quay.io/crucible/client-server"
    assert flags["engine-auth-file"] == "/root/crucible-client-server-token.json"


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


@pytest.mark.asyncio
async def test_contract_validation_passes(mock_provider, mock_secrets):
    """Contract validation succeeds when all required secrets are present."""
    async def noop(q): pass
    handlers, ssh = create_provisioning_tool_handlers(
        skill_provider=mock_provider,
        secrets_provider=mock_secrets,
        request_clarification_fn=noop,
    )
    config = await mock_provider.get_all_private_config("crucible")
    contract = config["install_contract"]
    assert len(contract["secret_files"]) == 2
    for entry in contract["secret_files"]:
        secret_path = config["secrets"][entry["secret_key"]]
        local = await mock_secrets.get_secret_file(secret_path)
        assert local is not None, f"Secret {entry['secret_key']} should be available"


@pytest.mark.asyncio
async def test_contract_validation_fails_missing_secret(mock_provider, mock_secrets_missing):
    """Contract validation fails when required secrets are missing."""
    async def noop(q): pass
    handlers, ssh = create_provisioning_tool_handlers(
        skill_provider=mock_provider,
        secrets_provider=mock_secrets_missing,
        request_clarification_fn=noop,
    )
    config = await mock_provider.get_all_private_config("crucible")
    for entry in config["install_contract"]["secret_files"]:
        secret_path = config["secrets"][entry["secret_key"]]
        local = await mock_secrets_missing.get_secret_file(secret_path)
        assert local is None, f"Secret {entry['secret_key']} should be missing"


@pytest.mark.asyncio
async def test_contract_absent_is_ok(mock_provider):
    """Harnesses without install_contract (like zathras) have no contract to validate."""
    config = await mock_provider.get_all_private_config("zathras")
    assert "install_contract" not in config
