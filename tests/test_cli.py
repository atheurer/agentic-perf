from __future__ import annotations

from types import SimpleNamespace

import httpx

import cli


class _FakeClient:
    def __init__(self, approvals: list[dict[str, str]]):
        self.approvals = approvals
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, {}))
        if path.endswith("/approvals"):
            return self._response(200, {"approvals": self.approvals})
        return self._response(
            200,
            {
                "status": "awaiting_customer_guidance",
                "previous_status": "executing_benchmark",
            },
        )

    def post(self, path: str, json: dict):
        self.calls.append(("POST", path, json))
        if path.endswith("/comments"):
            return self._response(200, {"id": "comment-1"})
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


def test_reply_resolves_pending_benchmark_approval(monkeypatch, capsys):
    approval_id = "apr-" + "a" * 32
    client = _FakeClient([{"approval_request_id": approval_id, "status": "pending"}])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))

    cli.cmd_reply(_args("approve"))

    assert [call[0:2] for call in client.calls] == [
        ("GET", "/api/v1/tickets/PERF-TEST"),
        ("GET", "/api/v1/tickets/PERF-TEST/approvals"),
        ("POST", "/api/v1/tickets/PERF-TEST/comments"),
        ("POST", f"/api/v1/tickets/PERF-TEST/approvals/{approval_id}/resolve"),
    ]
    assert "approved" in capsys.readouterr().out


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
