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
_DEST_MAC_LABELS = {"dest mac addr", "dst mac addr", "destination mac addr"}

_FILTER_RE = re.compile(r"(?im)^\s*Filter:\s*(\d+)\s*$")
_FILTER_PREFIX_RE = re.compile(r"(?im)^\s*Filter\s*:")
_TOTAL_RE = re.compile(r"(?im)^\s*Total\s+(\d+)\s+rules?\s*$")
_TOTAL_PREFIX_RE = re.compile(r"(?im)^\s*Total\b")
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


def _canonical_action(value: str) -> dict[str, Any] | None:
    normalized = value.strip()
    if normalized.lower() == "drop":
        return {"type": "drop", "queue": -1}
    queue_match = _QUEUE_ACTION_RE.match(normalized)
    if queue_match:
        queue_text = queue_match.group(1)
        queue = int(queue_text)
        if not queue_text.startswith("-"):
            return {"type": "queue", "queue": queue}
    return None


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


def _mask_width(field_name: str, flow_type: str) -> int | None:
    if field_name in {"src_mac", "dst_mac", "dst_mac_ext"}:
        return 48
    if field_name in {"src_ip", "dst_ip"}:
        if flow_type.endswith("4"):
            return 32
        if flow_type.endswith("6"):
            return 128
        return None
    if field_name in {"tos", "tclass"}:
        return 8
    if field_name in {"src_port", "dst_port", "ethertype", "vlan_ether_type"}:
        return 16
    if field_name in {"proto", "l4proto"}:
        return 8
    if field_name in {"l4data", "spi"}:
        return 32
    if field_name == "vlan":
        return 16
    if field_name == "user_def":
        return 64
    return None


def _field_token_is_valid(value: Any, field_name: str, flow_type: str) -> bool:
    """Reject malformed values instead of comparing opaque text as evidence."""
    text = str(value).strip()
    if field_name in {"src_mac", "dst_mac", "dst_mac_ext"}:
        return re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", text) is not None
    if field_name in {"src_ip", "dst_ip"}:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return False
        if flow_type.endswith("4"):
            expected_version = 4
        elif flow_type.endswith("6"):
            expected_version = 6
        else:
            return False
        return address.version == expected_version

    width = _mask_width(field_name, flow_type)
    if width is None:
        return False
    try:
        number = int(text, 0)
    except ValueError:
        try:
            number = int(text, 10)
        except ValueError:
            return False
    return 0 <= number < (1 << width)


def _full_match_mask(field_name: str, flow_type: str) -> Any | None:
    """Return ethtool's readback mask for an exact match (all zero bits)."""
    width = _mask_width(field_name, flow_type)
    if width is None:
        return None
    if field_name in {"src_mac", "dst_mac", "dst_mac_ext"}:
        return "00:00:00:00:00:00"
    if field_name in {"src_ip", "dst_ip"}:
        return ipaddress.ip_address("0.0.0.0" if width == 32 else "::")
    return 0


def _is_wildcard_mask(value: Any, field_name: str, flow_type: str) -> bool:
    """Return whether an ethtool readback mask ignores every field bit."""
    width = _mask_width(field_name, flow_type)
    if value is None or width is None:
        return False
    if field_name in {"src_mac", "dst_mac", "dst_mac_ext"}:
        return _normalize_value(value) == ("text", "ff:ff:ff:ff:ff:ff")
    if field_name in {"src_ip", "dst_ip"}:
        try:
            address = ipaddress.ip_address(str(value).strip())
        except ValueError:
            return False
        return address.version == (4 if width == 32 else 6) and int(address) == (
            (1 << width) - 1
        )
    return _normalize_value(value) == ("number", (1 << width) - 1)


def flow_rule_is_verifiable(actual: dict[str, Any]) -> bool:
    """Check that a raw parsed rule is complete enough for verification."""
    if actual.get("partial") is True or any(
        actual.get(key)
        for key in ("unparsed_lines", "missing_details", "duplicate_details")
    ):
        return False

    raw_flow = actual.get("flow_type")
    if not isinstance(raw_flow, dict):
        return False
    flow_label = raw_flow.get("label")
    flow_value = raw_flow.get("value")
    if (
        not isinstance(flow_label, str)
        or flow_label.strip().lower() not in {"rule type", "flow type"}
        or not isinstance(flow_value, str)
    ):
        return False
    flow_type = _canonical_flow_type(flow_value)
    if flow_type is None:
        return False

    raw_action = actual.get("action")
    if (
        not isinstance(raw_action, dict)
        or not isinstance(raw_action.get("label"), str)
        or raw_action["label"].strip().lower() != "action"
        or not isinstance(raw_action.get("value"), str)
        or _canonical_action(raw_action["value"]) is None
    ):
        return False

    fields = actual.get("fields")
    if not isinstance(fields, dict) or not _REQUIRED_FIELDS[flow_type] <= fields.keys():
        return False
    for field_name, field in fields.items():
        if field_name.startswith("raw:") or not isinstance(field, dict):
            return False
        label = field.get("label")
        if not isinstance(label, str):
            return False
        normalized_label = label.strip().lower()
        if field_name == "dst_mac_ext":
            if flow_type != "ether" or normalized_label not in _DEST_MAC_LABELS:
                return False
        elif _FIELD_NAMES.get(normalized_label) != field_name:
            return False
        value = field.get("value")
        mask = field.get("mask")
        if not _field_token_is_valid(
            value, field_name, flow_type
        ) or not _field_token_is_valid(mask, field_name, flow_type):
            return False
    return True


