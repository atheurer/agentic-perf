"""Tests for kernel provisioning tools (get_kernel_inventory, etc.)."""

from __future__ import annotations

import pytest

from tests.conftest import make_provisioning_handlers
from tests.fake_rhel import FakeFleetSSH, FakeRHELHost

KERNEL_A = "5.14.0-503.14.1.el9_5.x86_64"
KERNEL_B = "5.14.0-503.16.1.el9_5.x86_64"


@pytest.fixture
def two_hosts():
    h1 = FakeRHELHost("host1", running=KERNEL_A, available=[KERNEL_B])
    h2 = FakeRHELHost("host2", running=KERNEL_A, available=[KERNEL_B])
    return {"10.0.0.1": h1, "10.0.0.2": h2}


@pytest.fixture
def fleet_ssh(two_hosts):
    return FakeFleetSSH(two_hosts)


@pytest.fixture
def handlers(fleet_ssh):
    return make_provisioning_handlers(fleet_ssh)


class TestGetKernelInventory:
    @pytest.mark.asyncio
    async def test_two_hosts(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1", "10.0.0.2"],
        )
        assert "10.0.0.1" in result["results"]
        assert "10.0.0.2" in result["results"]
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "ok"
        assert h1["inventory"]["running"] == KERNEL_A

    @pytest.mark.asyncio
    async def test_unreachable_host(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1", "10.99.99.99"],
        )
        assert result["results"]["10.0.0.1"]["state"] == "ok"
        assert result["results"]["10.99.99.99"]["state"] in (
            "unreachable",
            "not_checked",
            "error",
        )

    @pytest.mark.asyncio
    async def test_with_kernel_installed(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_A,
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["requested"]["state"] == "installed"
        assert h1["requested"]["release"] == KERNEL_A

    @pytest.mark.asyncio
    async def test_with_kernel_available(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_B,
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["requested"]["state"] == "available"

    @pytest.mark.asyncio
    async def test_with_kernel_not_found(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"],
            kernel="9.99.0-1.el99.x86_64",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["requested"]["state"] == "not_found"


class TestInstallKernel:
    @pytest.mark.asyncio
    async def test_install_new_kernel(self, handlers, fleet_ssh):
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_B,
            approval_request_id="test-approval",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "installed"
        assert KERNEL_B in h1["after"]["installed"]
        dnf_calls = [
            c
            for c in fleet_ssh.calls
            if "dnf install" in c["command"] and c["host"] == "10.0.0.1"
        ]
        assert len(dnf_calls) == 1
        assert KERNEL_B in dnf_calls[0]["command"]

    @pytest.mark.asyncio
    async def test_idempotent_install(self, handlers, fleet_ssh):
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_A,
            approval_request_id="test-approval",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "already_installed"
        dnf_calls = [c for c in fleet_ssh.calls if "dnf install" in c["command"]]
        assert len(dnf_calls) == 0


class TestSelectDefaultKernel:
    @pytest.mark.asyncio
    async def test_select_current_default(self, handlers):
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_A,
            approval_request_id="test-approval",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "already_default"

    @pytest.mark.asyncio
    async def test_select_new_default(self, handlers, two_hosts, fleet_ssh):
        two_hosts["10.0.0.1"].installed.append(KERNEL_B)
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1"],
            kernel=KERNEL_B,
            approval_request_id="test-approval",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "ok"
        assert h1["after"] == f"/boot/vmlinuz-{KERNEL_B}"

    @pytest.mark.asyncio
    async def test_not_bootable(self, handlers, two_hosts):
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1"],
            kernel="9.99.0-1.el99.x86_64",
            approval_request_id="test-approval",
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "not_bootable"


class TestSelfHostRefusal:
    def test_localhost_is_self(self):
        from agents.server_utils import is_self_host

        assert is_self_host("localhost") is True
        assert is_self_host("127.0.0.1") is True
        assert is_self_host("::1") is True

    def test_own_hostname_is_self(self, monkeypatch):
        import socket

        from agents.server_utils import is_self_host

        monkeypatch.setattr(socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(socket, "getfqdn", lambda: "myhost.example.com")
        assert is_self_host("myhost") is True
        assert is_self_host("myhost.example.com") is True

    def test_remote_host_is_not_self(self, monkeypatch):
        import socket

        from agents.server_utils import is_self_host

        monkeypatch.setattr(socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda h, p, *a, **kw: [(socket.AF_INET, 0, 0, "", ("192.168.1.100", 0))]
            if h in ("myhost", "myhost.example.com")
            else [(socket.AF_INET, 0, 0, "", ("10.0.0.99", 0))],
        )
        assert is_self_host("10.0.0.1") is False
