from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

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

    def patch(self, path: str, json: dict):
        self.calls.append(("PATCH", path, json))
        return self._response(200, {})

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


def test_slash_reply_commands_emit_one_event_after_comment(monkeypatch, capsys):
    for message in ("/model gpt-test", "/extend-iterations 3", "/close"):
        client = _FakeClient([])
        monkeypatch.setattr(
            cli,
            "get_client",
            lambda _args, current=client: (current, "http://state-store"),
        )

        cli.cmd_reply(_args(message))

        paths = [call[1] for call in client.calls]
        comment_index = paths.index("/api/v1/tickets/PERF-TEST/comments")
        reply_indices = [
            index
            for index, path in enumerate(paths)
            if path == "/api/v1/tickets/PERF-TEST/user-reply"
        ]
        assert len(reply_indices) == 1, message
        assert comment_index < reply_indices[0], message
        assert all(
            reply_indices[0] < index
            for index, path in enumerate(paths)
            if path.endswith("/transition")
        ), message

    capsys.readouterr()


def test_noncommitting_slash_commands_do_not_emit_reply_event(monkeypatch, capsys):
    for message in ("/model", "/extend-iterations nope", "/abort", "/unknown"):
        client = _FakeClient([])
        monkeypatch.setattr(
            cli,
            "get_client",
            lambda _args, current=client: (current, "http://state-store"),
        )

        cli.cmd_reply(_args(message))

        assert not any(path.endswith("/user-reply") for _, path, _ in client.calls)
        assert not any(path.endswith("/comments") for _, path, _ in client.calls)

    capsys.readouterr()


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


def test_iteration_extension_commands_send_only_each_new_grant(monkeypatch, capsys):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    cli.cmd_reply(_args("/extend-iterations 30"))
    cli.cmd_reply(_args("/extend-iterations 20"))
    args = _args("Continue the next agent")
    args.max_iterations = 10
    cli.cmd_reply(args)

    grants = [
        body
        for method, path, body in client.calls
        if method == "POST" and path.endswith("/iteration-extensions")
    ]
    assert [grant["additional_iterations"] for grant in grants] == [30, 20, 10]
    assert len({grant["request_id"] for grant in grants}) == 3
    assert not [
        body
        for method, path, body in client.calls
        if method == "PATCH" and "max_iterations_override" in body.get("fields", {})
    ]
    capsys.readouterr()


def test_reply_zero_iterations_uses_pinned_unlimited_control(monkeypatch, capsys):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("Continue without the agent cap")
    args.max_iterations = 0
    cli.cmd_reply(args)

    grants = [
        body for _, path, body in client.calls if path.endswith("/iteration-extensions")
    ]
    assert len(grants) == 1
    assert grants[0]["additional_iterations"] == 0
    assert grants[0]["make_unlimited"] is True
    assert grants[0]["agent_type"] == "benchmark"
    capsys.readouterr()


def test_slash_iteration_extension_requires_positive_amount(monkeypatch, capsys):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    cli.cmd_reply(_args("/extend-iterations 0"))
    assert [method for method, _, _ in client.calls] == ["GET"]
    assert "positive" in capsys.readouterr().out


@pytest.mark.parametrize("additional", [30, 0])
def test_approval_reply_applies_allowance_before_resuming(
    monkeypatch, capsys, additional
):
    approval_id = "apr-" + "a" * 32
    client = _FakeClient([{"approval_request_id": approval_id, "status": "pending"}])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("approve")
    args.max_iterations = additional
    cli.cmd_reply(args)

    paths = [path for _, path, _ in client.calls]
    grant_index = paths.index("/api/v1/tickets/PERF-TEST/iteration-extensions")
    assert (
        paths.index(f"/api/v1/tickets/PERF-TEST/approvals/{approval_id}/resolve")
        < grant_index
    )
    assert grant_index < paths.index("/api/v1/tickets/PERF-TEST/transition")
    payload = client.calls[grant_index][2]
    assert payload["additional_iterations"] == additional
    assert payload["make_unlimited"] is (additional == 0)
    assert payload["agent_type"] == "benchmark"
    capsys.readouterr()


@pytest.mark.parametrize("additional", [20, 0])
def test_model_slash_command_applies_iteration_flag(monkeypatch, capsys, additional):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("/model gpt-test")
    args.max_iterations = additional
    cli.cmd_reply(args)

    grants = [
        body for _, path, body in client.calls if path.endswith("/iteration-extensions")
    ]
    assert len(grants) == 1
    assert grants[0]["additional_iterations"] == additional
    assert grants[0]["make_unlimited"] is (additional == 0)
    assert grants[0]["agent_type"] == "benchmark"
    capsys.readouterr()


@pytest.mark.parametrize(
    "message", ["/unknown", "/model", "/extend-iterations nope", "/abort", "/close"]
)
def test_noncontinuing_commands_do_not_apply_iteration_flags(
    monkeypatch, capsys, message
):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args(message)
    args.max_iterations = 30
    cli.cmd_reply(args)
    assert not [
        path for _, path, _ in client.calls if path.endswith("/iteration-extensions")
    ]
    capsys.readouterr()


def test_ambiguous_approval_does_not_apply_iteration_flag(monkeypatch, capsys):
    client = _FakeClient(
        [
            {"approval_request_id": "apr-" + "a" * 32, "status": "pending"},
            {"approval_request_id": "apr-" + "b" * 32, "status": "pending"},
        ]
    )
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("approve")
    args.max_iterations = 30
    cli.cmd_reply(args)
    assert all(method == "GET" for method, _, _ in client.calls)
    capsys.readouterr()


@pytest.mark.parametrize("additional", [-1, True, "30"])
def test_invalid_iteration_flag_has_no_side_effects(monkeypatch, additional):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("approve")
    args.max_iterations = additional
    with pytest.raises(ValueError, match="non-negative integer"):
        cli.cmd_reply(args)
    assert client.calls == []


def test_conflicting_iteration_grant_flags_have_no_side_effects(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("/extend-iterations 30")
    args.max_iterations = 20
    with pytest.raises(ValueError, match="not both"):
        cli.cmd_reply(args)
    assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("additional", [30, 0])
def test_cli_allowance_targets_original_agent_after_phase_change(
    monkeypatch, capsys, additional
):
    class AdvancingClient(_FakeClient):
        def get(self, path):
            response = super().get(path)
            self.previous_status = "awaiting_review"
            return response

    client = AdvancingClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("Continue the benchmark")
    args.max_iterations = additional
    cli.cmd_reply(args)

    payload = next(
        body for _, path, body in client.calls if path.endswith("/iteration-extensions")
    )
    assert client.previous_status == "awaiting_review"
    assert payload["agent_type"] == "benchmark"
    assert payload["make_unlimited"] is (additional == 0)
    capsys.readouterr()


def test_abort_flag_does_not_spend_iteration_allowance(monkeypatch, capsys):
    client = _FakeClient([])
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://state-store"))
    args = _args("/extend-iterations 30")
    args.max_iterations = 20
    args.abort = True
    cli.cmd_reply(args)
    assert any(path.endswith("/abort") for _, path, _ in client.calls)
    assert not any(
        path.endswith("/iteration-extensions") for _, path, _ in client.calls
    )
    capsys.readouterr()
