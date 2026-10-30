"""Boot-time harness directive schema.

Boot-time is a standalone harness without a full skill provider.
It registers its recognized directives and user-facing aliases
so the normalization framework can map variant key names to
canonical forms.
"""

from __future__ import annotations

import re
from typing import Any

from providers.directives import register_directives
from providers.skills.base import register_progress_parser

_SAMPLE_RE = re.compile(rb"^Sample (\d+) at ", re.MULTILINE)


def _parse_boot_time_progress(chunks: list[bytes]) -> dict[str, Any]:
    """Extract sample count from boot-time subprocess stdout."""
    for chunk in reversed(chunks):
        matches = _SAMPLE_RE.findall(chunk)
        if matches:
            return {"samples_collected": int(matches[-1])}
    return {}


register_progress_parser("boot-time", _parse_boot_time_progress)

register_directives(
    recognized={
        "sample_count",
        "reboot_method",
        "power_off_delay",
        "serial_capture",
        "kpi_pattern",
    },
    descriptions={
        "sample_count": "Number of reboot cycles to collect (int, e.g. 50)",
        "reboot_method": "'cold' (power cycle), 'warm' (SSH + cold fallback), or 'warm-only' (SSH, no fallback)",
        "power_off_delay": "Seconds to wait between power off and power on for cold reboot (int, e.g. 20)",
        "serial_capture": "Capture serial console output during test (bool)",
        "kpi_pattern": "Regex pattern for KPI log line matching (string)",
    },
    aliases={
        # Sample count variants
        "reboot_count": "sample_count",
        "boot_count": "sample_count",
        "boot_cycles": "sample_count",
        "num_boots": "sample_count",
        "num_samples": "sample_count",
        "run_count": "sample_count",
        "reboot_samples": "sample_count",
        "cold_reboots": "sample_count",
        "cold_boots": "sample_count",
        "cold_reboots_per_board": "sample_count",
        "samples": "sample_count",
        "sample": "sample_count",
        # Reboot method variants
        "reboot_type": "reboot_method",
        "reboot_mode": "reboot_method",
        "boot_type": "reboot_method",
        "boot_mode": "reboot_method",
        "restart_type": "reboot_method",
        "cold_reboot": "reboot_method",
        "cold_power_cycle": "reboot_method",
        # Serial capture variants
        "serial": "serial_capture",
        "serial_console_capture": "serial_capture",
        # Delay variants
        "reboot_delay_seconds": "power_off_delay",
        "delay_seconds": "power_off_delay",
    },
)
