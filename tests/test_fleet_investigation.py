"""Tests for fleet investigation tracking."""

from __future__ import annotations

import json

import pytest

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


class TestResourceAgentFleetExhaustion:
    """Resource agent routes to fleet coordinator on exhaustion (#994)."""

    async def test_request_human_input_catches_fleet_exhaustion(self):
        """_request_human_input triggers fleet exhaustion check."""
        from unittest.mock import AsyncMock

        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent.__new__(ResourceAgent)
        agent.agent_name = "resource-agent"
        agent._ticket_id = "PERF-FLEET"
        agent._mode = "acquire"
        agent._get_ticket = AsyncMock(
            return_value={
                "id": "PERF-FLEET",
                "custom_fields": {
                    "fleet_investigation": {
                        "enabled": True,
                        "tested_hosts": [
                            {"host_id": "board-01"},
                        ],
                    },
                    "resource_fleet_exhaustion_detected": True,
                },
            }
        )
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()

        from agents.base import HITLDriftError

        with pytest.raises(HITLDriftError, match="Fleet"):
            await agent._request_human_input(
                "PERF-FLEET",
                "All boards excluded",
            )

        agent._transition_ticket.assert_called_once()
        call_args = agent._transition_ticket.call_args
        assert call_args.args[1] == "coordinating_fleet"

    async def test_fleet_without_confirmed_exhaustion_allows_guidance(self):
        from unittest.mock import AsyncMock, patch

        from agents.base import AgentBase
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent.__new__(ResourceAgent)
        agent._ticket_id = "PERF-FLEET"
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {"fleet_investigation": {"enabled": True}},
            }
        )

        with patch.object(
            AgentBase,
            "_request_human_input",
            new_callable=AsyncMock,
            return_value="user reply",
        ) as mock_hitl:
            result = await agent._request_human_input(
                "PERF-FLEET",
                "Need the image version before provisioning.",
            )

        assert result == "user reply"
        mock_hitl.assert_awaited_once()

    async def test_completion_cannot_bypass_confirmed_exhaustion(self):
        from unittest.mock import AsyncMock, MagicMock

        from agents.base import HITLDriftError
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent.__new__(ResourceAgent)
        agent._ticket_id = "PERF-FLEET"
        agent._get_submit_result = lambda _response: {
            "assigned_hardware_ips": {"targets": ["10.0.0.1"]}
        }
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {
                    "fleet_investigation": {"enabled": True},
                    "resource_fleet_exhaustion_detected": True,
                }
            }
        )
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._update_fields = AsyncMock()

        with pytest.raises(HITLDriftError, match="Fleet"):
            await agent._handle_completion("PERF-FLEET", MagicMock())

        agent._transition_ticket.assert_awaited_once()
        agent._update_fields.assert_not_awaited()


