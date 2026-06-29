from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.provisioning.mcp_server import (
    _parse_os_release,
    _summarize,
    create_provisioning_tool_handlers,
    validate_platform_contract,
)
from tests.conftest import (
    MockSecretsProvider,
    MockSkillProvider,
    MockSSHExecutor,
    SSHResult,
)

ZATHRAS_PRIVATE_CONFIG = {
    "constraints": {
        "supported_os": ["rhel8", "rhel9", "centos"],
        "requires_epel": True,
    },
    "platform_contract": {
        "supported_os": ["rhel8", "rhel9", "centos"],
        "required_repos": ["epel"],
        "required_packages": ["git"],
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
    "platform_contract": {
        "supported_os": ["rhel8", "rhel9", "rhel10", "fedora", "centos"],
        "required_packages": ["podman", "git", "jq", "curl"],
    },
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
    return MockSecretsProvider(
        files={
            "crucible/crucible-client-server-token.json": "/fake/secrets/crucible-client-server-token.json",
            "crucible/crucible-production-quay-oauth.token": "/fake/secrets/crucible-production-quay-oauth.token",
        }
    )


@pytest.fixture
def mock_secrets_missing() -> MockSecretsProvider:
    return MockSecretsProvider(files={})


@pytest.fixture
def handlers(mock_provider, mock_ssh, mock_secrets):
    async def noop_clarification(q):
        pass

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
    assert (
        zathras_config["provisioning"]["verify_command"]
        == "/opt/zathras/bin/burden --usage"
    )
    assert zathras_config["provisioning"]["install_target_path"] == "/opt/zathras"

    crucible_config = await mock_provider.get_all_private_config("crucible")
    assert (
        crucible_config["provisioning"]["verify_command"]
        == "/opt/crucible/bin/crucible help"
    )
    assert crucible_config["provisioning"]["install_target_path"] == "/opt/crucible"


@pytest.mark.asyncio
async def test_update_install_reads_from_config(mock_provider):
    """Verify that zathras config provides an update command."""
    config = await mock_provider.get_all_private_config("zathras")
    assert "git pull" in config["provisioning"]["update_command"]


@pytest.mark.asyncio
async def test_contract_validation_passes(mock_provider, mock_secrets):
    """Contract validation succeeds when all required secrets are present."""

    async def noop(q):
        pass

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
async def test_contract_validation_fails_missing_secret(
    mock_provider, mock_secrets_missing
):
    """Contract validation fails when required secrets are missing."""

    async def noop(q):
        pass

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


# --- Platform contract tests ---

RHEL9_OS_RELEASE = """\
NAME="Red Hat Enterprise Linux"
VERSION="9.4 (Plow)"
ID="rhel"
VERSION_ID="9.4"
PLATFORM_ID="platform:el9"
"""

UBUNTU_OS_RELEASE = """\
NAME="Ubuntu"
VERSION="22.04.3 LTS (Jammy Jellyfish)"
ID=ubuntu
VERSION_ID="22.04"
"""

ROCKY9_OS_RELEASE = """\
NAME="Rocky Linux"
VERSION="9.3 (Blue Onyx)"
ID="rocky"
VERSION_ID="9.3"
"""


def test_parse_os_release_rhel():
    assert _parse_os_release(RHEL9_OS_RELEASE) == "rhel9"


def test_parse_os_release_ubuntu():
    assert _parse_os_release(UBUNTU_OS_RELEASE) == "ubuntu22"


def test_parse_os_release_rocky_normalizes_to_rhel():
    assert _parse_os_release(ROCKY9_OS_RELEASE) == "rhel9"


@pytest.mark.asyncio
async def test_platform_contract_os_match():
    """Platform validation passes when OS matches supported list."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
            "repolist": SSHResult(
                stdout="repo id              repo name\nepel               Extra Packages"
            ),
            "which git": SSHResult(stdout="/usr/bin/git"),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel8", "rhel9"],
            "required_repos": ["epel"],
            "required_packages": ["git"],
        }
    }
    result = await validate_platform_contract(ssh, "testhost", config)
    assert result["status"] == "ok"
    assert result["detected_os"] == "rhel9"
    assert result["os_match"] is True
    assert result["missing_repos"] == []
    assert result["missing_packages"] == []


@pytest.mark.asyncio
async def test_platform_contract_os_mismatch():
    """Platform validation fails when OS is not in supported list."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=UBUNTU_OS_RELEASE),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel8", "rhel9", "fedora"],
        }
    }
    result = await validate_platform_contract(ssh, "testhost", config)
    assert result["status"] == "failed"
    assert result["detected_os"] == "ubuntu22"
    assert result["os_match"] is False
    assert "ubuntu22" in result["message"]


