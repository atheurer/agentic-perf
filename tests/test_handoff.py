"""Tests for orchestrator handoff validation."""

from __future__ import annotations

from orchestrator.handoff import check_handoff


class TestResourceToProvisioning:
    """Validate awaiting_provision handoff (resource → provisioning)."""

    def test_sufficient_hosts(self):
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.2", "10.0.0.3"],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok

    def test_insufficient_hosts(self):
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok
        assert "Insufficient hosts" in reason

    def test_no_hosts_at_all(self):
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {},
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok
        assert "empty" in reason.lower()

    def test_single_host_single_role(self):
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller", "client"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok

    def test_controller_overlaps_target(self):
        """Controller IP same as only target — only 1 unique host for 2 roles."""
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok

    def test_no_targets_multi_role(self):
        """Controller exists but no separate targets for multi-role benchmark."""
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": [],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok

    def test_public_private_ip_same_host(self):
        """Controller public IP and target private IP are the same machine."""
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "18.191.189.21",
                    "targets": ["172.31.6.108"],
                },
                "resource_provider_metadata": {
                    "ip_mapping": {"18.191.189.21": "172.31.6.108"},
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok
        assert "Insufficient hosts" in reason or "same host" in reason

    def test_public_private_ip_different_hosts(self):
        """Controller and target have different private IPs — actually 2 hosts."""
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "3.1.1.1",
                    "targets": ["3.2.2.2", "3.3.3.3"],
                },
                "resource_provider_metadata": {
                    "ip_mapping": {
                        "3.1.1.1": "172.31.1.1",
                        "3.2.2.2": "172.31.2.2",
                        "3.3.3.3": "172.31.3.3",
                    },
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok

    def test_nested_provider_metadata_ip_mapping(self):
        """IP mapping nested under controller/endpoints sub-dicts."""
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
                "assigned_hardware_ips": {
                    "controller": "52.15.1.1",
                    "targets": ["172.31.6.108"],
                },
                "resource_provider_metadata": {
                    "controller": {
                        "ip_mapping": {"52.15.1.1": "172.31.6.108"},
                    },
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok

    def test_missing_custom_fields(self):
        """Ticket with no custom_fields — should still pass with defaults."""
        ticket = {"custom_fields": {}}
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok
        assert "empty" in reason.lower()


class TestProvisioningToBenchmark:
    """Validate executing_benchmark handoff (provisioning → benchmark)."""

    def test_provisioning_complete(self):
        ticket = {
            "custom_fields": {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
                "harness_name": "crucible",
            }
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert ok

    def test_provisioning_not_complete(self):
        ticket = {
            "custom_fields": {
                "provisioning_complete": False,
                "hosts_provisioned": [],
                "harness_name": "crucible",
            }
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok
        assert "not marked complete" in reason.lower()

    def test_provisioning_field_missing(self):
        ticket = {"custom_fields": {}}
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok


class TestBenchmarkToReview:
    """Validate awaiting_review handoff (benchmark → review)."""

    def test_benchmark_completed_with_run_id(self):
        ticket = {
            "custom_fields": {
                "run_id": "abc-123",
                "benchmark_status": "completed",
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok

    def test_no_run_id_not_completed(self):
        ticket = {
            "custom_fields": {
                "benchmark_status": "failed",
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok

    def test_run_id_present_even_if_status_missing(self):
        ticket = {
            "custom_fields": {
                "run_id": "abc-123",
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok


class TestNoCheckStatuses:
    """Statuses without handoff checks should always pass."""

    def test_triage_pending(self):
        ok, _ = check_handoff("triage_pending", {})
        assert ok

    def test_awaiting_hardware(self):
        ok, _ = check_handoff("awaiting_hardware", {})
        assert ok

    def test_awaiting_teardown(self):
        ok, _ = check_handoff("awaiting_teardown", {})
        assert ok


class TestEnrichedRequiredHosts:
    """required_hosts with hardware specs must still pass validation."""

    def test_enriched_hosts_pass_handoff(self):
        ticket = {
            "custom_fields": {
                "required_hosts": [
                    {"roles": ["controller"], "min_memory_gb": 16},
                    {"roles": ["client"], "nic_speed": 25, "os": "RHEL9"},
                    {"roles": ["server"], "nic_speed": 25, "os": "RHEL9"},
                ],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.2", "10.0.0.3"],
                },
            }
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok, reason
