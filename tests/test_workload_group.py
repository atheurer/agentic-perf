"""Tests for providers/workload_group.py."""

from __future__ import annotations

from providers.workload_group import check_workload_group


class TestCheckWorkloadGroup:
    def test_first_step_in_group_ok(self):
        plan = {
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "kernel-ab"},
                },
            ],
        }
        ok, reason = check_workload_group(plan, 0, "abc123")
        assert ok
        assert reason == ""

    def test_matching_fingerprint_ok(self):
        plan = {
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "kernel-ab"},
                    "results": {"runfile_fingerprint": "abc123"},
                },
                {
                    "id": 1,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "kernel-ab"},
                },
            ],
        }
        ok, reason = check_workload_group(plan, 1, "abc123")
        assert ok

    def test_different_fingerprint_fails(self):
        plan = {
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "kernel-ab"},
                    "results": {"runfile_fingerprint": "abc123"},
                },
                {
                    "id": 1,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "kernel-ab"},
                },
            ],
        }
        ok, reason = check_workload_group(plan, 1, "def456")
        assert not ok
        assert "workload_drift" in reason

    def test_no_group_always_ok(self):
        plan = {
            "steps": [
                {"id": 0, "agent_type": "benchmark", "params": {}},
            ],
        }
        ok, reason = check_workload_group(plan, 0, "anything")
        assert ok

    def test_different_groups_independent(self):
        plan = {
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "group-a"},
                    "results": {"runfile_fingerprint": "aaa"},
                },
                {
                    "id": 1,
                    "agent_type": "benchmark",
                    "params": {"workload_group": "group-b"},
                },
            ],
        }
        ok, reason = check_workload_group(plan, 1, "bbb")
        assert ok
