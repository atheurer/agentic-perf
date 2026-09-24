"""Regression tests for ethtool RX flow-rule parsing and verification."""

from __future__ import annotations

import json

import pytest

from agents.ethtool import (
    flow_rule_is_verifiable,
    flow_rule_matches_config,
    parse_ethtool_flow_rules,
    same_parsed_flow_rule,
)
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
    src_port_mask: str = "0xffff",
    dst_port_mask: str = "0x0",
    extra_qualifier: str = "",
    action: str = "Action: Direct to queue 3",
) -> str:
    return (
        "Total 1 rules\n"
        "\n"
        "Filter: 5\n"
        "Rule Type: TCP over IPv4\n"
        "Src IP addr: 0.0.0.0 mask: 255.255.255.255\n"
        "Dest IP addr: 0.0.0.0 mask: 255.255.255.255\n"
        "TOS: 0x0 mask: 0xff\n"
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
        "Src MAC addr: 00:11:22:33:44:55 mask: 00:00:00:00:00:00\n"
        "Dest MAC addr: 00:aa:bb:cc:dd:ee mask: ff:ff:ff:ff:ff:00\n"
        "Ethertype: 0x0800 mask: 0x0\n"
        "VLAN EtherType: 0x8100 mask: 0x0\n"
        "Action: Direct to queue 2\n"
    )