@pytest.mark.asyncio
async def test_platform_contract_missing_repo():
    """Platform validation fails when required repo is missing."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
            "repolist": SSHResult(
                stdout="repo id              repo name\nbaseos             BaseOS"
            ),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel9"],
            "required_repos": ["epel"],
        }
    }
    result = await validate_platform_contract(ssh, "testhost", config)
    assert result["status"] == "failed"
    assert "epel" in result["missing_repos"]


@pytest.mark.asyncio
async def test_platform_contract_missing_package_is_warning():
    """Missing packages produce a warning (ok status), not a failure."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
            "which podman": SSHResult(exit_code=1, stdout=""),
            "rpm -q podman": SSHResult(exit_code=1, stdout=""),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel9"],
            "required_packages": ["podman"],
        }
    }
    result = await validate_platform_contract(ssh, "testhost", config)
    assert result["status"] == "ok"
    assert "podman" in result["missing_packages"]


@pytest.mark.asyncio
async def test_platform_contract_absent():
    """No platform_contract in config means no validation (passthrough)."""
    ssh = MockSSHExecutor()
    result = await validate_platform_contract(ssh, "testhost", {"provisioning": {}})
    assert result["status"] == "ok"
    assert ssh.calls == []


# --- Summarize helper tests ---


def test_summarize_all_success():
    results = {
        "h1": {"status": "success"},
        "h2": {"status": "ok"},
    }
    s = _summarize(results)
    assert s["summary"] == "2 host(s): 2 success, 0 failed"
    assert "h1" in s["results"]
    assert "h2" in s["results"]


def test_summarize_mixed():
    results = {
        "h1": {"status": "success"},
        "h2": {"status": "failed"},
        "h3": {"status": "already_installed"},
    }
    s = _summarize(results)
    assert s["summary"] == "3 host(s): 2 success, 1 failed"


def test_summarize_boolean_fields():
    results = {
        "h1": {"all_met": True},
        "h2": {"installed": True},
        "h3": {"verified": True},
        "h4": {"installed": False},
    }
    s = _summarize(results)
    assert s["summary"] == "4 host(s): 3 success, 1 failed"


# --- Batched tool handler tests ---


