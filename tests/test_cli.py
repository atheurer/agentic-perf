from __future__ import annotations

from types import SimpleNamespace

import httpx

import cli


class _FakeClient:
    def __init__(
        self,
        approvals: list[dict[str, str]],
        *,
        previous_status: str | None = "executing_benchmark",
    ):
        self.approvals = approvals
        self.previous_status = previous_status
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, {}))
        if path.endswith("/approvals"):
            return self._response(200, {"approvals": self.approvals})
        return self._response(
            200,
            {
                "status": "awaiting_customer_guidance",
                "previous_status": self.previous_status,
            },
        )

    def post(self, path: str, json: dict):
        self.calls.append(("POST", path, json))
        if path.endswith("/comments"):
            return self._response(200, {"id": "comment-1"})
        if path.endswith("/user-reply"):
            return self._response(200, {"status": "recorded"})
        return self._response(200, {"status": "approved"})

    @staticmethod
    def _response(status: int, payload: dict):
        return httpx.Response(
            status,
            json=payload,
            request=httpx.Request("GET", "http://state-store"),
        )


def _args(message: str):
    return SimpleNamespace(
        ticket_id="PERF-TEST",
        message=message,
        store_url="http://state-store",
        abort=False,
    )


def test_read_events_advances_to_max_cursor_for_display_page(monkeypatch):
    class FakeEventBus:
        def get_events(self, ticket_id, *, since, limit):
            assert ticket_id == "PERF-TEST"
            assert since == 1
            assert limit == 100_000
            return [{"seq": 3}, {"seq": 2}]

        def close(self):
            pass

    monkeypatch.setattr("providers.events.EventBus", FakeEventBus)

    events, last_seq = cli._read_events("PERF-TEST", 1)

    assert [event["seq"] for event in events] == [3, 2]
    assert last_seq == 3


def test_reply_resolves_pending_benchmark_approval(monkeypatch, capsys):
    approval_id = "apr-" + "a" * 32
    client = _FakeClient([{"approval_request_id": approval_id, "status": "pending"}])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))

    cli.cmd_reply(_args("approve"))

    assert [call[0:2] for call in client.calls] == [
        ("GET", "/api/v1/tickets/PERF-TEST"),
        ("GET", "/api/v1/tickets/PERF-TEST/approvals"),
        ("POST", "/api/v1/tickets/PERF-TEST/comments"),
        ("POST", "/api/v1/tickets/PERF-TEST/user-reply"),
        ("POST", f"/api/v1/tickets/PERF-TEST/approvals/{approval_id}/resolve"),
        ("POST", "/api/v1/tickets/PERF-TEST/transition"),
    ]
    assert "approved" in capsys.readouterr().out


def test_reply_records_event_without_previous_status(monkeypatch, capsys):
    client = _FakeClient([], previous_status=None)
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))

    cli.cmd_reply(_args("please continue"))

    assert [call[0:2] for call in client.calls] == [
        ("GET", "/api/v1/tickets/PERF-TEST"),
        ("POST", "/api/v1/tickets/PERF-TEST/comments"),
        ("POST", "/api/v1/tickets/PERF-TEST/user-reply"),
    ]
    assert "cannot resume automatically" in capsys.readouterr().out


def test_abort_reply_records_event_once(monkeypatch, capsys):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("stop")
    args.abort = True

    cli.cmd_reply(args)

    assert [call[0:2] for call in client.calls] == [
        ("GET", "/api/v1/tickets/PERF-TEST"),
        ("POST", "/api/v1/tickets/PERF-TEST/comments"),
        ("POST", "/api/v1/tickets/PERF-TEST/user-reply"),
        ("POST", "/api/v1/tickets/PERF-TEST/abort"),
    ]
    assert "ticket aborted" in capsys.readouterr().out


def test_reply_does_not_resume_with_ambiguous_approvals(monkeypatch, capsys):
    client = _FakeClient(
        [
            {"approval_request_id": "apr-" + "a" * 32, "status": "pending"},
            {"approval_request_id": "apr-" + "b" * 32, "status": "pending"},
        ]
    )
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))

    cli.cmd_reply(_args("approve"))

    assert all(call[0] == "GET" for call in client.calls)
    assert "Multiple benchmark approvals" in capsys.readouterr().err
