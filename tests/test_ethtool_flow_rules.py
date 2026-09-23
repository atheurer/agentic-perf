"""Regression tests for ethtool RX flow-rule parsing and verification."""

from __future__ import annotations

import json

import pytest

from agents.ethtool import flow_rule_matches_config, parse_ethtool_flow_rules
from agents.infra import server as infra_server
from agents.provisioning import server as provisioning_server
from agents.side_effect_inventory import (
    INVENTORIED_SIDE_EFFECTS,
    INVENTORY_DISPOSITIONS,
)
from agents.tool_audit_policy import POLICY_BY_REGISTRATION
from tests.conftest import MockSSHExecutor, SSHResult


def _tcp4_table(
    *,
    src_port_mask: str = "0x0",
    dst_port_mask: str = "0xffff",
    extra_qualifier: str = "",
    action: str = "Action: Direct to queue 3",
) -> str:
    return (
        "Total 1 rules\n"
        "\n"
        "Filter: 5\n"
        "Rule Type: TCP over IPv4\n"
        "Src IP addr: 0.0.0.0 mask: 0.0.0.0\n"
        "Dest IP addr: 0.0.0.0 mask: 0.0.0.0\n"
        "TOS: 0x0 mask: 0x0\n"
        f"Src port: 0 mask: {src_port_mask}\n"
        f"Dest port: 443 mask: {dst_port_mask}\n"
        f"{extra_qualifier}"
        f"{action}\n"
    )


def _ether_table() -> str:
    return (
        "Total 1 rules\n"
        "\n"
        "Filter: 7\n"
        "Flow Type: Raw Ethernet\n"
        "Src MAC addr: 00:11:22:33:44:55 mask: ff:ff:ff:ff:ff:ff\n"
        "Dest MAC addr: 00:aa:bb:cc:dd:ee mask: ff:ff:ff:ff:ff:ff\n"
        "Ethertype: 0x0800 mask: 0xffff\n"
        "VLAN EtherType: 0x8100 mask: 0xffff\n"
        "Action: Direct to queue 2\n"
    )


def test_parser_rejects_unknown_qualifiers_and_incomplete_rules() -> None:
    with pytest.raises(ValueError, match="unsupported ethtool rule qualifier"):
        parse_ethtool_flow_rules(
            _tcp4_table(extra_qualifier="Future field: 0x1 mask: 0xffff\n")
        )

    with pytest.raises(ValueError, match="missing its action"):
        parse_ethtool_flow_rules(_tcp4_table(action=""))

    with pytest.raises(ValueError, match="missing its flow type"):
        parse_ethtool_flow_rules(
            _tcp4_table().replace("Rule Type: TCP over IPv4\n", "")
        )


def test_config_match_requires_full_default_mask_and_rejects_extra_constraints() -> (
    None
):
    actual = parse_ethtool_flow_rules(_tcp4_table())[0]
    configured = {"flow_type": "tcp4", "queue": 3, "dst_port": 443}

    assert flow_rule_matches_config(actual, configured)

    actual["fields"]["dst_port"]["mask"] = "0x0001"
    assert not flow_rule_matches_config(actual, configured)

    configured["dst_port_mask"] = "0x0001"
    assert flow_rule_matches_config(actual, configured)
    actual["fields"]["dst_port"]["mask"] = "0xffff"
    assert not flow_rule_matches_config(actual, configured)

    configured.pop("dst_port_mask")
    actual["fields"]["src_port"]["mask"] = "0xffff"
    assert not flow_rule_matches_config(actual, configured)
    actual["fields"]["src_port"]["mask"] = "0x0"
    assert flow_rule_matches_config(actual, configured)


def test_ipv6_fields_compare_semantically_and_map_tos_to_tclass() -> None:
    output = (
        "Total 1 rules\n"
        "Filter: 8\n"
        "Rule Type: TCP over IPv6\n"
        "Src IP addr: :: mask: ::\n"
        "Dest IP addr: 2001:db8::2 mask: ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff\n"
        "Traffic Class: 0x10 mask: 0xff\n"
        "Src port: 0 mask: 0x0\n"
        "Dest port: 443 mask: 0xffff\n"
        "Action: Direct to queue 1\n"
    )
    actual = parse_ethtool_flow_rules(output)[0]
    configured = {
        "flow_type": "tcp6",
        "queue": 1,
        "dst_ip": "2001:0db8:0000:0000:0000:0000:0000:0002",
        "dst_ip_mask": "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "tos": 16,
        "tos_mask": "255",
        "dst_port": 443,
    }

    assert flow_rule_matches_config(actual, configured)