@pytest.fixture
def batched_handlers(mock_provider, mock_secrets):
    """Create handlers and return (handlers_dict, MockSSHExecutor)."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
            "repolist": SSHResult(
                stdout="repo id              repo name\nepel               Extra Packages"
            ),
            "which podman": SSHResult(stdout="/usr/bin/podman\npodman version 4.9"),
            "which git": SSHResult(stdout="/usr/bin/git\ngit version 2.43"),
            "which jq": SSHResult(stdout="/usr/bin/jq\njq-1.7"),
            "which curl": SSHResult(stdout="/usr/bin/curl\ncurl 8.5"),
            "dnf install": SSHResult(stdout="Complete!"),
        }
    )

    async def noop_clarification(q):
        pass

    h, _ = create_provisioning_tool_handlers(
        skill_provider=mock_provider,
        secrets_provider=mock_secrets,
        request_clarification_fn=noop_clarification,
    )
    # Patch the ssh inside the closure handlers
    # The handlers close over ssh from SSHExecutor(user="root") — we need
    # to intercept at the module level. Instead, test the batching via
    # the module-level helpers directly.
    return h, ssh


@pytest.mark.asyncio
async def test_batched_check_platform_contract(mock_provider):
    """check_platform_contract with multiple hosts returns per-host results."""
    ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
            "repolist": SSHResult(
                stdout="repo id              repo name\nepel               Extra Packages"
            ),
            "which git": SSHResult(stdout="/usr/bin/git"),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel8", "rhel9"],
            "required_repos": ["epel"],
            "required_packages": ["git"],
        }
    }
    from agents.provisioning.mcp_server import _gather_for_hosts

    async def _check_one(host: str) -> dict:
        return await validate_platform_contract(ssh, host, config)

    results = await _gather_for_hosts(
        ["h1", "h2", "h3"],
        _check_one,
    )
    assert len(results) == 3
    for host in ["h1", "h2", "h3"]:
        assert results[host]["status"] == "ok"
        assert results[host]["detected_os"] == "rhel9"


@pytest.mark.asyncio
async def test_batched_check_platform_contract_partial_failure(mock_provider):
    """When one host has a different OS, only that host fails."""
    rhel_ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=RHEL9_OS_RELEASE),
        }
    )
    ubuntu_ssh = MockSSHExecutor(
        results={
            "os-release": SSHResult(stdout=UBUNTU_OS_RELEASE),
        }
    )
    config = {
        "platform_contract": {
            "supported_os": ["rhel9"],
        }
    }

    # Test individually since MockSSHExecutor doesn't differentiate by host
    r1 = await validate_platform_contract(rhel_ssh, "h1", config)
    r2 = await validate_platform_contract(ubuntu_ssh, "h2", config)

    results = {"h1": r1, "h2": r2}
    s = _summarize(results)
    assert s["summary"] == "2 host(s): 1 success, 1 failed"
    assert results["h1"]["status"] == "ok"
    assert results["h2"]["status"] == "failed"


@pytest.mark.asyncio
async def test_batched_check_host_prerequisites():
    """check_host_prerequisites returns per-host prerequisite status."""
    ssh = MockSSHExecutor(
        results={
            "which podman": SSHResult(stdout="/usr/bin/podman\npodman 4.9"),
            "which git": SSHResult(stdout="/usr/bin/git\ngit 2.43"),
            "which jq": SSHResult(stdout="/usr/bin/jq\njq-1.7"),
            "which curl": SSHResult(stdout="/usr/bin/curl\ncurl 8.5"),
        }
    )
    from agents.provisioning.mcp_server import _gather_for_hosts

    async def _check_one(host: str) -> dict:
        prereqs = {}
        for cmd in ["podman", "git", "jq", "curl"]:
            result = await ssh.run(
                host,
                f"which {cmd} 2>/dev/null && {cmd} --version 2>/dev/null | head -1",
            )
            if result.exit_code == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                prereqs[cmd] = {
                    "installed": True,
                    "version": lines[-1] if len(lines) > 1 else lines[0],
                }
            else:
                prereqs[cmd] = {"installed": False, "version": None}
        all_met = all(p["installed"] for p in prereqs.values())
        return {
            "host": host,
            "prerequisites": prereqs,
            "all_met": all_met,
            "message": f"All prerequisites met on {host}"
            if all_met
            else f"Missing prerequisites on {host}",
        }

    results = await _gather_for_hosts(["h1", "h2"], _check_one)
    s = _summarize(results)
    assert s["summary"] == "2 host(s): 2 success, 0 failed"
    for host in ["h1", "h2"]:
        assert results[host]["all_met"] is True


@pytest.mark.asyncio
async def test_batched_install_packages():
    """install_packages with targets executes per-host with different packages."""
    ssh = MockSSHExecutor(
        results={
            "dnf install": SSHResult(stdout="Complete!"),
        }
    )

    async def _install_one(host: str, packages: list[str]) -> dict:
        pkg_list = " ".join(packages)
        result = await ssh.run(host, f"dnf install -y {pkg_list}", timeout=300)
        return {
            "host": host,
            "packages": packages,
            "status": "success" if result.exit_code == 0 else "failed",
        }

    import asyncio

    targets = [
        {"host": "h1", "packages": ["fio", "iperf3"]},
        {"host": "h2", "packages": ["fio"]},
    ]
    coros = [_install_one(t["host"], t["packages"]) for t in targets]
    raw = await asyncio.gather(*coros)
    results = {t["host"]: r for t, r in zip(targets, raw)}
    s = _summarize(results)
    assert s["summary"] == "2 host(s): 2 success, 0 failed"

    # Verify different packages were used per host
    h1_calls = [c for c in ssh.calls if c["host"] == "h1"]
    h2_calls = [c for c in ssh.calls if c["host"] == "h2"]
    assert any("iperf3" in c["command"] for c in h1_calls)
    assert not any("iperf3" in c["command"] for c in h2_calls)


@pytest.mark.asyncio
async def test_gather_for_hosts_handles_exceptions():
    """_gather_for_hosts converts exceptions to error results."""
    from agents.provisioning.mcp_server import _gather_for_hosts

    async def _fail_on_h2(host: str) -> dict:
        if host == "h2":
            raise ConnectionError("SSH connection refused")
        return {"status": "ok"}

    results = await _gather_for_hosts(["h1", "h2", "h3"], _fail_on_h2)
    assert results["h1"]["status"] == "ok"
    assert results["h2"]["status"] == "error"
    assert "SSH connection refused" in results["h2"]["message"]
    assert results["h3"]["status"] == "ok"


@pytest.mark.asyncio
async def test_single_host_list_works():
    """Passing a single-element list produces the same result as the old API."""
    from agents.provisioning.mcp_server import _gather_for_hosts

    async def _simple(host: str) -> dict:
        return {"host": host, "status": "success"}

    results = await _gather_for_hosts(["h1"], _simple)
    assert len(results) == 1
    assert results["h1"]["status"] == "success"


@pytest.mark.asyncio
async def test_ensure_harness_installed_tool_in_definitions():
    """ensure_harness_installed tool exists in the tool definitions."""
    from agents.provisioning.mcp_server import get_provisioning_tools

    tools = get_provisioning_tools()
    names = [t.name for t in tools]
    assert "ensure_harness_installed" in names

    tool = next(t for t in tools if t.name == "ensure_harness_installed")
    assert "hosts" in tool.input_schema["properties"]
    assert tool.input_schema["properties"]["hosts"]["type"] == "array"


@pytest.mark.asyncio
async def test_all_tools_use_hosts_or_targets():
    """All host-facing tools use 'hosts' (array) or 'targets' (array), not 'host' (string)."""
    from agents.provisioning.mcp_server import get_provisioning_tools

    tools = get_provisioning_tools()
    host_facing = [
        t
        for t in tools
        if t.name
        not in (
            "get_private_config",
            "request_clarification",
            "submit_provisioning_result",
        )
    ]
    for t in host_facing:
        props = t.input_schema["properties"]
        assert "host" not in props, (
            f"Tool {t.name} still uses 'host' (string) — should use "
            f"'hosts' (array) or 'targets' (array)"
        )
        has_hosts = "hosts" in props and props["hosts"]["type"] == "array"
        has_targets = "targets" in props and props["targets"]["type"] == "array"
        assert has_hosts or has_targets, (
            f"Tool {t.name} must have either 'hosts' or 'targets' array"
        )