class TestResourceServerFleetExhaustion:
    def test_exhaustion_requires_every_matching_board_to_be_tested(self):
        from agents.resource.server import _is_confirmed_fleet_exhaustion

        result = {
            "available": False,
            "all_excluded": True,
            "excluded_hosts": ["board-01", "board-02"],
        }

        assert _is_confirmed_fleet_exhaustion(result, {"board-01", "board-02"})
        assert not _is_confirmed_fleet_exhaustion(result, {"board-01"})
        assert not _is_confirmed_fleet_exhaustion(result, set())

    async def test_persists_only_confirmed_exhaustion(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch

        import agents.resource.server as resource_server

        provider = MagicMock()
        provider.provider_name = "jumpstarter"
        provider.check_available = AsyncMock(
            side_effect=[
                {
                    "available": False,
                    "all_excluded": True,
                    "excluded_hosts": ["board-01"],
                    "selector": "board_type=s32g",
                },
                {"available": True, "devices": [{"name": "board-02"}]},
            ]
        )
        registry = MagicMock()
        registry.get_provider = AsyncMock(return_value=provider)
        monkeypatch.setattr(resource_server, "_ensure_init", AsyncMock())
        monkeypatch.setattr(resource_server, "_registry", registry)
        monkeypatch.setattr(
            resource_server,
            "_ticket",
            {
                "id": "PERF-FLEET",
                "custom_fields": {
                    "fleet_investigation": {
                        "enabled": True,
                        "tested_hosts": [{"host_id": "board-01"}],
                    }
                },
            },
        )
        monkeypatch.setenv("TICKET_ID", "PERF-FLEET")

        ticket_response = MagicMock(status_code=200)
        ticket_response.json.return_value = {
            "custom_fields": {
                "fleet_investigation": {
                    "enabled": True,
                    "tested_hosts": [{"host_id": "board-01"}],
                }
            }
        }
        update_response = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=ticket_response)
        http_client.patch = AsyncMock(return_value=update_response)
        http_context = AsyncMock()
        http_context.__aenter__.return_value = http_client
        http_context.__aexit__.return_value = False

        with (
            patch(
                "providers.execution.AuditedAsyncHTTPClient",
                return_value=http_context,
            ),
            patch("state_store.auth.read_token_from_file", return_value="token"),
        ):
            exhausted = json.loads(
                await resource_server.check_available_resources(
                    provider="jumpstarter",
                    requirements={"jumpstarter_selector": "board_type=s32g"},
                )
            )
            available = json.loads(
                await resource_server.check_available_resources(
                    provider="jumpstarter",
                    requirements={"jumpstarter_selector": "board_type=s32g"},
                )
            )

        assert exhausted["fleet_exhausted"] is True
        assert "fleet_exhausted" not in available
        assert [
            call.kwargs["json"]["fields"]["resource_fleet_exhaustion_detected"]
            for call in http_client.patch.await_args_list
        ] == [True, False]

    async def test_non_fleet_falls_through(self):
        """Non-fleet tickets use normal HITL path."""
        from unittest.mock import AsyncMock, patch

        from agents.base import AgentBase
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent.__new__(ResourceAgent)
        agent.agent_name = "resource-agent"
        agent._ticket_id = "PERF-NORMAL"
        agent._mode = "acquire"
        agent._get_ticket = AsyncMock(
            return_value={
                "id": "PERF-NORMAL",
                "custom_fields": {},
            }
        )

        # Patch the grandparent _request_human_input
        with patch.object(
            AgentBase,
            "_request_human_input",
            new_callable=AsyncMock,
            return_value="user reply",
        ) as mock_hitl:
            result = await agent._request_human_input(
                "PERF-NORMAL",
                "Need help",
            )
            assert result == "user reply"
            mock_hitl.assert_called_once()


class TestSnapshotIterationData:
    """snapshot_iteration_data captures per-board state (#1035)."""

    def test_captures_benchmark_data(self):
        from providers.fleet import snapshot_iteration_data

        cf = {
            "run_id": "boot-time-abc123",
            "benchmark_status": "failed",
            "samples_collected": 2,
            "benchmark_kpis": {"avg_boot_s": 12.5},
        }
        snap = snapshot_iteration_data(cf)
        assert snap["run_id"] == "boot-time-abc123"
        assert snap["benchmark_status"] == "failed"
        assert snap["samples_collected"] == 2
        assert snap["benchmark_kpis"]["avg_boot_s"] == 12.5

    def test_captures_platform_data(self):
        from providers.fleet import snapshot_iteration_data

        cf = {
            "platform_ip": "10.26.29.22",
            "platform_flash_duration_s": 98.5,
            "platform_boot_duration_s": 31.2,
            "platform_serial_log": "/tmp/serial-capture.log",
            "output_dir": "/tmp/artifacts/run-123",
            "jumpstarter_flash": {"diagnostics": ["Flash succeeded in 98s"]},
        }
        snap = snapshot_iteration_data(cf)
        assert snap["platform_ip"] == "10.26.29.22"
        assert snap["flash_duration_s"] == 98.5
        assert snap["boot_duration_s"] == 31.2
        assert snap["flash_diagnostics"] == ["Flash succeeded in 98s"]
        assert snap["serial_log_path"] == "/tmp/serial-capture.log"
        assert snap["output_dir"] == "/tmp/artifacts/run-123"

    def test_empty_fields_omitted(self):
        from providers.fleet import snapshot_iteration_data

        snap = snapshot_iteration_data({})
        assert snap == {}

    def test_build_entry_includes_iteration_data(self):
        from providers.fleet import build_tested_host_entry

        entry = build_tested_host_entry(
            host_id="board-01",
            status="completed",
            iteration_data={"run_id": "boot-time-xyz"},
        )
        assert entry["iteration_data"]["run_id"] == "boot-time-xyz"

    def test_build_entry_omits_iteration_data_when_none(self):
        from providers.fleet import build_tested_host_entry

        entry = build_tested_host_entry(host_id="board-01")
        assert "iteration_data" not in entry
