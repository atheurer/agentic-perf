"""Tests for providers/rhel_kernel.py — KernelSpec, command builders, parsers."""

from __future__ import annotations

import pytest

from providers.rhel_kernel import (
    HOST_STATE_TRANSITIONS,
    HOST_STATES,
    TERMINAL_STATES,
    KernelSpec,
    classify_available,
    inventory_fingerprint,
    parse_inventory,
    parse_probe,
    validate_host_transition,
)


class TestKernelSpec:
    def test_valid_releases(self):
        s1 = KernelSpec.parse("5.14.0-503.14.1.el9_5.x86_64")
        assert s1.release == "5.14.0-503.14.1.el9_5.x86_64"
        assert s1.package == "kernel"
        assert s1.nevra == "kernel-5.14.0-503.14.1.el9_5.x86_64"
        assert s1.vmlinuz == "/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64"
        assert s1.initramfs == "/boot/initramfs-5.14.0-503.14.1.el9_5.x86_64.img"

        s2 = KernelSpec.parse("6.12.0-0.rc6.58.el10.x86_64")
        assert s2.release == "6.12.0-0.rc6.58.el10.x86_64"

    def test_dict_input(self):
        s = KernelSpec.parse(
            {
                "release": "5.14.0-503.16.1.el9_5.x86_64",
                "package": "kernel-rt",
            }
        )
        assert s.package == "kernel-rt"
        assert s.nevra == "kernel-rt-5.14.0-503.16.1.el9_5.x86_64"

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="does not match"):
            KernelSpec.parse("5.14; rm -rf /")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            KernelSpec.parse("")

    def test_rejects_bare_name(self):
        with pytest.raises(ValueError, match="does not match"):
            KernelSpec.parse("kernel")

    def test_rejects_overlong(self):
        with pytest.raises(ValueError, match="exceeds"):
            KernelSpec.parse("5.14.0-" + "a" * 100)

    def test_rejects_missing_release_in_dict(self):
        with pytest.raises(ValueError, match="missing 'release'"):
            KernelSpec.parse({"package": "kernel"})

    def test_frozen(self):
        s = KernelSpec.parse("5.14.0-503.14.1.el9_5.x86_64")
        with pytest.raises(AttributeError):
            s.release = "changed"


INVENTORY_FIXTURE = """\
@@uname
5.14.0-503.14.1.el9_5.x86_64
@@arch
x86_64
@@cmdline
BOOT_IMAGE=/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64 root=/dev/sda1
@@boot_id
abc-123-def
@@os
NAME="Red Hat Enterprise Linux"
ID="rhel"
VERSION_ID="9.5"
@@rpm
kernel 5.14.0-503.14.1.el9_5.x86_64
kernel 5.14.0-503.16.1.el9_5.x86_64
kernel-core 5.14.0-503.14.1.el9_5.x86_64
@@default
/boot/vmlinuz-5.14.0-503.16.1.el9_5.x86_64
@@default_index
1
@@entries
index=0
kernel="/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64"
initrd="/boot/initramfs-5.14.0-503.14.1.el9_5.x86_64.img"
title="Red Hat Enterprise Linux (5.14.0-503.14.1.el9_5.x86_64) 9.5"
id="rhel-5.14.0-503.14.1"
index=1
kernel="/boot/vmlinuz-5.14.0-503.16.1.el9_5.x86_64"
initrd="/boot/initramfs-5.14.0-503.16.1.el9_5.x86_64.img"
title="Red Hat Enterprise Linux (5.14.0-503.16.1.el9_5.x86_64) 9.5"
id="rhel-5.14.0-503.16.1"
@@tuned
Current active profile: throughput-performance
"""