def flow_rule_matches_config(
    actual: dict[str, Any], configured: dict[str, Any]
) -> bool:
    """Return whether a parsed rule has the configured fields and no extras."""
    if not flow_rule_is_verifiable(actual):
        return False
    configured_flow_type = _canonical_flow_type(str(configured.get("flow_type", "")))
    actual_flow = actual.get("flow_type")
    flow_type_value = actual_flow.get("value")
    flow_type = _canonical_flow_type(flow_type_value)
    if flow_type is None or flow_type != configured_flow_type:
        return False

    queue = configured.get("queue")
    if type(queue) is not int or queue < -1:
        return False
    raw_action = actual.get("action")
    action = _canonical_action(raw_action["value"])
    expected_action_type = "drop" if queue == -1 else "queue"
    if action.get("type") != expected_action_type or action.get("queue") != queue:
        return False

    fields = actual["fields"]
    expected_fields: dict[str, tuple[str, str | None]] = {}
    for field_name, (config_key, mask_key) in _CONFIG_FIELDS.items():
        configured_value = configured.get(config_key)
        if configured_value is None:
            continue
        actual_name = field_name
        if field_name == "tos" and flow_type.endswith("6"):
            actual_name = "tclass"
        expected_fields[actual_name] = (config_key, mask_key)

    for field_name, actual_field in fields.items():
        # `flow_rule_is_verifiable` already checked each raw field structure.
        expected = expected_fields.get(field_name)
        if not _field_token_is_valid(actual_field.get("value"), field_name, flow_type):
            return False
        if not _field_token_is_valid(actual_field.get("mask"), field_name, flow_type):
            return False
        if expected is None:
            if not _is_wildcard_mask(actual_field.get("mask"), field_name, flow_type):
                return False
            continue

        config_key, mask_key = expected
        configured_value = configured.get(config_key)
        if not _field_token_is_valid(configured_value, field_name, flow_type):
            return False
        if actual_field is None or _normalize_value(actual_field.get("value")) != (
            _normalize_value(configured_value)
        ):
            return False
        configured_mask = configured.get(mask_key) if mask_key else None
        if configured_mask is not None and not _field_token_is_valid(
            configured_mask, field_name, flow_type
        ):
            return False
        if configured_mask is None:
            expected_mask = _full_match_mask(field_name, flow_type)
        else:
            expected_mask = configured_mask
        if expected_mask is None or _normalize_value(actual_field.get("mask")) != (
            _normalize_value(expected_mask)
        ):
            return False
        if _is_wildcard_mask(actual_field.get("mask"), field_name, flow_type):
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
        if left_fields[key] != right_fields[key]:
            return False
    return True


