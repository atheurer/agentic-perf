"""Benchmark progress heartbeats.

A long harness subprocess emits no events, so the orchestrator's stale-task
detector needs periodic trace activity from the benchmark server.  These
tests cover the progress parser, the heartbeat payload, and its failure
tolerance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import providers.skills.boot_time  # noqa: F401  # registers the progress parser
from providers.skills.base import get_progress_parser
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


class TestBootTimeProgressParser:
    def test_reports_latest_sample(self) -> None:
        parser = get_progress_parser("boot-time")
        assert parser is not None
        assert parser([b"Sample 1 at x\nSample 2 at y\n"]) == {"samples_collected": 2}

    def test_handles_sample_line_split_across_chunks(self) -> None:
        parser = get_progress_parser("boot-time")
        assert parser is not None
        assert parser([b"Sample 9 ", b"at split\n"]) == {"samples_collected": 9}

    def test_returns_empty_without_matches(self) -> None:
        parser = get_progress_parser("boot-time")
        assert parser is not None
        assert parser([b"starting up\n"]) == {}
        assert parser([]) == {}

    def test_unknown_harness_has_no_parser(self) -> None:
        assert get_progress_parser("no-such-harness") is None

    def test_heartbeat_is_faster_than_the_stall_window(self) -> None:
        from agents.benchmark import server

        assert (
            server._PROGRESS_HEARTBEAT_INTERVAL_S < server._PROGRESS_COMMENT_INTERVAL_S
        )


@pytest.mark.asyncio
async def test_heartbeat_publishes_progress_and_comments_when_due() -> None:
    from agents.benchmark import server

    trace = MagicMock()
    comment = AsyncMock()
    with (
        patch.object(server, "_publish_progress_trace", trace),
        patch.object(server, "_post_progress_comment", comment),
    ):
        attributes = await server._publish_progress_heartbeat(
            "PERF-HB",
            [b"Sample 1 at a\nSample 2 at b\n"],
            harness="boot-time",
            elapsed_seconds=600,
            samples_requested=50,
            post_comment=True,
        )

    assert attributes["samples_collected"] == 2
    assert attributes["samples_requested"] == 50
    assert attributes["elapsed_seconds"] == 600
    trace.assert_called_once()
    assert trace.call_args.args[0]["samples_collected"] == 2
    comment.assert_awaited_once()
    body = comment.await_args.args[1]
    assert "2/50 samples" in body
    assert "10 min" in body


@pytest.mark.asyncio
async def test_heartbeat_skips_comment_when_not_due() -> None:
    from agents.benchmark import server

    trace = MagicMock()
    comment = AsyncMock()
    with (
        patch.object(server, "_publish_progress_trace", trace),
        patch.object(server, "_post_progress_comment", comment),
    ):
        await server._publish_progress_heartbeat(
            "PERF-HB",
            [b"Sample 1 at a\n"],
            harness="boot-time",
            elapsed_seconds=300,
            samples_requested=10,
            post_comment=False,
        )

    trace.assert_called_once()
    comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_heartbeat_failures_are_not_fatal() -> None:
    from agents.benchmark import server

    with (
        patch.object(
            server,
            "_publish_progress_trace",
            side_effect=RuntimeError("trace store down"),
        ),
        patch.object(
            server,
            "_post_progress_comment",
            AsyncMock(side_effect=RuntimeError("store unreachable")),
        ),
    ):
        attributes = await server._publish_progress_heartbeat(
            "PERF-HB",
            [b"Sample 4 at a\n"],
            harness="boot-time",
            elapsed_seconds=900,
            samples_requested=10,
            post_comment=True,
        )

    assert attributes["samples_collected"] == 4


@pytest.mark.asyncio
async def test_heartbeat_without_ticket_only_builds_attributes() -> None:
    from agents.benchmark import server

    trace = MagicMock()
    with patch.object(server, "_publish_progress_trace", trace):
        attributes = await server._publish_progress_heartbeat(
            "",
            [b"Sample 3 at a\n"],
            harness="boot-time",
            elapsed_seconds=300,
            samples_requested=10,
            post_comment=True,
        )

    assert attributes["samples_collected"] == 3
    trace.assert_not_called()


def test_progress_trace_uses_subprocess_action_and_flushes() -> None:
    from agents.benchmark import server

    context = new_trace_context(ticket_id="PERF-HB", agent_id="benchmark-agent")
    token = bind_trace_context(context)
    client = MagicMock()
    try:
        with (
            patch.dict(
                "os.environ",
                {"AGENTIC_PERF_API_TOKEN": "token", "STATE_STORE_URL": "http://store"},
            ),
            patch(
                "providers.tracing.client.TraceClient", return_value=client
            ) as client_class,
        ):
            server._publish_progress_trace({"samples_collected": 7})
    finally:
        reset_trace_context(token)

    client_class.assert_called_once_with("http://store", "token")
    event = client.record.call_args.args[0]
    assert event.ticket_id == "PERF-HB"
    assert event.action.type.value == "subprocess"
    assert event.action.phase == "progress"
    assert event.action.target == "benchmark_progress"
    assert event.lifecycle.state.value == "started"
    assert event.attributes["samples_collected"] == 7
    client.flush.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_harness_kill_reason_is_patched_to_the_ticket() -> None:
    from agents.benchmark import server

    reason = {
        "code": "harness_timeout",
        "timeout_seconds": 5400,
        "mechanism": "benchmark_harness",
    }
    client = MagicMock()
    client.patch = AsyncMock(return_value=MagicMock(status_code=200))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "providers.execution.AuditedAsyncHTTPClient", return_value=client
    ) as client_class:
        await server._record_subprocess_kill_reason("PERF-HB", reason)

    client_class.assert_called_once()
    assert "headers" in client_class.call_args.kwargs
    client.patch.assert_awaited_once()
    url = client.patch.await_args.args[0]
    fields = client.patch.await_args.kwargs["json"]["fields"]
    assert url.endswith("/api/v1/tickets/PERF-HB/fields")
    assert fields == {"last_subprocess_kill_reason": reason}


@pytest.mark.asyncio
async def test_harness_kill_reason_skips_writes_without_a_ticket() -> None:
    from agents.benchmark import server

    with patch("providers.execution.AuditedAsyncHTTPClient") as client_class:
        await server._record_subprocess_kill_reason("", {"code": "harness_stall"})

    client_class.assert_not_called()


@pytest.mark.asyncio
async def test_new_output_dir_clears_previous_kill_reason() -> None:
    from agents.benchmark import server

    client = MagicMock()
    client.get = AsyncMock(
        return_value=MagicMock(
            json=MagicMock(
                return_value={
                    "custom_fields": {
                        "output_dirs": ["/old"],
                        "last_subprocess_kill_reason": {"code": "harness_stall"},
                    }
                }
            )
        )
    )
    client.patch = AsyncMock(return_value=MagicMock(status_code=200))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("providers.execution.AuditedAsyncHTTPClient", return_value=client):
        await server._append_output_dir("PERF-HB", "/new")

    fields = client.patch.await_args.kwargs["json"]["fields"]
    assert fields["output_dir"] == "/new"
    assert fields["output_dirs"] == ["/old", "/new"]
    assert fields["last_subprocess_kill_reason"] is None


@pytest.mark.asyncio
async def test_new_output_dir_keeps_absent_kill_reason_absent() -> None:
    from agents.benchmark import server

    client = MagicMock()
    client.get = AsyncMock(
        return_value=MagicMock(
            json=MagicMock(return_value={"custom_fields": {"output_dirs": []}})
        )
    )
    client.patch = AsyncMock(return_value=MagicMock(status_code=200))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("providers.execution.AuditedAsyncHTTPClient", return_value=client):
        await server._append_output_dir("PERF-HB", "/new")

    fields = client.patch.await_args.kwargs["json"]["fields"]
    assert "last_subprocess_kill_reason" not in fields