class TestParseInventory:
    def test_full_inventory(self):
        inv = parse_inventory(INVENTORY_FIXTURE)
        assert inv.running == "5.14.0-503.14.1.el9_5.x86_64"
        assert inv.arch == "x86_64"
        assert inv.boot_id == "abc-123-def"
        assert inv.os_id == "rhel"
        assert inv.os_version_id == "9.5"
        assert len(inv.installed) == 3
        assert "5.14.0-503.14.1.el9_5.x86_64" in inv.installed
        assert "5.14.0-503.16.1.el9_5.x86_64" in inv.installed
        assert inv.default_entry == "/boot/vmlinuz-5.14.0-503.16.1.el9_5.x86_64"
        assert inv.default_index == 1
        assert len(inv.entries) == 2
        assert inv.entries[0].index == 0
        assert inv.entries[1].kernel == "/boot/vmlinuz-5.14.0-503.16.1.el9_5.x86_64"
        assert inv.tuned_profile == "throughput-performance"

    def test_not_installed(self):
        stdout = (
            "@@uname\n5.14.0-503.14.1.el9_5.x86_64\n"
            "@@arch\nx86_64\n"
            "@@cmdline\nBOOT_IMAGE=/boot/vmlinuz\n"
            "@@boot_id\nabc\n"
            "@@os\nID=rhel\nVERSION_ID=9.5\n"
            "@@rpm\npackage kernel is not installed\n"
            "@@default\n/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64\n"
            "@@default_index\n0\n"
            "@@entries\n"
            "@@tuned\nNo current active profile.\n"
        )
        inv = parse_inventory(stdout)
        assert inv.installed == []
        assert inv.tuned_profile == "No current active profile."

    def test_grubby_error(self):
        stdout = (
            "@@uname\n5.14.0-503.14.1.el9_5.x86_64\n"
            "@@arch\nx86_64\n"
            "@@cmdline\nBOOT_IMAGE=/boot/vmlinuz\n"
            "@@boot_id\nabc\n"
            "@@os\nID=rhel\nVERSION_ID=9.5\n"
            "@@rpm\nkernel 5.14.0-503.14.1.el9_5.x86_64\n"
            "@@default\ngrubby: error reading /boot/grub2/grubenv\n"
            "@@default_index\n-1\n"
            "@@entries\n"
            "@@tuned\nthroughput-performance\n"
        )
        inv = parse_inventory(stdout)
        assert "error" in inv.default_entry.lower()
        assert inv.default_index == -1

    def test_to_dict_round_trip(self):
        inv = parse_inventory(INVENTORY_FIXTURE)
        d = inv.to_dict()
        assert d["running"] == inv.running
        assert len(d["entries"]) == 2
        assert d["entries"][0]["index"] == 0


class TestParseProbe:
    def test_normal(self):
        stdout = (
            "@@boot_id\nabc-123\n"
            "@@uname\n5.14.0-503.14.1.el9_5.x86_64\n"
            "@@default\n/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64\n"
        )
        probe = parse_probe(stdout)
        assert probe.boot_id == "abc-123"
        assert probe.kernel == "5.14.0-503.14.1.el9_5.x86_64"
        assert probe.default_entry == "/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64"

    def test_missing_sections_raises(self):
        with pytest.raises(ValueError, match="missing required sections"):
            parse_probe("no markers here")


class TestClassifyAvailable:
    def test_available(self):
        assert classify_available(0, "kernel.x86_64  5.14.0") == "available"

    def test_not_found(self):
        assert (
            classify_available(1, "Error: No matching Packages to list") == "not_found"
        )

    def test_not_checked(self):
        assert classify_available(1, "some other error") == "not_checked"


class TestInventoryFingerprint:
    def test_stable(self):
        inv = parse_inventory(INVENTORY_FIXTURE)
        fp1 = inventory_fingerprint(inv)
        fp2 = inventory_fingerprint(inv)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_changes_on_kernel_change(self):
        inv1 = parse_inventory(INVENTORY_FIXTURE)
        fp1 = inventory_fingerprint(inv1)
        modified = INVENTORY_FIXTURE.replace(
            "@@uname\n5.14.0-503.14.1",
            "@@uname\n5.14.0-999.99.9",
        )
        inv2 = parse_inventory(modified)
        fp2 = inventory_fingerprint(inv2)
        assert fp1 != fp2


class TestHostStateTransitions:
    def test_all_states_covered(self):
        defined = set(HOST_STATE_TRANSITIONS.keys())
        for targets in HOST_STATE_TRANSITIONS.values():
            defined.update(targets)
        assert defined <= HOST_STATES

    def test_terminal_states_have_no_transitions(self):
        for state in TERMINAL_STATES:
            if state == "indeterminate":
                assert HOST_STATE_TRANSITIONS.get(state) == frozenset({"verified"})
            else:
                assert state not in HOST_STATE_TRANSITIONS or (
                    HOST_STATE_TRANSITIONS[state] == frozenset()
                )

    def test_valid_transition(self):
        validate_host_transition("pending", "rebooting")

    def test_invalid_transition_raises(self):
        with pytest.raises(ValueError, match="illegal"):
            validate_host_transition("verified", "rebooting")

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            validate_host_transition("nonexistent", "verified")
