from __future__ import annotations

import json
from types import SimpleNamespace

import cli


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = body
        self.text = json.dumps(body)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._body


class _Client:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body
        self.params: dict[str, object] | None = None

    def get(self, _endpoint: str, *, params: dict[str, object]) -> _Response:
        self.params = params
        return _Response(self.body)


def _args(**overrides: object) -> SimpleNamespace:
    values = {
        "store_url": "http://test",
        "ticket_id": "PERF-1",
        "ticket_id_option": None,
        "trace_id": None,
        "action_id": None,
        "invocation": None,
        "action_type": None,
        "outcome": None,
        "parent_action_id": None,
        "producer_component": None,
        "since": None,
        "until": None,
        "retry_kind": None,
        "idempotency_outcome": None,
        "lifecycle_state": None,
        "causal": False,
        "tree": False,
        "include_payloads": False,
        "limit": 1000,
        "cursor": None,
        "json": False,
        "jsonl": False,
        "export": False,
        "format": "json",
        "output": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _body() -> dict[str, object]:
    return {
        "events": [
            {
                "action_id": "a" * 16,
                "parent_action_id": None,
                "action": {"type": "dispatch", "target": "external-job"},
                "lifecycle": {
                    "state": "started",
                    "attempt": 2,
                    "retry_kind": "transport_replay",
                    "replay_of_action_id": "b" * 16,
                },
                "duration_ms": 12,
                "producer": {"process_start_id": "proc"},
                "mcp": {
                    "session_id": "sess",
                    "protocol_request_id": "req",
                    "correlation_request_id": "corr",
                },
                "error": {"message": "failed"},
            },
            {"action_id": "orphan", "parent_action_id": "f" * 16},
        ],
        "diagnostics": {"missing_parents": ["f" * 16]},
    }


def test_ticket_default_and_explicit_tree_render_incomplete_context(
    capsys, monkeypatch
):
    client = _Client(_body())
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://test"))
    cli.cmd_trace(_args())
    output = capsys.readouterr().out
    assert "transport_replay" in output
    assert "process=proc" in output and "correlation=corr" in output
    assert "incomplete diagnostics" in output
    assert "incomplete component" in output

    cli.cmd_trace(_args(tree=True))
    assert "dispatch" in capsys.readouterr().out


def test_json_and_jsonl_flags_are_machine_readable(monkeypatch, capsys):
    body = _body()
    client = _Client(body)
    monkeypatch.setattr(cli, "get_client", lambda _args: (client, "http://test"))
    cli.cmd_trace(_args(json=True))
    assert json.loads(capsys.readouterr().out)["events"]
    cli.cmd_trace(_args(jsonl=True))
    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[0])["action_id"] == "a" * 16
    assert client.params is not None and client.params["causal"] is True
