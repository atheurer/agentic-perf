"""Boot-time harness directive schema.

Boot-time is a standalone harness without a full skill provider.
It registers its recognized directives and user-facing aliases
so the normalization framework can map variant key names to
canonical forms.
"""

from __future__ import annotations

from providers.directives import register_directives

register_directives(
    recognized={
        "sample_count",
        "reboot_method",
        "power_off_delay",
        "serial_capture",
        "kpi_pattern",
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
