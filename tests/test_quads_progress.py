from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.quads import QuadsClient
from providers.redaction import get_shared_redactor
from providers.resource.quads import QuadsResourceProvider
from providers.resource.quads_progress import QuadsProgressReporter


@pytest.fixture
def progress(monkeypatch):
    posted = AsyncMock()
    monkeypatch.setattr("providers.resource.quads_progress.tool_progress", posted)
    yield posted
    get_shared_redactor().deregister_ticket("PERF-QUADS-PROGRESS")


def make_reporter() -> tuple[QuadsProgressReporter, AsyncMock]:
    client = AsyncMock()
    client.password = "quads-password-secret"
    client.default_root_password = "root-password-secret"
    client.get_move_progress.return_value = [
        {
            "host": "host-a.example.com",
            "status": "validation",
            "message": "Running validation checks",
            "error_message": "Switch configuration failed",
            "started_at": "2026-11-03T17:36:00Z",
            "completed_at": "2026-11-03T17:37:00Z",
        },
        {"host": "other.example.com", "error_message": "unrelated allocation"},
    ]
    client.get_host_progress.return_value = {
        "validated": False,
        "build": True,
        "switch_config_applied": False,
    }
    return (
        QuadsProgressReporter(
            client, "PERF-QUADS-PROGRESS", 733, "cloud06", ["host-a.example.com"]
        ),
        client,
    )


async def test_ticket_progress_includes_assignment_and_host_evidence(progress):
    reporter, _ = make_reporter()
    await reporter.assignment_created()
    await reporter({"validated": False, "provisioned": True}, 120, "waiting")

    assert "assignment 733 (cloud06) created" in progress.await_args_list[0].args[0]
    message = progress.await_args_list[1].args[0]
    assert "provisioned=true, validated=false" in message
    assert "host-a.example.com" in message
    assert "move=validation" in message
    assert "last reported error=Switch configuration failed" in message
    assert "switch configured=false" in message
    assert "record end=2026-11-03T17:37:00Z" in message
    assert "may be stale" in message
    assert "other.example.com" not in message
    assert progress.await_args_list[1].kwargs["ticket_id"] == "PERF-QUADS-PROGRESS"


async def test_progress_deduplicates_and_heartbeats_and_reports_changes(progress):
    reporter, client = make_reporter()
    status = {"validated": False, "provisioned": True}
    await reporter(status, 0, "waiting")
    await reporter(status, 120, "waiting")
    assert progress.await_count == 1
    await reporter(status, 600, "waiting")
    assert progress.await_count == 2
    client.get_host_progress.return_value["switch_config_applied"] = True
    await reporter(status, 720, "waiting")
    assert progress.await_count == 3
    assert "switch configured=true" in progress.await_args.args[0]
    await reporter(status, 840, "timed_out")
    assert "reconcile it before retrying" in progress.await_args.args[0]


async def test_unavailable_details_are_reported_without_failing(progress):
    reporter, client = make_reporter()
    client.get_move_progress.side_effect = RuntimeError("sensitive details")
    client.get_host_progress.side_effect = RuntimeError("sensitive details")
    await reporter({}, 0, "waiting")
    message = progress.await_args.args[0]
    assert "provisioned=unknown, validated=unknown" in message
    assert "QUADS move status is unavailable" in message
    assert "host status unavailable" in message
    assert "sensitive details" not in message


async def test_slow_detail_query_does_not_hide_other_host_status(progress, monkeypatch):
    reporter, client = make_reporter()

    async def slow_move_query(cloud):
        await asyncio.sleep(60)

    client.get_move_progress.side_effect = slow_move_query
    monkeypatch.setattr(
        "providers.resource.quads_progress._DETAIL_TIMEOUT_SECONDS", 0.02
    )
    await reporter({"validated": False}, 0, "waiting")
    message = progress.await_args.args[0]
    assert "QUADS move status is unavailable" in message
    assert "switch configured=false" in message


async def test_api_error_text_is_redacted_before_ticket_comment(progress):
    reporter, client = make_reporter()
    client.get_move_progress.return_value[0]["error_message"] = (
        "Switch failed; quads-password-secret root-password-secret"
    )
    await reporter({"validated": False}, 0, "waiting")
    message = progress.await_args.args[0]
    assert "quads-password-secret" not in message
    assert "root-password-secret" not in message
    assert "[REDACTED:" in message


