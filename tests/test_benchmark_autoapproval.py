"""Automatic run-file approval for user_pre_run_approval=false.

The benchmark prompt promises a run without a human pause when the
directive opts out, while the durable-validation work (#788) requires
every ticket-scoped crucible execution to carry a bound approval.
execute_benchmark bridges the two: it creates and resolves the same
benchmark_run_file approval a human decision would produce (marked as
automatic) and lets its normal path perform the single atomic consume
(#1232).
"""

from __future__ import annotations

import json

import pytest

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
from tests.test_benchmark_idempotency import _crucible_fixture


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeApprovalsAPI:
    """Records approval-API calls; create/resolve/comment succeed."""

    def __init__(self, *, fail_create: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_create = fail_create
        self.approval_seq = 0

    async def post(self, url: str, json: dict | None = None, **kwargs) -> _Response:
        self.calls.append((str(url), dict(json or {})))
        if str(url).endswith("/approvals"):
            if self.fail_create:
                return _Response(409, {"detail": "approval rejected: claim_expired"})
            self.approval_seq += 1
            return _Response(
                200, {"approval_request_id": f"apr-auto{self.approval_seq}"}
            )
        if "/resolve" in str(url):
            return _Response(
                200, {"approval_request_id": "apr-auto", "status": "approved"}
            )
        if "/consume" in str(url):
            return _Response(200, {"approval_request_id": "apr-auto"})
        if "/comments" in str(url):
            return _Response(200, {})
        return _Response(404, {"detail": "unexpected url"})


class _FakeClient:
    """AuditedAsyncHTTPClient stand-in routing to the shared fake API."""

    api: _FakeApprovalsAPI | None = None

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return _FakeClient.api

    async def __aexit__(self, *args):
        return None


def _wire(monkeypatch, tmp_path, *, directives: dict | None, fail_create=False):
    """Reuse the deterministic crucible fixture, then add directives and
    the fake approvals API."""
    record, calls = _crucible_fixture(tmp_path, monkeypatch)
    # A ticket-owned MCP server receives its causal context at launch;
    # restore the same environment so the trace context resolves.
    monkeypatch.setenv("AGENTIC_PERF_TRACE_ID", "a" * 32)
    monkeypatch.setenv(
        "AGENTIC_PERF_TRACE_INVOCATION_ID", "0123456789abcdef0123456789abcdef"
    )
    monkeypatch.setenv("AGENTIC_PERF_TRACE_ACTION_ID", "b" * 16)
    ticket = {
        "id": "T-788",
        "status": "executing_benchmark",
        "custom_fields": {
            "benchmark_validations": {"records": {"val-788": record}},
            "directives": directives or {},
        },
    }

    async def active_check(**kwargs):
        return ticket

    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    api = _FakeApprovalsAPI(fail_create=fail_create)
    _FakeClient.api = api
    monkeypatch.setattr("providers.execution.AuditedAsyncHTTPClient", _FakeClient)
    return record, calls, api


def _approval_calls(api: _FakeApprovalsAPI, kind: str):
    return [
        (url, body)
        for url, json_body in api.calls
        for url, body in [(url, json_body)]
        if kind in url
    ]


@pytest.mark.asyncio
async def test_autoapproval_runs_the_benchmark_without_a_pause(tmp_path, monkeypatch):
    """user_pre_run_approval=false: execution completes with no
    approval_request_id from the agent, and the durable trail (create +
    resolve + consume + audit comment) was produced automatically."""
    _, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result

    urls = [url for url, _ in api.calls]
    assert any(u.endswith("/approvals") for u in urls)
    assert any("/resolve" in u for u in urls)
    create_bodies = [body for url, body in api.calls if url.endswith("/approvals")]
    assert create_bodies[0]["kind"] == "benchmark_run_file"
    assert create_bodies[0]["summary"].startswith("Automatic run-file approval")
    assert create_bodies[0]["validation_id"] == "val-788"
    assert create_bodies[0]["invocation_id"]
    assert create_bodies[0]["tool_call_id"] == "execute-benchmark-autoapproval"
    # The single consume happens on execute_benchmark's normal path.
    assert any("/consume" in u for u in urls)
    # The ticket carries the audit note.
    assert any("/comments" in u for u in urls)


@pytest.mark.asyncio
async def test_default_directive_keeps_the_hard_gate(tmp_path, monkeypatch):
    """Without the opt-out, execution without an approval id is still
    rejected and nothing touches the approvals API."""
    _, calls, api = _wire(monkeypatch, tmp_path, directives={})

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert "auto_approval_error" not in result
    assert api.calls == []
    assert not [c for c in calls if c[0] == "launch"]


@pytest.mark.asyncio
async def test_autoapproval_with_unknown_validation_rejected(tmp_path, monkeypatch):
    """The opt-out with no validation record fails closed with the
    reason surfaced in the rejection."""
    _, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )

    result = json.loads(
        await benchmark_server.execute_benchmark(
            "controller", validation_id="val-unknown"
        )
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert "no validation record" in result["auto_approval_error"]
    assert api.calls == []
    assert not [c for c in calls if c[0] == "launch"]


@pytest.mark.asyncio
async def test_autoapproval_creation_failure_rejected(tmp_path, monkeypatch):
    """If the store refuses the automatic approval, execution is
    rejected rather than allowed to proceed unapproved."""
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        fail_create=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert "claim_expired" in result["auto_approval_error"]
    assert not [c for c in calls if c[0] == "launch"]
