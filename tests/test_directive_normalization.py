"""Tests for directive key normalization and validation."""

from __future__ import annotations

import subprocess
import sys

import pytest

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

    def test_suffix_does_not_rewrite_safety_directives(self):
        canonical, reason = normalize_key("skip_teardown_s")
        assert canonical == "skip_teardown_s"
        assert reason is None

    def test_millisecond_suffix_does_not_rewrite_boolean_timeout(self):
        normalized, applied, unrecognized = normalize_directives(
            {"disable_hitl_timeout_ms": 1000}
        )
        assert normalized == {"disable_hitl_timeout_ms": 1000}
        assert applied == []
        assert len(unrecognized) == 1

    def test_prefix_does_not_rewrite_boolean_timeout(self):
        normalized, applied, unrecognized = normalize_directives(
            {"jumpstarter_disable_hitl_timeout": True}
        )
        assert normalized == {"jumpstarter_disable_hitl_timeout": True}
        assert applied == []
        assert len(unrecognized) == 1

    def test_prefix_does_not_rewrite_unrelated_directives(self):
        canonical, reason = normalize_key("jumpstarter_skip_teardown")
        assert canonical == "jumpstarter_skip_teardown"
        assert reason is None


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

    @pytest.mark.parametrize(
        "key", ["reboot_type", "reboot_mode", "boot_type", "boot_mode", "restart_type"]
    )
    def test_generic_reboot_method_boolean_is_omitted_and_reported(self, key):
        normalized, applied, unrecognized = normalize_directives({key: True})
        assert "reboot_method" not in normalized
        assert applied == []
        assert len(unrecognized) == 1
        assert f"'{key}'" in unrecognized[0]
        assert "True" in unrecognized[0]

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
        directives = {"sample_count": "25"}
        normalized, applied, unrecognized = normalize_directives(directives)
        assert normalized["sample_count"] == 25
        assert any("converted to an integer" in note for note in applied)
        assert unrecognized == []

    def test_sample_count_boolean_is_omitted_and_reported(self):
        normalized, applied, unrecognized = normalize_directives({"sample_count": True})
        assert "sample_count" not in normalized
        assert applied == []
        assert any(
            "'sample_count'" in note
            and "True" in note
            and "expected an integer" in note
            for note in unrecognized
        )

    def test_sample_count_fraction_is_omitted_and_reported(self):
        normalized, applied, unrecognized = normalize_directives({"sample_count": 2.5})
        assert "sample_count" not in normalized
        assert applied == []
        assert any(
            "'sample_count'" in note and "2.5" in note and "expected an integer" in note
            for note in unrecognized
        )

    @pytest.mark.parametrize("value", [True, 2.5, 0, -1, "0", "-2"])
    def test_non_positive_or_non_integral_sample_counts_are_reported(self, value):
        normalized, applied, unrecognized = normalize_directives(
            {"sample_count": value}
        )
        assert "sample_count" not in normalized
        assert applied == []
        assert any(
            "'sample_count'" in note
            and repr(value) in note
            and "expected an integer" in note
            for note in unrecognized
        )

    def test_positive_integral_sample_count_number_is_accepted(self):
        normalized, applied, unrecognized = normalize_directives({"sample_count": 2.0})
        assert normalized["sample_count"] == 2
        assert any("converted to an integer" in note for note in applied)
        assert unrecognized == []

    def test_invalid_sample_count_alias_is_omitted_and_reported(self):
        normalized, applied, unrecognized = normalize_directives({"reboot_count": 0})
        assert "sample_count" not in normalized
        assert applied == []
        assert any(
            "'reboot_count'" in note and "0" in note and "expected an integer" in note
            for note in unrecognized
        )

    def test_invalid_duplicate_does_not_replace_valid_sample_count(self):
        normalized, _applied, unrecognized = normalize_directives(
            {"sample_count": 3, "reboot_count": 0}
        )
        assert normalized["sample_count"] == 3
        assert any("'reboot_count'" in note for note in unrecognized)

    def test_valid_sample_count_after_invalid_value_is_kept(self):
        normalized, _applied, unrecognized = normalize_directives(
            {"sample_count": 0, "reboot_count": 3}
        )
        assert normalized["sample_count"] == 3
        assert any("'sample_count'" in note and "0" in note for note in unrecognized)

    def test_millisecond_duration_converts_to_seconds(self):
        normalized, applied, unrecognized = normalize_directives(
            {"power_off_delay_ms": "2500"}
        )
        assert normalized["power_off_delay"] == 2.5
        assert any("milliseconds to seconds" in note for note in applied)
        assert unrecognized == []

    def test_invalid_millisecond_duration_stays_unrecognized(self):
        normalized, applied, unrecognized = normalize_directives(
            {"power_off_delay_ms": "not-a-number"}
        )
        assert normalized["power_off_delay_ms"] == "not-a-number"
        assert applied == []
        assert len(unrecognized) == 1

    def test_jumpstarter_directive_is_available_without_resource_import(self):
        script = (
            "import sys; "
            "from providers.directives import normalize_directives; "
            "assert 'providers.resource.jumpstarter' not in sys.modules; "
            "result = normalize_directives({'jumpstarter_serial': True}); "
            "assert result == ({'jumpstarter_serial': True}, [], [])"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

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
