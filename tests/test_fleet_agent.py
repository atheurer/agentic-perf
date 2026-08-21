"""Tests for the fleet coordinator agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

from agents.fleet.agent import FleetCoordinatorAgent


def _make_coordinator():
    """Create a coordinator with mocked HTTP client."""
    coord = FleetCoordinatorAgent(
        state_store_url="http://localhost:8090",
    )
    coord._client = AsyncMock()
    return coord


def _mock_ticket(
    *,
    platform_ready=True,
    platform_board="board-01",
    benchmark_status="completed",
    fleet_investigation=None,
    resource_reservation_id="lease-1",
    platform_ip="10.0.0.1",
    benchmark_kpis=None,
):
    """Build a mock ticket dict."""
    fleet = fleet_investigation or {
        "enabled": True,
        "tested_hosts": [],
    }
    cf = {
        "platform_ready": platform_ready,
        "platform_board": platform_board,
        "benchmark_status": benchmark_status,
        "fleet_investigation": fleet,
        "resource_reservation_id": resource_reservation_id,
        "platform_ip": platform_ip,
    }
    if benchmark_kpis:
        cf["benchmark_kpis"] = benchmark_kpis
    return {
        "id": "PERF-TEST",
        "status": "coordinating_fleet",
        "custom_fields": cf,
        "comments": [],
    }


class TestCoordinatorRecording:
    """Coordinator records per-host results correctly."""

    async def test_records_completed_host(self):
        coord = _make_coordinator()
        ticket = _mock_ticket(
            benchmark_status="completed",
            benchmark_kpis={"avg_boot_s": 17.8},
        )
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        # Check that update_fields was called with fleet data
        patch_calls = coord._client.patch.call_args_list
        fleet_update = None
        for call in patch_calls:
            body = call.kwargs.get("json") or (
                call.args[1] if len(call.args) > 1 else {}
            )
            fields = body.get("fields", {})
            if "fleet_investigation" in fields:
                fleet_update = fields["fleet_investigation"]
        assert fleet_update is not None
        tested = fleet_update["tested_hosts"]
        assert len(tested) == 1
        assert tested[0]["host_id"] == "board-01"
        assert tested[0]["status"] == "completed"

    async def test_records_partial_on_benchmark_failure(self):
        coord = _make_coordinator()
        ticket = _mock_ticket(benchmark_status="failed")
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        patch_calls = coord._client.patch.call_args_list
        for call in patch_calls:
            body = call.kwargs.get("json") or {}
            fields = body.get("fields", {})
            if "fleet_investigation" in fields:
                tested = fields["fleet_investigation"]["tested_hosts"]
                assert tested[0]["status"] == "partial"
                break

    async def test_records_partial_on_platform_failure(self):
        coord = _make_coordinator()
        ticket = _mock_ticket(
            platform_ready=False,
            benchmark_status=None,
        )
        # Add a diagnostic comment
        ticket["comments"] = [
            {
                "author": "platform-agent",
                "body": "**Platform Setup Failed**\n\n- **Diagnostics:** Flash timeout",
            }
        ]
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        patch_calls = coord._client.patch.call_args_list
        for call in patch_calls:
            body = call.kwargs.get("json") or {}
            fields = body.get("fields", {})
            if "fleet_investigation" in fields:
                tested = fields["fleet_investigation"]["tested_hosts"]
                assert tested[0]["status"] == "partial"
                assert "failure_reason" in tested[0]
                break


class TestCoordinatorRouting:
    """Coordinator routes correctly after recording."""

    async def test_routes_to_awaiting_hardware_after_benchmark(
        self,
    ):
        coord = _make_coordinator()
        ticket = _mock_ticket(benchmark_status="completed")
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        # Find the transition call
        post_calls = coord._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "awaiting_hardware"

    async def test_routes_to_evaluating_on_exhaustion(self):
        """When entered from resource exhaustion (board already
        tested, no new board provisioned), routes to evaluate."""
        coord = _make_coordinator()
        ticket = _mock_ticket(
            platform_ready=False,
            platform_board="board-01",
            benchmark_status=None,
            fleet_investigation={
                "enabled": True,
                "tested_hosts": [
                    {"host_id": "board-01", "status": "completed"},
                ],
            },
        )
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        # Should set fleet_exhausted and route to evaluate
        patch_calls = coord._client.patch.call_args_list
        exhaustion_set = False
        for call in patch_calls:
            body = call.kwargs.get("json") or {}
            fields = body.get("fields", {})
            fleet = fields.get("fleet_investigation", {})
            if fleet.get("fleet_exhausted"):
                exhaustion_set = True
        assert exhaustion_set

        post_calls = coord._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "evaluating_convergence"


class TestCoordinatorErrorHandling:
    """Coordinator handles errors gracefully."""

    async def test_error_routes_to_guidance(self):
        coord = _make_coordinator()
        coord._client.get = AsyncMock(side_effect=Exception("connection failed"))
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord.run("PERF-TEST")

        # Should transition to awaiting_customer_guidance
        post_calls = coord._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "awaiting_customer_guidance"


class TestCoordinatorExhaustionEdgeCases:
    """Edge cases in exhaustion detection."""

    async def test_no_board_assigned_is_exhaustion(self):
        """When no board was ever provisioned (resource failed
        on first try), coordinator declares exhaustion."""
        coord = _make_coordinator()
        ticket = _mock_ticket(
            platform_ready=False,
            platform_board="unknown",
            benchmark_status=None,
        )
        coord._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: ticket,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )
        coord._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                raise_for_status=AsyncMock(),
            )
        )

        await coord._coordinate("PERF-TEST")

        post_calls = coord._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "evaluating_convergence"