def parse_ethtool_flow_rules(output: str) -> list[dict[str, Any]]:
    """Parse the text table emitted by ``ethtool -u`` / ``--show-ntuple``.

    Filter markers anchor rule blocks. A total count is optional when filters
    are present, but when supplied it must be unique and match the parsed
    filters. Rule details are extracted as raw evidence; incomplete lines are
    retained and marked so verification can fail closed. Returning an empty list requires an explicit
    ``Total 0 rules``.
    """
    total_matches = list(_TOTAL_RE.finditer(output))
    filter_matches = list(_FILTER_RE.finditer(output))
    filter_prefix_matches = list(_FILTER_PREFIX_RE.finditer(output))
    total_prefix_matches = list(_TOTAL_PREFIX_RE.finditer(output))
    if len(filter_prefix_matches) != len(filter_matches):
        raise ValueError("ethtool output contains a malformed filter marker")
    if len(total_prefix_matches) != len(total_matches):
        raise ValueError("ethtool output contains a malformed rule-table count")
    if len(total_matches) > 1:
        raise ValueError("ethtool output must contain at most one rule-table count")
    total_match = total_matches[0] if total_matches else None
    if not filter_matches:
        if total_match is None:
            raise ValueError("ethtool output has no filters or explicit empty count")
        if int(total_match.group(1)) != 0:
            raise ValueError(
                "ethtool rule-table count mismatch: "
                f"reported {total_match.group(1)}, parsed 0"
            )
        if output[total_match.end() :].strip():
            raise ValueError("ethtool empty-table count has trailing output")
        return []

    # The optional count may be a header before the first filter or a footer
    # after the final filter. A count between filter blocks is ambiguous and
    # must not truncate a rule while parsing.
    if total_match is not None:
        count_position = total_match.start()
        before_first = count_position < filter_matches[0].start()
        after_last = count_position > filter_matches[-1].start()
        if not before_first and not after_last:
            raise ValueError("ethtool rule-table count appears between filters")

    rules: list[dict[str, Any]] = []
    seen_rule_ids: set[int] = set()
    for index, match in enumerate(filter_matches):
        rule_id = int(match.group(1))
        if rule_id in seen_rule_ids:
            raise ValueError(f"ethtool output contains duplicate filter ID {rule_id}")
        seen_rule_ids.add(rule_id)
        end = (
            filter_matches[index + 1].start()
            if index + 1 < len(filter_matches)
            else len(output)
        )
        if (
            index == len(filter_matches) - 1
            and total_match is not None
            and total_match.start() > match.start()
        ):
            end = total_match.start()
        block = output[match.end() : end]
        rule: dict[str, Any] = {
            "id": rule_id,
            "flow_type": None,
            "fields": {},
            "action": None,
            "unparsed_lines": [],
            "missing_details": [],
            "duplicate_details": [],
        }
        deferred_dst_mac_fields: list[tuple[str, dict[str, Any], str]] = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            label, sep, value = line.partition(":")
            if not sep:
                rule["unparsed_lines"].append(raw_line)
                continue
            normalized_label = label.strip().lower()
            if not normalized_label:
                rule["unparsed_lines"].append(raw_line)
                continue
            value = value.strip()
            if normalized_label in {"rule type", "flow type", "unknown flow type"}:
                if rule["flow_type"] is not None:
                    rule["duplicate_details"].append(raw_line)
                    continue
                if not value:
                    rule["missing_details"].append("flow-type value")
                else:
                    rule["flow_type"] = {"label": label.strip(), "value": value}
                continue
            if normalized_label == "action":
                if rule["action"] is not None:
                    rule["duplicate_details"].append(raw_line)
                    continue
                if not value:
                    rule["missing_details"].append("action value")
                else:
                    rule["action"] = {"label": label.strip(), "value": value}
                continue

            field_name = _FIELD_NAMES.get(normalized_label)
            if field_name is None:
                field_name = f"raw:{normalized_label}"
            is_deferred_dst_mac = (
                field_name == "dst_mac"
                and normalized_label in _DEST_MAC_LABELS
                and "dst_mac" in rule["fields"]
            )
            if field_name in rule["fields"] and not is_deferred_dst_mac:
                rule["duplicate_details"].append(raw_line)
                duplicate_number = 2
                field_name = f"raw:duplicate:{normalized_label}:{duplicate_number}"
                while field_name in rule["fields"]:
                    duplicate_number += 1
                    field_name = f"raw:duplicate:{normalized_label}:{duplicate_number}"
            mask_match = _MASK_RE.match(value)
            if mask_match:
                field_value = mask_match.group(1).strip()
                field_mask: str | None = mask_match.group(2).strip() or None
            else:
                field_value = value
                field_mask = None
            if not field_value:
                rule["missing_details"].append(f"value for {label.strip()}")
            if field_mask is None:
                rule["missing_details"].append(f"mask for {label.strip()}")
            parsed_field = {
                "value": field_value,
                "mask": field_mask,
                "label": label.strip(),
            }
            if is_deferred_dst_mac:
                deferred_dst_mac_fields.append(
                    (normalized_label, parsed_field, raw_line)
                )
            else:
                rule["fields"][field_name] = parsed_field

        if deferred_dst_mac_fields:
            flow_type_value = (rule["flow_type"] or {}).get("value", "")
            can_map_dst_mac_ext = (
                _canonical_flow_type(flow_type_value) == "ether"
                and "dst_mac_ext" not in rule["fields"]
            )
            if can_map_dst_mac_ext:
                _, parsed_field, _ = deferred_dst_mac_fields.pop(0)
                rule["fields"]["dst_mac_ext"] = parsed_field
            for normalized_label, parsed_field, raw_line in deferred_dst_mac_fields:
                rule["duplicate_details"].append(raw_line)
                duplicate_number = 2
                duplicate_key = f"raw:duplicate:{normalized_label}:{duplicate_number}"
                while duplicate_key in rule["fields"]:
                    duplicate_number += 1
                    duplicate_key = (
                        f"raw:duplicate:{normalized_label}:{duplicate_number}"
                    )
                rule["fields"][duplicate_key] = parsed_field
        if rule["flow_type"] is None:
            rule["missing_details"].append("flow type")
        if rule["action"] is None:
            rule["missing_details"].append("action")
        rule["partial"] = bool(
            rule["unparsed_lines"]
            or rule["missing_details"]
            or rule["duplicate_details"]
        )
        rules.append(rule)

    if total_match is not None and int(total_match.group(1)) != len(rules):
        raise ValueError(
            "ethtool rule-table count mismatch: "
            f"reported {total_match.group(1)}, parsed {len(rules)}"
        )
    return rules