async def test_redacts_before_truncating_api_text(progress):
    reporter, client = make_reporter()
    secret = "secret-" + "x" * 250
    get_shared_redactor().register("PERF-QUADS-PROGRESS", "long-secret", secret)
    client.get_move_progress.return_value[0]["error_message"] = "failure: " + secret
    await reporter({"validated": False}, 0, "waiting")
    message = progress.await_args.args[0]
    assert secret[:100] not in message
    assert "[REDACTED:" in message


async def test_excessive_api_text_is_omitted(progress):
    reporter, client = make_reporter()
    client.get_move_progress.return_value[0]["error_message"] = "x" * 2049
    await reporter({"validated": False}, 0, "waiting")
    assert "[QUADS text omitted: too long]" in progress.await_args.args[0]


async def test_poll_reports_waiting_and_validated(monkeypatch):
    client = QuadsClient.__new__(QuadsClient)
    client.get_assignment_status = AsyncMock(
        side_effect=[{"validated": False}, {"validated": True}]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("providers.quads.asyncio.sleep", sleep)
    callback = AsyncMock()
    status = await client.poll_until_validated(
        733, interval=120, progress_callback=callback
    )
    assert status == {"validated": True}
    assert [call.args[1:] for call in callback.await_args_list] == [
        (0, "waiting"),
        (120, "validated"),
    ]


async def test_poll_reports_timeout_and_progress_failure_does_not_abort(monkeypatch):
    client = QuadsClient.__new__(QuadsClient)
    client.get_assignment_status = AsyncMock(return_value={"validated": False})
    monkeypatch.setattr("providers.quads.asyncio.sleep", AsyncMock())
    callback = AsyncMock(side_effect=RuntimeError("progress unavailable"))
    with pytest.raises(TimeoutError, match="assignment 733 not validated"):
        await client.poll_until_validated(
            733, interval=120, timeout=240, progress_callback=callback
        )
    assert [call.args[1:] for call in callback.await_args_list] == [
        (0, "waiting"),
        (120, "waiting"),
        (240, "timed_out"),
    ]


async def test_poll_preserves_cancellation():
    client = QuadsClient.__new__(QuadsClient)
    client.get_assignment_status = AsyncMock(return_value={"validated": False})
    callback = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await client.poll_until_validated(733, progress_callback=callback)


async def test_reservation_reports_assignment_and_poll_results(progress):
    reporter, client = make_reporter()
    client.create_assignment.return_value = {"id": 733, "cloud_name": "cloud06"}
    client.schedule_host.return_value = {"end": "2026-11-04"}
    client.ssh_key_path = "/tmp/existing-key"
    client.setup_ssh.return_value = {"status": "success"}

    async def poll(assignment_id, *, progress_callback):
        assert assignment_id == 733
        await progress_callback({"validated": False, "provisioned": True}, 0, "waiting")
        await progress_callback(
            {"validated": True, "provisioned": True}, 120, "validated"
        )
        return {"validated": True}

    client.poll_until_validated.side_effect = poll
    provider = QuadsResourceProvider(client)
    result = await provider.reserve(
        {"hostnames": reporter.hostnames},
        "test benchmark",
        ticket_id="PERF-QUADS-PROGRESS",
    )
    assert result["status"] == "success"
    assert len(progress.await_args_list) == 3
    assert "assignment 733" in progress.await_args_list[0].args[0]
    assert "Switch configuration failed" in progress.await_args_list[1].args[0]
    assert "validated after 120s" in progress.await_args_list[2].args[0]


async def test_move_and_host_queries_use_read_only_quads_endpoints():
    client = QuadsClient.__new__(QuadsClient)
    client._base_url = "https://quads.example.com"
    move = MagicMock()
    move.json.return_value = [{"host": "host-a.example.com", "status": "failed"}]
    host = MagicMock()
    host.json.return_value = {"switch_config_applied": False}
    client._client = MagicMock()
    client._client.get = AsyncMock(side_effect=[move, host])
    assert (await client.get_move_progress("cloud06"))[0]["status"] == "failed"
    assert (await client.get_host_progress("host-a.example.com"))[
        "switch_config_applied"
    ] is False
    assert client._client.get.await_args_list[0].kwargs["params"] == {
        "cloud": "cloud06"
    }
    assert (
        client._client.get.await_args_list[1]
        .args[0]
        .endswith("/api/v3/hosts/host-a.example.com")
    )


async def test_missing_validation_remains_unknown_but_not_ready():
    client = AsyncMock()
    client.get_assignment_status.return_value = {"validated": None}
    result = await QuadsResourceProvider(client).get_reservation_status("733")
    assert result["ready"] is False
    assert result["details"]["validated"] is None
