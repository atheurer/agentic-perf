"""Tests for directive key normalization and validation."""

from __future__ import annotations

# Import harness modules to trigger their directive registrations.
import providers.skills.boot_time  # noqa: F401
from providers.directives import (
    format_normalization_report,
    normalize_directives,
    normalize_key,
)


class TestNormalizeKey:
    """Individual key normalization."""

    def test_recognized_key_unchanged(self):
        canonical, reason = normalize_key("power_off_delay")
        assert canonical == "power_off_delay"
        assert reason is None

    def test_suffix_stripped(self):
        canonical, reason = normalize_key("power_off_delay_seconds")
        assert canonical == "power_off_delay"
        assert "suffix" in reason

    def test_suffix_sec(self):
        canonical, reason = normalize_key("power_off_delay_sec")
        assert canonical == "power_off_delay"

    def test_prefix_stripped(self):
        canonical, reason = normalize_key("jumpstarter_power_off_delay")
        assert canonical == "power_off_delay"
        assert "prefix" in reason

    def test_semantic_map_reboot_count(self):
        canonical, reason = normalize_key("reboot_count")
        assert canonical == "sample_count"
        assert "alias" in reason

    def test_semantic_map_boot_cycles(self):
        canonical, reason = normalize_key("boot_cycles")
        assert canonical == "sample_count"

    def test_semantic_map_reboot_type(self):
        canonical, reason = normalize_key("reboot_type")
        assert canonical == "reboot_method"

    def test_semantic_map_serial(self):
        canonical, reason = normalize_key("serial")
        assert canonical == "serial_capture"

    def test_semantic_map_serial_console(self):
        canonical, reason = normalize_key("serial_console_capture")
        assert canonical == "serial_capture"

    def test_unrecognized_passthrough(self):
        canonical, reason = normalize_key("totally_unknown_key")
        assert canonical == "totally_unknown_key"
        assert reason is None

    def test_combined_prefix_and_suffix(self):
        canonical, reason = normalize_key("jumpstarter_power_off_delay_seconds")
        assert canonical == "power_off_delay"
        assert "prefix" in reason and "suffix" in reason


class TestNormalizeDirectives:
    """Full directive dict normalization."""

    def test_all_recognized_unchanged(self):
        directives = {
            "harness": "boot-time",
            "serial_capture": True,
            "sample_count": 50,
        }
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized == directives
        assert applied == []
        assert unrecognized == []

    def test_mixed_normalization(self):
        directives = {
            "harness": "boot-time",
            "reboot_count": 50,
            "power_off_delay_seconds": 45,
            "serial_console_capture": True,
        }
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized["harness"] == "boot-time"
        assert normalized["sample_count"] == 50
        assert normalized["power_off_delay"] == 45
        assert normalized["serial_capture"] is True
        assert len(applied) == 3
        assert unrecognized == []

    def test_unrecognized_keys_preserved_with_warning(self):
        directives = {
            "harness": "boot-time",
            "totally_custom_thing": "value",
        }
        normalized, applied, unrecognized = normalize_directives(directives)
        assert "totally_custom_thing" in normalized
        assert len(unrecognized) == 1
        assert "totally_custom_thing" in unrecognized[0]

    def test_fuzzy_match_suggestions(self):
        directives = {"serial_captur": True}  # typo
        normalized, applied, unrecognized = normalize_directives(directives)
        assert len(unrecognized) == 1
        assert "serial_capture" in unrecognized[0]

    def test_boolean_cold_reboot_infers_method(self):
        directives = {"cold_reboot": True}
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized["reboot_method"] == "cold"

    def test_boolean_cold_reboot_false_reported(self):
        directives = {"cold_reboot": False, "harness": "boot-time"}
        normalized, applied, unrecognized = normalize_directives(directives)
        assert "reboot_method" not in normalized
        assert normalized["harness"] == "boot-time"
        assert any("skipped" in a for a in applied)

    def test_duplicate_after_normalization_keeps_first(self):
        directives = {
            "sample_count": 50,
            "reboot_count": 100,
        }
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized["sample_count"] == 50

    def test_sample_count_coerced_to_int(self):
        directives = {"reboot_count": "25"}
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized["sample_count"] == 25

    def test_empty_directives(self):
        normalized, applied, unrecognized = normalize_directives({})
        assert normalized == {}
        assert applied == []
        assert unrecognized == []


class TestFormatReport:
    """Normalization report formatting."""

    def test_no_changes_returns_none(self):
        assert format_normalization_report([], []) is None

    def test_normalizations_reported(self):
        report = format_normalization_report(
            ["'reboot_count' is an alias for 'sample_count'"],
            [],
        )
        assert "reboot_count" in report
        assert "sample_count" in report

    def test_unrecognized_reported(self):
        report = format_normalization_report(
            [],
            ["Unrecognized directive 'foo' — no close match found"],
        )
        assert "foo" in report
        assert "⚠️" in report

    def test_both_reported(self):
        report = format_normalization_report(
            ["'x' normalized to 'y'"],
            ["Unrecognized directive 'z'"],
        )
        assert "normalization" in report.lower()
        assert "Unrecognized" in report