def test_ether_command_and_readback_keep_raw_and_vlan_ethertypes_distinct() -> None:
    rule = {
        "flow_type": "ether",
        "queue": 2,
        "src_mac": "00:11:22:33:44:55",
        "dst_mac": "00:aa:bb:cc:dd:ee",
        "ethertype": "0x0800",
        "ethertype_mask": "0xffff",
        "vlan_ether_type": "0x8100",
    }
    command = provisioning_server._build_flow_rule_cmd("eth0", rule)

    assert "flow-type ether src 00:11:22:33:44:55" in command
    assert "dst 00:aa:bb:cc:dd:ee" in command
    assert "proto 0x0800 m 0xffff" in command
    assert "vlan-etype 0x8100" in command
    assert "src-mac" not in command
    assert "dst-mac" not in command
    assert "vlan-ether-type" not in command

    actual = parse_ethtool_flow_rules(_ether_table())[0]
    assert flow_rule_matches_config(actual, rule)
    assert "ethertype" in actual["fields"]
    assert "vlan_ether_type" in actual["fields"]


@pytest.mark.asyncio
async def test_configure_flow_steering_verifies_mocked_readback(monkeypatch) -> None:
    before = "Total 0 rules\n"
    after = _tcp4_table()

    class SequenceSSH:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []
            self.readbacks = iter((before, after))

        async def run(self, host: str, command: str, timeout: int = 300):
            self.calls.append({"host": host, "command": command})
            if "ethtool -u" in command:
                return SSHResult(stdout=next(self.readbacks))
            if "flow-type tcp4" in command:
                return SSHResult(stdout="Added rule with ID 5\n")
            return SSHResult()

    ssh = SequenceSSH()
    monkeypatch.setattr(provisioning_server, "_ssh", ssh)
    result = await provisioning_server._configure_flow_steering_one(
        "host.example",
        "eth0",
        rules=[{"flow_type": "tcp4", "queue": 3, "dst_port": 443}],
        clear_existing=True,
    )

    assert result["status"] == "ok"
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["actual_rule_count"] == 1
    assert [call["host"] for call in ssh.calls] == ["host.example"] * len(ssh.calls)
    assert sum("ethtool -u" in call["command"] for call in ssh.calls) == 2


@pytest.mark.asyncio
async def test_flow_rule_read_tools_parse_mocked_ssh(monkeypatch) -> None:
    infra_ssh = MockSSHExecutor({"ethtool -u": SSHResult(stdout=_ether_table())})
    monkeypatch.setattr(infra_server, "_ssh", infra_ssh)
    infra_result = json.loads(
        await infra_server.get_ethtool_info("host.example", "eth0", mode="flow_rules")
    )
    assert infra_result["rule_count"] == 1
    assert infra_result["data"]["rules"][0]["fields"]["ethertype"]["value"] == "0x0800"

    provision_ssh = MockSSHExecutor({"ethtool -u": SSHResult(stdout=_ether_table())})
    monkeypatch.setattr(provisioning_server, "_ssh", provision_ssh)
    monkeypatch.setattr(provisioning_server, "_initialized", True)
    provision_result = json.loads(
        await provisioning_server.get_flow_steering_rules(
            targets=[
                provisioning_server.FlowSteeringTarget(
                    host="host.example", interface="eth0"
                )
            ]
        )
    )
    assert provision_result["status"] == "ok"
    assert (
        provision_result["results"][0]["rules"][0]["fields"]["vlan_ether_type"]["value"]
        == "0x8100"
    )


def test_flow_rule_read_tool_policy_and_ssh_boundary_are_audited() -> None:
    registration = "agents/provisioning/server.py:get_flow_steering_rules"
    boundary = (
        "agents/provisioning/server.py",
        "_read_flow_steering_rules_one",
        "ssh",
    )

    assert POLICY_BY_REGISTRATION[registration].classification == "read_only"
    assert boundary in INVENTORIED_SIDE_EFFECTS
    assert INVENTORY_DISPOSITIONS[boundary][0] == "audited"
