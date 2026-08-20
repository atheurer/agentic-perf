"""Tests for fleet investigation tracking."""

from __future__ import annotations

from providers.fleet import (
    build_tested_host_entry,
    get_fleet_progress,
    get_tested_host_ids,
    is_fleet_investigation,
)


class TestIsFleetInvestigation:
    def test_enabled(self):
        cf = {"fleet_investigation": {"enabled": True}}
        assert is_fleet_investigation(cf) is True

    def test_not_enabled(self):
        cf = {"fleet_investigation": {"enabled": False}}
        assert is_fleet_investigation(cf) is False

    def test_missing(self):
        assert is_fleet_investigation({}) is False

    def test_no_fleet_key(self):
        cf = {"harness": "boot-time"}
        assert is_fleet_investigation(cf) is False


class TestGetTestedHostIds:
    def test_returns_ids(self):
        cf = {
            "fleet_investigation": {
                "tested_hosts": [
                    {"host_id": "board-01", "status": "completed"},
                    {"host_id": "board-02", "status": "partial"},
                ],
            },
        }
        assert get_tested_host_ids(cf) == [
            "board-01",
            "board-02",
        ]

    def test_skips_missing_host_id(self):
        cf = {
            "fleet_investigation": {
                "tested_hosts": [
                    {"host_id": "board-01"},
                    {"status": "partial"},  # no host_id
                ],
            },
        }
        assert get_tested_host_ids(cf) == ["board-01"]

    def test_empty(self):
        assert get_tested_host_ids({}) == []


class TestGetFleetProgress:
    def test_hard_exhaustion(self):
        cf = {
            "fleet_investigation": {
                "fleet_exhausted": {"hard": True},
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                    {"host_id": "b", "status": "partial"},
                    {"host_id": "c", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 3
        assert p["completed"] == 2
        assert p["partial"] == 1
        assert p["fleet_exhausted"] is True
        assert p["exhaustion_type"] == "hard"
        assert p["converged"] is True

    def test_soft_exhaustion(self):
        cf = {
            "fleet_investigation": {
                "fleet_exhausted": {
                    "soft": True,
                    "unavailable_hosts": [
                        "board-04",
                        "board-05",
                    ],
                },
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                    {"host_id": "b", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 2
        assert p["fleet_exhausted"] is True
        assert p["exhaustion_type"] == "soft"
        assert p["unavailable_hosts"] == [
            "board-04",
            "board-05",
        ]
        assert p["converged"] is True

    def test_not_exhausted(self):
        cf = {
            "fleet_investigation": {
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 1
        assert p["fleet_exhausted"] is False
        assert p["exhaustion_type"] is None
        assert p["converged"] is False

    def test_empty(self):
        p = get_fleet_progress({})
        assert p["converged"] is False
        assert p["fleet_exhausted"] is False
        assert p["tested"] == 0

    def test_exhausted_but_no_hosts_not_converged(self):
        """Edge: exhaustion flag set but no hosts tested."""
        cf = {
            "fleet_investigation": {
                "fleet_exhausted": {"hard": True},
                "tested_hosts": [],
            },
        }
        p = get_fleet_progress(cf)
        assert p["fleet_exhausted"] is True
        assert p["converged"] is False  # need at least 1 host


class TestBuildTestedHostEntry:
    def test_basic(self):
        entry = build_tested_host_entry(
            host_id="board-01",
            lease_id="perf-xxx",
            ip="10.0.0.1",
        )
        assert entry["host_id"] == "board-01"
        assert entry["status"] == "completed"
        assert "failure_reason" not in entry
        assert "metrics" not in entry

    def test_with_failure(self):
        entry = build_tested_host_entry(
            host_id="board-02",
            status="partial",
            failure_reason="SUT unreachable after 3 reboots",
            metrics={"avg_total_boot_s": 28.1},
        )
        assert entry["status"] == "partial"
        assert entry["failure_reason"] == "SUT unreachable after 3 reboots"
        assert entry["metrics"]["avg_total_boot_s"] == 28.1

    def test_defaults(self):
        entry = build_tested_host_entry(host_id="board-03")
        assert entry["status"] == "completed"
        assert entry["lease_id"] == ""
        assert entry["ip"] == ""
        assert "metrics" not in entry


class TestStateMachineFleetTransitions:
    """Fleet coordinator state machine transitions."""

    def test_benchmark_to_coordinating_fleet(self):
        from state_store.models import VALID_TRANSITIONS, TicketStatus

        allowed = VALID_TRANSITIONS[TicketStatus.EXECUTING_BENCHMARK]
        assert TicketStatus.COORDINATING_FLEET in allowed

    def test_platform_to_coordinating_fleet(self):
        from state_store.models import VALID_TRANSITIONS, TicketStatus

        allowed = VALID_TRANSITIONS[TicketStatus.PREPARING_PLATFORM]
        assert TicketStatus.COORDINATING_FLEET in allowed

    def test_fleet_to_awaiting_hardware(self):
        from state_store.models import VALID_TRANSITIONS, TicketStatus

        allowed = VALID_TRANSITIONS[TicketStatus.COORDINATING_FLEET]
        assert TicketStatus.AWAITING_HARDWARE in allowed

    def test_fleet_to_evaluating_convergence(self):
        from state_store.models import VALID_TRANSITIONS, TicketStatus

        allowed = VALID_TRANSITIONS[TicketStatus.COORDINATING_FLEET]
        assert TicketStatus.EVALUATING_CONVERGENCE in allowed

    def test_fleet_to_guidance(self):
        from state_store.models import VALID_TRANSITIONS, TicketStatus

        allowed = VALID_TRANSITIONS[TicketStatus.COORDINATING_FLEET]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed
