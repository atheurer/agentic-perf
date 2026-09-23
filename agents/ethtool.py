"""Parsing helpers for read-only ethtool flow-rule inspection."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

_FLOW_TYPES = {
    "ether": "ether",
    "ethernet": "ether",
    "raw ethernet": "ether",
    "ip4": "ip4",
    "raw ipv4": "ip4",
    "tcp4": "tcp4",
    "tcp over ipv4": "tcp4",
    "udp4": "udp4",
    "udp over ipv4": "udp4",
    "sctp4": "sctp4",
    "sctp over ipv4": "sctp4",
    "ah4": "ah4",
    "ipsec ah over ipv4": "ah4",
    "esp4": "esp4",
    "ipsec esp over ipv4": "esp4",
    "ip6": "ip6",
    "raw ipv6": "ip6",
    "tcp6": "tcp6",
    "tcp over ipv6": "tcp6",
    "udp6": "udp6",
    "udp over ipv6": "udp6",
    "sctp6": "sctp6",
    "sctp over ipv6": "sctp6",
    "ah6": "ah6",
    "ipsec ah over ipv6": "ah6",
    "esp6": "esp6",
    "ipsec esp over ipv6": "esp6",
}

_FIELD_NAMES = {
    "src mac addr": "src_mac",
    "source mac addr": "src_mac",
    "dest mac addr": "dst_mac",
    "dst mac addr": "dst_mac",
    "destination mac addr": "dst_mac",
    "src ip addr": "src_ip",
    "source ip addr": "src_ip",
    "dest ip addr": "dst_ip",
    "dst ip addr": "dst_ip",
    "destination ip addr": "dst_ip",
    "src port": "src_port",
    "source port": "src_port",
    "dest port": "dst_port",
    "dst port": "dst_port",
    "destination port": "dst_port",
    "tos": "tos",
    "traffic class": "tclass",
    "tclass": "tclass",
    "proto": "proto",
    "protocol": "proto",
    "l4proto": "l4proto",
    "spi": "spi",
    "l4data": "l4data",
    "l4 bytes": "l4data",
    "ethertype": "ethertype",
    "vlan ethertype": "vlan_ether_type",
    "vlan ether type": "vlan_ether_type",
    "vlan": "vlan",
    "user-defined": "user_def",
    "user-defined data": "user_def",
}

_FILTER_RE = re.compile(r"(?im)^\s*Filter:\s*(\d+)\s*$")
_TOTAL_RE = re.compile(r"(?im)^\s*Total\s+(\d+)\s+rules?\s*$")
_MASK_RE = re.compile(r"^(.*?)\s+mask:\s*(.*?)\s*$", re.IGNORECASE)
_QUEUE_ACTION_RE = re.compile(r"^direct to queue\s+(-?\d+)\s*$", re.IGNORECASE)

_CONFIG_FIELDS = {
    "src_mac": ("src_mac", "src_mac_mask"),
    "dst_mac": ("dst_mac", "dst_mac_mask"),
    "src_ip": ("src_ip", "src_ip_mask"),
    "dst_ip": ("dst_ip", "dst_ip_mask"),
    "tos": ("tos", "tos_mask"),
    "src_port": ("src_port", "src_port_mask"),
    "dst_port": ("dst_port", "dst_port_mask"),
    "ethertype": ("ethertype", "ethertype_mask"),
    "vlan_ether_type": ("vlan_ether_type", None),
}

_REQUIRED_FIELDS = {
    "ether": {"src_mac", "dst_mac", "ethertype"},
    "ip4": {"src_ip", "dst_ip", "tos", "proto", "l4data"},
    "tcp4": {"src_ip", "dst_ip", "tos", "src_port", "dst_port"},
    "udp4": {"src_ip", "dst_ip", "tos", "src_port", "dst_port"},
    "sctp4": {"src_ip", "dst_ip", "tos", "src_port", "dst_port"},
    "ah4": {"src_ip", "dst_ip", "tos", "spi"},
    "esp4": {"src_ip", "dst_ip", "tos", "spi"},
    "ip6": {"src_ip", "dst_ip", "tclass", "proto", "l4data"},
    "tcp6": {"src_ip", "dst_ip", "tclass", "src_port", "dst_port"},
    "udp6": {"src_ip", "dst_ip", "tclass", "src_port", "dst_port"},
    "sctp6": {"src_ip", "dst_ip", "tclass", "src_port", "dst_port"},
    "ah6": {"src_ip", "dst_ip", "tclass", "spi"},
    "esp6": {"src_ip", "dst_ip", "tclass", "spi"},
}


def _canonical_flow_type(value: str) -> str | None:
    return _FLOW_TYPES.get(value.strip().lower())


def _normalize_value(value: Any) -> tuple[str, Any]:
    text = str(value).strip().lower()
    try:
        return ("number", int(text, 0))
    except ValueError:
        try:
            return ("number", int(text, 10))
        except ValueError:
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                return ("text", text)
            return ("ip", (address.version, address.packed))


def _is_wildcard_mask(value: Any) -> bool:
    if value is None:
        return False
    return _normalize_value(value) in {
        ("number", 0),
        ("ip", (4, ipaddress.IPv4Address("0.0.0.0").packed)),
        ("ip", (6, ipaddress.IPv6Address("::").packed)),
        ("text", "00:00:00:00:00:00"),
    }


def _full_match_mask(field_name: str, flow_type: str) -> Any | None:
    """Return the mask for an exact match, or None when the width is unknown."""
    if field_name in {"src_mac", "dst_mac", "dst_mac_ext"}:
        return "ff:ff:ff:ff:ff:ff"
    if field_name in {"src_ip", "dst_ip"}:
        if flow_type.endswith("4"):
            return ipaddress.IPv4Address("255.255.255.255")
        if flow_type.endswith("6"):
            return ipaddress.IPv6Address("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
        return None
    if field_name in {"tos", "tclass"}:
        return 0xFF
    if field_name in {"src_port", "dst_port", "ethertype", "vlan_ether_type"}:
        return 0xFFFF
    return None


def flow_rule_matches_config(
    actual: dict[str, Any], configured: dict[str, Any]
) -> bool:
    """Return whether a parsed rule has the configured fields and no extras."""
    if _canonical_flow_type(str(configured.get("flow_type", ""))) != actual.get(
        "flow_type"
    ):
        return False

    queue = configured.get("queue")
    action = actual.get("action") or {}
    expected_action_type = "drop" if queue == -1 else "queue"
    if action.get("type") != expected_action_type or action.get("queue") != queue:
        return False

    fields = actual.get("fields") or {}
    expected_fields: dict[str, tuple[str, str | None]] = {}
    for field_name, (config_key, mask_key) in _CONFIG_FIELDS.items():
        configured_value = configured.get(config_key)
        if configured_value is None:
            continue
        actual_name = field_name
        if field_name == "tos" and str(actual.get("flow_type", "")).endswith("6"):
            actual_name = "tclass"
        expected_fields[actual_name] = (config_key, mask_key)

    for field_name, actual_field in fields.items():
        if not isinstance(actual_field, dict) or actual_field.get("mask") is None:
            return False
        expected = expected_fields.get(field_name)
        if expected is None:
            if not _is_wildcard_mask(actual_field.get("mask")):
                return False
            continue

        config_key, mask_key = expected
        configured_value = configured.get(config_key)
        if actual_field is None or _normalize_value(actual_field.get("value")) != (
            _normalize_value(configured_value)
        ):
            return False
        configured_mask = configured.get(mask_key) if mask_key else None
        expected_mask = configured_mask
        if expected_mask is None:
            expected_mask = _full_match_mask(
                field_name, str(actual.get("flow_type", ""))
            )
        if expected_mask is None or _normalize_value(actual_field.get("mask")) != (
            _normalize_value(expected_mask)
        ):
            return False
    return expected_fields.keys() <= fields.keys()


def same_parsed_flow_rule(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare two read-back entries while ignoring the driver-assigned ID."""
    if left.get("flow_type") != right.get("flow_type"):
        return False
    if left.get("action") != right.get("action"):
        return False
    left_fields = left.get("fields") or {}
    right_fields = right.get("fields") or {}
    if left_fields.keys() != right_fields.keys():
        return False
    for key in left_fields:
        for part in ("value", "mask"):
            if _normalize_value(left_fields[key].get(part)) != _normalize_value(
                right_fields[key].get(part)
            ):
                return False
    return True