def test_parser_preserves_unknown_qualifiers_and_incomplete_rules_fail_closed() -> None:
    parsed = parse_ethtool_flow_rules(
        _tcp4_table(extra_qualifier="Future field: VendorToken mask: VendorMask\n")
    )[0]
    unknown = parsed["fields"]["raw:future field"]
    assert unknown == {
        "label": "Future field",
        "value": "VendorToken",
        "mask": "VendorMask",
    }
    assert not flow_rule_matches_config(
        parsed, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )
    changed = {
        **parsed,
        "fields": {key: dict(value) for key, value in parsed["fields"].items()},
    }
    changed["fields"]["raw:future field"]["value"] = "vendortoken"
    assert not same_parsed_flow_rule(parsed, changed)
    changed_action = {**parsed, "action": {**parsed["action"], "value": "drop"}}
    assert not same_parsed_flow_rule(parsed, changed_action)
    changed_flow_type = {
        **parsed,
        "flow_type": {**parsed["flow_type"], "value": "TCP over IPv6"},
    }
    assert not same_parsed_flow_rule(parsed, changed_flow_type)

    missing_action = parse_ethtool_flow_rules(_tcp4_table(action=""))[0]
    assert missing_action["action"] is None
    assert "action" in missing_action["missing_details"]
    assert missing_action["partial"] is True
    assert not flow_rule_is_verifiable(missing_action)

    missing_flow_type = parse_ethtool_flow_rules(
        _tcp4_table().replace("Rule Type: TCP over IPv4\n", "")
    )[0]
    assert missing_flow_type["flow_type"] is None
    assert "flow type" in missing_flow_type["missing_details"]
    assert missing_flow_type["partial"] is True
    assert not flow_rule_is_verifiable(missing_flow_type)

    invalid_value = parse_ethtool_flow_rules(
        _tcp4_table().replace("Dest port: 443", "Dest port: nope")
    )[0]
    assert invalid_value["fields"]["dst_port"]["value"] == "nope"
    assert not flow_rule_matches_config(
        invalid_value, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    invalid_mask = parse_ethtool_flow_rules(
        _tcp4_table().replace(
            "Dest port: 443 mask: 0x0", "Dest port: 443 mask: VendorMask"
        )
    )[0]
    assert invalid_mask["fields"]["dst_port"]["mask"] == "VendorMask"
    assert not flow_rule_matches_config(
        invalid_mask, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    missing_mask = parse_ethtool_flow_rules(
        _tcp4_table(extra_qualifier="Future field: value without mask\n")
    )[0]
    assert missing_mask["fields"]["raw:future field"] == {
        "label": "Future field",
        "value": "value without mask",
        "mask": None,
    }
    assert missing_mask["partial"] is True
    assert not flow_rule_is_verifiable(missing_mask)

    empty_label = parse_ethtool_flow_rules(
        _tcp4_table(extra_qualifier=": vendor-value mask: vendor-mask\n")
    )[0]
    assert empty_label["unparsed_lines"] == [": vendor-value mask: vendor-mask"]
    assert empty_label["partial"] is True
    assert not flow_rule_is_verifiable(empty_label)

    unparsed = parse_ethtool_flow_rules(
        _tcp4_table(extra_qualifier="unexpected text without a colon\n")
    )[0]
    assert unparsed["unparsed_lines"] == ["unexpected text without a colon"]
    assert not flow_rule_is_verifiable(unparsed)

    with pytest.raises(ValueError, match="malformed filter marker"):
        parse_ethtool_flow_rules(_tcp4_table(extra_qualifier="Filter: broken\n"))


def test_parser_preserves_raw_field_values_and_masks_for_verifier() -> None:
    parsed = parse_ethtool_flow_rules(
        _tcp4_table().replace(
            "Dest port: 443 mask: 0x0", "Dest port: 00443 mask: 0X0000"
        )
    )[0]

    assert parsed["fields"]["dst_port"] == {
        "label": "Dest port",
        "value": "00443",
        "mask": "0X0000",
    }
    assert flow_rule_matches_config(
        parsed, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )


def test_parser_preserves_raw_type_action_and_known_field_labels() -> None:
    output = (
        _tcp4_table()
        .replace("Rule Type: TCP over IPv4", "rUlE tYpE: TCP over IPv4")
        .replace("Action: Direct to queue 3", "aCtIoN: Direct to queue 03")
    )
    parsed = parse_ethtool_flow_rules(output)[0]

    assert parsed["flow_type"] == {
        "label": "rUlE tYpE",
        "value": "TCP over IPv4",
    }
    assert parsed["action"] == {"label": "aCtIoN", "value": "Direct to queue 03"}
    assert parsed["fields"]["dst_port"] == {
        "label": "Dest port",
        "value": "443",
        "mask": "0x0",
    }
    assert flow_rule_matches_config(
        parsed, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    unknown_flow = parse_ethtool_flow_rules(
        output.replace("TCP over IPv4", "Vendor Specific Flow")
    )[0]
    assert unknown_flow["flow_type"]["value"] == "Vendor Specific Flow"
    assert not flow_rule_matches_config(
        unknown_flow, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    unknown_flow_label = parse_ethtool_flow_rules(
        output.replace("rUlE tYpE", "Unknown flow type")
    )[0]
    assert unknown_flow_label["flow_type"] == {
        "label": "Unknown flow type",
        "value": "TCP over IPv4",
    }
    assert not flow_rule_matches_config(
        unknown_flow_label, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    unknown_action = parse_ethtool_flow_rules(
        output.replace("Direct to queue 03", "Redirect to VF 2")
    )[0]
    assert unknown_action["action"]["value"] == "Redirect to VF 2"
    assert not flow_rule_matches_config(
        unknown_action, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )

    incomplete = parse_ethtool_flow_rules(
        _tcp4_table().replace("Src IP addr: 0.0.0.0 mask: 255.255.255.255\n", "")
    )[0]
    assert "src_ip" not in incomplete["fields"]
    assert not flow_rule_matches_config(
        incomplete, {"flow_type": "tcp4", "queue": 3, "dst_port": 443}
    )


def test_parser_allows_preamble_and_validates_empty_table_tail_and_count() -> None:
    assert parse_ethtool_flow_rules("8 RX rings available\nTotal 0 rules\n") == []

    with pytest.raises(ValueError, match="empty-table count has trailing output"):
        parse_ethtool_flow_rules("8 RX rings available\nTotal 0 rules\nextra text\n")

    with pytest.raises(ValueError, match="rule-table count mismatch"):
        parse_ethtool_flow_rules("8 RX rings available\nTotal 1 rules\n")

    with pytest.raises(ValueError, match="malformed filter marker"):
        parse_ethtool_flow_rules("Total 0 rules\nFilter: broken\n")

    with pytest.raises(ValueError, match="malformed filter marker"):
        parse_ethtool_flow_rules(
            _tcp4_table().replace("Action:", "Filter: broken\nAction:")
        )

    with pytest.raises(ValueError, match="malformed rule-table count"):
        parse_ethtool_flow_rules(
            _tcp4_table().replace("Total 1 rules", "Total 1 rules reported")
        )

    assert parse_ethtool_flow_rules("8 RX rings available\n" + _tcp4_table())[0][
        "flow_type"
    ] == {"label": "Rule Type", "value": "TCP over IPv4"}


def test_parser_anchors_filters_without_requiring_total_count() -> None:
    without_count = _tcp4_table().replace("Total 1 rules\n", "")
    parsed = parse_ethtool_flow_rules("driver preamble\n" + without_count)
    assert parsed[0]["id"] == 5

    footer_count = without_count + "Total 1 rules\ndriver footer\n"
    assert parse_ethtool_flow_rules(footer_count)[0]["id"] == 5

    with pytest.raises(ValueError, match="no filters or explicit empty count"):
        parse_ethtool_flow_rules("8 RX rings available\n")

    with pytest.raises(ValueError, match="duplicate filter ID"):
        parse_ethtool_flow_rules(
            "Total 2 rules\n"
            + without_count.replace("Filter: 5", "Filter: 5")
            + without_count.replace("Filter: 5", "Filter: 5")
        )

    with pytest.raises(ValueError, match="appears between filters"):
        parse_ethtool_flow_rules(
            "Filter: 5\nRule Type: TCP over IPv4\nTotal 1 rules\nFilter: 6\n"
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

    # ethtool's all-ones mask ignores the whole field, so the supplied port
    # value cannot be considered a successfully verified match.
    configured["dst_port_mask"] = "0xffff"
    assert not flow_rule_matches_config(actual, configured)

    configured.pop("dst_port_mask")
    actual["fields"]["src_port"]["mask"] = "0x0"
    assert not flow_rule_matches_config(actual, configured)
    actual["fields"]["src_port"]["mask"] = "0xffff"
    actual["fields"]["dst_port"]["mask"] = "0x0"
    assert flow_rule_matches_config(actual, configured)
    assert not flow_rule_matches_config(
        actual, {"flow_type": "tcp4", "queue": 3, "dst_port": "not-a-port"}
    )


def test_ipv6_fields_compare_semantically_and_map_tos_to_tclass() -> None:
    output = (
        "Total 1 rules\n"
        "Filter: 8\n"
        "Rule Type: TCP over IPv6\n"
        "Src IP addr: :: mask: ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff\n"
        "Dest IP addr: 2001:db8::2 mask: ffff:ffff:ffff:ffff::\n"
        "Traffic Class: 0x10 mask: 0x0\n"
        "Src port: 0 mask: 0xffff\n"
        "Dest port: 443 mask: 0x0\n"
        "Action: Direct to queue 1\n"
    )
    actual = parse_ethtool_flow_rules(output)[0]
    configured = {
        "flow_type": "tcp6",
        "queue": 1,
        "dst_ip": "2001:0db8:0000:0000:0000:0000:0000:0002",
        "dst_ip_mask": "ffff:ffff:ffff:ffff:0000:0000:0000:0000",
        "tos": 16,
        "tos_mask": "0",
        "dst_port": 443,
    }

    assert flow_rule_matches_config(actual, configured)


def test_ether_command_and_readback_keep_raw_and_vlan_ethertypes_distinct() -> None:
    rule = {
        "flow_type": "ether",
        "queue": 2,
        "src_mac": "00:11:22:33:44:55",
        "dst_mac": "00:aa:bb:cc:dd:ee",
        "dst_mac_mask": "ff:ff:ff:ff:ff:00",
        "ethertype": "0x0800",
        "ethertype_mask": "0x0",
        "vlan_ether_type": "0x8100",
    }
    command = provisioning_server._build_flow_rule_cmd("eth0", rule)

    assert "flow-type ether src 00:11:22:33:44:55" in command
    assert "dst 00:aa:bb:cc:dd:ee m ff:ff:ff:ff:ff:00" in command
    assert "proto 0x0800 m 0x0" in command
    assert "vlan-etype 0x8100" in command
    assert "src-mac" not in command
    assert "dst-mac" not in command
    assert "vlan-ether-type" not in command

    actual = parse_ethtool_flow_rules(_ether_table())[0]
    assert flow_rule_matches_config(actual, rule)
    assert "ethertype" in actual["fields"]
    assert "vlan_ether_type" in actual["fields"]


def test_raw_ether_second_destination_mac_is_retained_as_extension() -> None:
    output = _ether_table().replace(
        "Ethertype:",
        "Dest MAC addr: 00:bb:cc:dd:ee:ff mask: ff:ff:ff:ff:ff:ff\nEthertype:",
    )
    actual = parse_ethtool_flow_rules(output)[0]

    assert actual["fields"]["dst_mac_ext"] == {
        "label": "Dest MAC addr",
        "value": "00:bb:cc:dd:ee:ff",
        "mask": "ff:ff:ff:ff:ff:ff",
    }
    assert flow_rule_is_verifiable(actual)
    assert flow_rule_matches_config(
        actual,
        {
            "flow_type": "ether",
            "queue": 2,
            "src_mac": "00:11:22:33:44:55",
            "dst_mac": "00:aa:bb:cc:dd:ee",
            "dst_mac_mask": "ff:ff:ff:ff:ff:00",
            "ethertype": "0x0800",
            "ethertype_mask": "0x0",
            "vlan_ether_type": "0x8100",
        },
    )


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
    output_with_vendor_field = _ether_table().replace(
        "Action:", "Vendor qualifier: Token mask: VendorMask\nAction:"
    )
    infra_ssh = MockSSHExecutor(
        {"ethtool -u": SSHResult(stdout=output_with_vendor_field)}
    )
    monkeypatch.setattr(infra_server, "_ssh", infra_ssh)
    infra_result = json.loads(
        await infra_server.get_ethtool_info("host.example", "eth0", mode="flow_rules")
    )
    assert infra_result["rule_count"] == 1
    assert infra_result["partial"] is True
    assert infra_result["data"]["rules"][0]["fields"]["ethertype"]["value"] == "0x0800"
    assert infra_result["stdout"] == output_with_vendor_field
    assert infra_result["data"]["rules"][0]["fields"]["raw:vendor qualifier"] == {
        "label": "Vendor qualifier",
        "value": "Token",
        "mask": "VendorMask",
    }

    provision_ssh = MockSSHExecutor(
        {"ethtool -u": SSHResult(stdout=output_with_vendor_field)}
    )
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
    assert provision_result["status"] == "error"
    assert provision_result["results"][0]["stdout"] == output_with_vendor_field
    assert provision_result["results"][0]["partial"] is True
    assert provision_result["results"][0]["rule_count"] == 1
    assert (
        provision_result["results"][0]["rules"][0]["fields"]["vlan_ether_type"]["value"]
        == "0x8100"
    )
    assert (
        provision_result["results"][0]["rules"][0]["fields"]["raw:vendor qualifier"][
            "value"
        ]
        == "Token"
    )


@pytest.mark.asyncio
async def test_reset_flow_steering_uses_shared_parser_and_aborts_on_read_error(
    monkeypatch,
) -> None:
    ssh = MockSSHExecutor({"ethtool -u": SSHResult(stdout="unrecognized output\n")})
    monkeypatch.setattr(provisioning_server, "_ssh", ssh)

    result = await provisioning_server._reset_flow_steering_one("host.example", "eth0")

    assert result["status"] == "error"
    assert "could not verify ethtool rule table" in result["errors"][0]
    assert len(ssh.calls) == 1
    assert "ethtool -u" in ssh.calls[0]["command"]
    assert "ethtool -n" not in ssh.calls[0]["command"]


@pytest.mark.asyncio
async def test_reset_flow_steering_does_not_delete_partial_rule(monkeypatch) -> None:
    partial = _tcp4_table().replace("Dest port: 443 mask: 0x0", "Dest port: 443")
    ssh = MockSSHExecutor({"ethtool -u": SSHResult(stdout=partial)})
    monkeypatch.setattr(provisioning_server, "_ssh", ssh)

    result = await provisioning_server._reset_flow_steering_one("host.example", "eth0")

    assert result["status"] == "error"
    assert result["readback"]["partial"] is True
    assert result["readback"]["rules"][0]["fields"]["dst_port"]["mask"] is None
    assert len(ssh.calls) == 1
    assert "delete" not in ssh.calls[0]["command"]


@pytest.mark.asyncio
async def test_reset_flow_steering_deletes_only_parsed_rule_ids(monkeypatch) -> None:
    ssh = MockSSHExecutor({"ethtool -u": SSHResult(stdout=_tcp4_table())})
    monkeypatch.setattr(provisioning_server, "_ssh", ssh)

    result = await provisioning_server._reset_flow_steering_one("host.example", "eth0")

    assert result["status"] == "ok"
    commands = [call["command"] for call in ssh.calls]
    assert commands == [
        "ethtool -u eth0 2>&1",
        "ethtool -N eth0 delete 5 2>&1",
        "ethtool -K eth0 ntuple off 2>&1",
    ]


@pytest.mark.asyncio
async def test_reset_flow_steering_reports_delete_failure_without_disabling_ntuple(
    monkeypatch,
) -> None:
    ssh = MockSSHExecutor(
        {
            "ethtool -u": SSHResult(stdout=_tcp4_table()),
            "delete 5": SSHResult(exit_code=1, stdout="delete failed"),
        }
    )
    monkeypatch.setattr(provisioning_server, "_ssh", ssh)

    result = await provisioning_server._reset_flow_steering_one("host.example", "eth0")

    assert result["status"] == "error"
    assert result["applied"] == []
    assert "failed to delete existing rule 5" in result["errors"][0]
    assert all("ntuple off" not in call["command"] for call in ssh.calls)


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