def parse_ethtool_flow_rules(output: str) -> list[dict[str, Any]]:
    """Parse the text table emitted by ``ethtool -u`` / ``--show-ntuple``.

    Raise ``ValueError`` when ethtool output does not contain a recognizable
    rule-table header or when its declared rule count does not match the
    parsed filters. Returning an empty list is reserved for a confirmed
    ``Total 0 rules`` response.
    """
    total_match = _TOTAL_RE.search(output)
    total_matches = list(_TOTAL_RE.finditer(output))
    filter_matches = list(_FILTER_RE.finditer(output))
    if total_match is None or len(total_matches) != 1:
        raise ValueError("ethtool output must contain one rule-table count")
    if filter_matches and total_match.start() > filter_matches[0].start():
        raise ValueError("ethtool rule-table count appears after the first filter")

    preamble_end = filter_matches[0].start() if filter_matches else len(output)
    preamble = output[:preamble_end]
    allowed_header = total_match.group(0).strip()
    if any(
        line.strip() and line.strip() != allowed_header
        for line in preamble.splitlines()
    ):
        raise ValueError("ethtool output contains unrecognized text before the rules")

    rules: list[dict[str, Any]] = []
    for index, match in enumerate(filter_matches):
        end = (
            filter_matches[index + 1].start()
            if index + 1 < len(filter_matches)
            else len(output)
        )
        block = output[match.end() : end]
        rule: dict[str, Any] = {
            "id": int(match.group(1)),
            "flow_type": None,
            "fields": {},
            "action": None,
        }
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            label, sep, value = line.partition(":")
            if not sep:
                raise ValueError(f"unrecognized ethtool rule output: {line!r}")
            normalized_label = label.strip().lower()
            value = value.strip()
            if normalized_label in {"rule type", "flow type"}:
                canonical_type = _canonical_flow_type(value)
                if canonical_type is None:
                    raise ValueError(f"unsupported ethtool flow type: {value!r}")
                if rule["flow_type"] is not None:
                    raise ValueError("ethtool rule contains duplicate flow-type fields")
                rule["flow_type"] = canonical_type
                continue
            if normalized_label == "action":
                if value.lower() == "drop":
                    if rule["action"] is not None:
                        raise ValueError(
                            "ethtool rule contains duplicate action fields"
                        )
                    rule["action"] = {"type": "drop", "queue": -1}
                    continue
                queue_match = _QUEUE_ACTION_RE.match(value)
                if queue_match:
                    queue = int(queue_match.group(1))
                    if queue < 0:
                        raise ValueError(f"unsupported ethtool queue action: {value!r}")
                    if rule["action"] is not None:
                        raise ValueError(
                            "ethtool rule contains duplicate action fields"
                        )
                    rule["action"] = {
                        "type": "queue",
                        "queue": queue,
                    }
                    continue
                raise ValueError(f"unsupported ethtool action: {value!r}")
            if normalized_label == "unknown flow type":
                raise ValueError(f"ethtool reported an unknown flow type: {value!r}")

            field_name = _FIELD_NAMES.get(normalized_label)
            if field_name is None:
                raise ValueError(
                    f"unsupported ethtool rule qualifier: {label.strip()!r}"
                )
            if field_name in rule["fields"]:
                if field_name == "dst_mac" and rule["flow_type"] == "ether":
                    field_name = "dst_mac_ext"
                else:
                    raise ValueError(
                        f"duplicate ethtool rule qualifier: {label.strip()!r}"
                    )
            if field_name in rule["fields"]:
                raise ValueError(f"duplicate ethtool rule qualifier: {label.strip()!r}")
            mask_match = _MASK_RE.match(value)
            if not mask_match:
                raise ValueError(
                    f"ethtool rule qualifier is missing its mask: {line!r}"
                )
            field_value = mask_match.group(1).strip()
            field_mask = mask_match.group(2).strip()
            if not field_value or not field_mask:
                raise ValueError(f"incomplete ethtool rule qualifier: {line!r}")
            rule["fields"][field_name] = {
                "value": field_value,
                "mask": field_mask,
            }
        if rule["flow_type"] is None:
            raise ValueError(f"ethtool filter {rule['id']} is missing its flow type")
        if rule["action"] is None:
            raise ValueError(f"ethtool filter {rule['id']} is missing its action")
        missing_fields = _REQUIRED_FIELDS[rule["flow_type"]] - rule["fields"].keys()
        if missing_fields:
            missing = ", ".join(sorted(missing_fields))
            raise ValueError(
                f"ethtool filter {rule['id']} is missing match fields: {missing}"
            )
        rules.append(rule)

    if total_match is not None and int(total_match.group(1)) != len(rules):
        raise ValueError(
            "ethtool rule-table count mismatch: "
            f"reported {total_match.group(1)}, parsed {len(rules)}"
        )
    return rules
