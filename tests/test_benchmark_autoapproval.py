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

import asyncio
import json
from datetime import datetime, timezone

import pytest

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
from state_store.trace_store import TraceStore
from tests.test_benchmark_idempotency import (
    _crucible_fixture,
    _wait_for_event_or_producer,
)


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 300:
            raise RuntimeError(self.text)


class _FakeApprovalsAPI:
    """Records approval-API calls; create/resolve/comment succeed."""

    def __init__(
        self,
        *,
        fail_create: bool = False,
        resolve_lost: bool = False,
        resolve_http_409: bool = False,
        apply_resolution: bool = True,
        fail_list: bool = False,
        mismatched_binding: bool = False,
        create_lost: bool = False,
        apply_create: bool = True,
        create_status: str = "pending",
        create_returns_manual: bool = False,
        create_http_5xx: bool = False,
        seed_unrelated: bool = False,
        seed_duplicate: bool = False,
        consume_lost: bool = False,
        consume_http_409: bool = False,
        consume_http_422: bool = False,
        apply_consume: bool = True,
        mismatched_consume_binding: bool = False,
        consume_cancel: bool = False,
        fail_list_after_consume: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_create = fail_create
        self.resolve_lost = resolve_lost
        self.resolve_http_409 = resolve_http_409
        self.apply_resolution = apply_resolution
        self.fail_list = fail_list
        self.mismatched_binding = mismatched_binding
        self.create_lost = create_lost
        self.apply_create = apply_create
        self.create_status = create_status
        self.create_returns_manual = create_returns_manual
        self.create_http_5xx = create_http_5xx
        self.seed_unrelated = seed_unrelated
        self.seed_duplicate = seed_duplicate
        self.consume_lost = consume_lost
        self.consume_http_409 = consume_http_409
        self.consume_http_422 = consume_http_422
        self.apply_consume = apply_consume
        self.mismatched_consume_binding = mismatched_consume_binding
        self.consume_cancel = consume_cancel
        self.fail_list_after_consume = fail_list_after_consume
        self.approval_seq = 0
        self.approvals: list[dict] = []

    async def get(self, url: str, **kwargs) -> _Response:
        self.calls.append((str(url), {}))
        if str(url).endswith("/approvals"):
            if self.fail_list or (
                self.fail_list_after_consume
                and any(item.get("consumed_at") for item in self.approvals)
            ):
                return _Response(503, {"detail": "listing unavailable"})
            return _Response(200, {"approvals": self.approvals})
        return _Response(404, {"detail": "unexpected url"})

    async def post(self, url: str, json: dict | None = None, **kwargs) -> _Response:
        self.calls.append((str(url), dict(json or {})))
        if str(url).endswith("/approvals"):
            if self.fail_create:
                return _Response(409, {"detail": "approval rejected: claim_expired"})
            self.approval_seq += 1
            approval_id = f"apr-auto{self.approval_seq}"
            approval = {
                "approval_request_id": approval_id,
                "ticket_id": "T-788",
                "status": self.create_status,
                **{
                    key: json[key]
                    for key in (
                        "kind",
                        "validation_id",
                        "presented_run_file_digest",
                        "execution_intent_digest",
                        "summary",
                        "invocation_id",
                        "tool_call_id",
                        "session_id",
                        "session_epoch",
                        "waiter_owner",
                        "claim_id",
                        "ticket_attempt",
                    )
                },
            }
            if self.create_returns_manual:
                approval["summary"] = "Manual benchmark review"
            if self.apply_create:
                self.approvals.append(approval)
            if self.seed_unrelated:
                self.approvals.append(
                    {
                        **approval,
                        "approval_request_id": "apr-unrelated",
                        "tool_call_id": "other-tool",
                        "summary": "Manual benchmark review",
                        "status": "pending",
                    }
                )
            if self.seed_duplicate:
                self.approvals.append(
                    {**approval, "approval_request_id": "apr-duplicate"}
                )
            if self.create_lost:
                raise ConnectionError("create response lost")
            if self.create_http_5xx:
                return _Response(503, {"detail": "create committed, response failed"})
            return _Response(200, approval)
        if "/resolve" in str(url):
            approval_id = str(url).split("/approvals/", 1)[1].split("/", 1)[0]
            approval = next(
                (
                    item
                    for item in self.approvals
                    if item["approval_request_id"] == approval_id
                ),
                None,
            )
            if approval is None:
                return _Response(404, {"detail": "approval not found"})
            if self.apply_resolution:
                approval["status"] = "approved"
                if self.mismatched_binding:
                    approval["execution_intent_digest"] = "wrong"
            if self.resolve_lost:
                raise ConnectionError("resolve response lost")
            if self.resolve_http_409:
                return _Response(409, {"detail": "approval already resolved"})
            return _Response(200, approval)
        if "/consume" in str(url):
            approval_id = str(url).split("/approvals/", 1)[1].split("/", 1)[0]
            approval = next(
                (
                    item
                    for item in self.approvals
                    if item["approval_request_id"] == approval_id
                ),
                None,
            )
            if approval is None or approval.get("consumed_at"):
                return _Response(409, {"detail": "approval unavailable"})
            if self.consume_http_422:
                return _Response(422, {"detail": "invalid consume request"})
            if self.apply_consume:
                approval["consumed_at"] = datetime.now(timezone.utc).isoformat()
            if self.mismatched_consume_binding:
                approval["claim_id"] = "other-claim"
            if self.consume_cancel:
                raise asyncio.CancelledError()
            if self.consume_lost:
                raise ConnectionError("consume response lost")
            if self.consume_http_409:
                return _Response(409, {"detail": "approval already consumed"})
            return _Response(200, approval)
        if "/comments" in str(url):
            if not json.get("author"):
                return _Response(422, {"detail": "author required"})
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


def _wire(
    monkeypatch,
    tmp_path,
    *,
    directives: dict | None,
    launch=None,
    **api_options,
):
    """Reuse the deterministic crucible fixture, then add directives and
    the fake approvals API."""
    record, calls = _crucible_fixture(tmp_path, monkeypatch, launch=launch)
    record["execution_intent_digest"] = "e" * 64
    # A ticket-owned MCP server receives its causal context at launch;
    # restore the same environment so the trace context resolves.
    monkeypatch.setenv("AGENTIC_PERF_TRACE_ID", "a" * 32)
    monkeypatch.setenv(
        "AGENTIC_PERF_TRACE_INVOCATION_ID", "0123456789abcdef0123456789abcdef"
    )
    monkeypatch.setenv("AGENTIC_PERF_TRACE_ACTION_ID", "b" * 16)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "session-788")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "epoch-1")
    monkeypatch.setenv("AGENTIC_PERF_CLAIM_ID", "claim-788")
    ticket = {
        "id": "T-788",
        "status": "executing_benchmark",
        "custom_fields": {
            "benchmark_validations": {"records": {"val-788": record}},
            "directives": directives,
        },
    }

    async def active_check(**kwargs):
        return ticket

    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    api = _FakeApprovalsAPI(**api_options)
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
    create_bodies = [
        body for url, body in api.calls if url.endswith("/approvals") and body
    ]
    assert create_bodies[0]["kind"] == "benchmark_run_file"
    assert create_bodies[0]["summary"].startswith("Automatic run-file approval")
    assert create_bodies[0]["validation_id"] == "val-788"
    assert create_bodies[0]["execution_intent_digest"] == "e" * 64
    assert create_bodies[0]["invocation_id"]
    assert create_bodies[0]["tool_call_id"] == "execute-benchmark-autoapproval"
    # The single consume happens on execute_benchmark's normal path.
    assert any("/consume" in u for u in urls)
    # The ticket carries the audit note.
    assert any("/comments" in u for u in urls)
    comment_bodies = [body for url, body in api.calls if "/comments" in url]
    assert comment_bodies[0]["author"] == "benchmark-agent/execute_benchmark"
    assert "apr-auto1" in comment_bodies[0]["body"]


@pytest.mark.asyncio
async def test_lost_resolve_response_reconciles_approved_binding(tmp_path, monkeypatch):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        resolve_lost=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed"
    assert len(_approval_calls(api, "/resolve")) == 1
    assert (
        len(
            [
                url
                for url, body in api.calls
                if url.endswith("/approvals") and body == {}
            ]
        )
        == 2
    )
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_resolve_409_after_commit_reconciles_approved_binding(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        resolve_http_409=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result
    assert len(_approval_calls(api, "/resolve")) == 1
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("create_status", ["pending", "approved"])
async def test_lost_create_response_reconciles_exact_binding(
    tmp_path, monkeypatch, create_status
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        create_lost=True,
        create_status=create_status,
        seed_unrelated=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed"
    assert len(_approval_calls(api, "/resolve")) == (
        1 if create_status == "pending" else 0
    )
    assert api.approvals[0]["status"] == "approved"
    assert api.approvals[1]["status"] == "pending"
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_create_commits_but_returns_5xx_reuses_exact_approval(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        create_http_5xx=True,
        seed_unrelated=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result
    assert (
        len([url for url, body in api.calls if url.endswith("/approvals") and body])
        == 1
    )
    assert [item["approval_request_id"] for item in api.approvals] == [
        "apr-auto1",
        "apr-unrelated",
    ]
    assert api.approvals[0]["consumed_at"]
    assert api.approvals[1]["status"] == "pending"
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_consume_commits_but_response_is_lost_launches_once(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        consume_lost=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result
    assert [item["approval_request_id"] for item in api.approvals] == ["apr-auto1"]
    assert api.approvals[0]["consumed_at"]
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1
    store = TraceStore(tmp_path / "trace.db")
    try:
        operation = store.get_operation("benchmark-execution:T-788:intent-1")
        assert operation.external_ids["approval_request_id"] == "apr-auto1"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_consume_409_after_exact_bound_commit_launches_once(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        consume_http_409=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result
    assert api.approvals[0]["consumed_at"]
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_options",
    [
        {"apply_consume": False},
        {"mismatched_consume_binding": True},
    ],
)
async def test_consume_409_without_bound_consumed_readback_rejects(
    tmp_path, monkeypatch, api_options
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        consume_http_409=True,
        **api_options,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result == {"status": "rejected", "reason_code": "approval_not_approved"}
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert not [call for call in calls if call[0] == "launch"]
    store = TraceStore(tmp_path / "trace.db")
    try:
        operation = store.get_operation("benchmark-execution:T-788:intent-1")
        assert operation.state == "terminal"
        assert operation.terminal_outcome == "failure"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_consume_definite_4xx_rejects_without_readback(tmp_path, monkeypatch):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        consume_http_422=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result == {"status": "rejected", "reason_code": "approval_not_approved"}
    consume_index = next(
        index for index, (url, _) in enumerate(api.calls) if url.endswith("/consume")
    )
    assert not any(
        url.endswith("/approvals") for url, _ in api.calls[consume_index + 1 :]
    )
    assert not [call for call in calls if call[0] == "launch"]


@pytest.mark.asyncio
async def test_cancel_after_consumed_approval_replays_without_second_consume(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        consume_cancel=True,
    )
    monkeypatch.setattr(benchmark_server, "_BENCHMARK_OPERATION_LEASE_TTL_SECONDS", 0.3)

    first_task = asyncio.create_task(
        benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert not [call for call in calls if call[0] == "launch"]
    store = TraceStore(tmp_path / "trace.db")
    try:
        operation = store.get_operation("benchmark-execution:T-788:intent-1")
        assert operation.state == "prepared"
        assert operation.external_ids["approval_request_id"] == "apr-auto1"
        expiry = datetime.fromisoformat(operation.lease_expires_at)
    finally:
        store.close()
    await asyncio.sleep(
        max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05
    )
    api.consume_cancel = False

    replay = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert replay["status"] == "completed", replay
    assert [item["approval_request_id"] for item in api.approvals] == ["apr-auto1"]
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("consume_response", ["consume_lost", "consume_http_409"])
async def test_uncertain_consume_readback_leaves_prepared_operation_reclaimable(
    tmp_path, monkeypatch, consume_response
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        fail_list_after_consume=True,
        **{consume_response: True},
    )
    monkeypatch.setattr(benchmark_server, "_BENCHMARK_OPERATION_LEASE_TTL_SECONDS", 0.3)

    first = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert first["reason_code"] == "approval_check_failed"
    store = TraceStore(tmp_path / "trace.db")
    try:
        operation = store.get_operation("benchmark-execution:T-788:intent-1")
        assert operation.state == "prepared"
        assert operation.external_ids["approval_request_id"] == "apr-auto1"
        expiry = datetime.fromisoformat(operation.lease_expires_at)
    finally:
        store.close()
    await asyncio.sleep(
        max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05
    )
    api.fail_list_after_consume = False

    replay = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert replay["status"] == "completed", replay
    assert [item["approval_request_id"] for item in api.approvals] == ["apr-auto1"]
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_options",
    [
        {"create_lost": True, "fail_list": True},
        {"create_lost": True, "apply_create": False},
        {"create_lost": True, "apply_create": False, "seed_unrelated": True},
        {"create_lost": True, "seed_duplicate": True},
    ],
)
async def test_lost_create_without_unique_bound_request_fails_closed(
    tmp_path, monkeypatch, api_options
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        **api_options,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert not _approval_calls(api, "/resolve")
    assert not _approval_calls(api, "/consume")
    assert not [call for call in calls if call[0] == "launch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_options",
    [
        {"resolve_lost": True, "apply_resolution": False},
        {"resolve_lost": True, "fail_list": True},
        {"resolve_lost": True, "mismatched_binding": True},
    ],
)
async def test_unconfirmed_resolution_fails_closed(tmp_path, monkeypatch, api_options):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        **api_options,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert not _approval_calls(api, "/consume")
    assert not [call for call in calls if call[0] == "launch"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_terminal_replay_create_one_approval(
    tmp_path, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def launch():
        entered.set()
        await release.wait()

    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        launch=launch,
    )
    first_task = asyncio.create_task(
        benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    try:
        await _wait_for_event_or_producer(
            entered, first_task, operation="benchmark launch"
        )
        concurrent = json.loads(
            await benchmark_server.execute_benchmark(
                "controller", validation_id="val-788"
            )
        )
        assert concurrent["reason_code"] == "existing_operation"
        release.set()
        first = json.loads(await first_task)
    finally:
        release.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
    replay = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert first["status"] == replay["status"] == "completed"
    assert replay["existing_operation"] is True
    assert (
        len([url for url, body in api.calls if url.endswith("/approvals") and body])
        == 1
    )
    assert len(_approval_calls(api, "/resolve")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_cancel_after_resolve_reuses_approval_after_prepared_lease_expires(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )
    monkeypatch.setattr(benchmark_server, "_BENCHMARK_OPERATION_LEASE_TTL_SECONDS", 0.3)
    note_entered = asyncio.Event()
    original_note = benchmark_server._add_execution_note

    async def pause_after_resolution(*_args):
        note_entered.set()
        await asyncio.Future()

    monkeypatch.setattr(benchmark_server, "_add_execution_note", pause_after_resolution)
    first_task = asyncio.create_task(
        benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    await _wait_for_event_or_producer(
        note_entered, first_task, operation="automatic approval resolution"
    )
    assert api.approvals[0]["status"] == "approved"
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    monkeypatch.setattr(benchmark_server, "_add_execution_note", original_note)

    store = TraceStore(tmp_path / "trace.db")
    try:
        operation = store.get_operation("benchmark-execution:T-788:intent-1")
        assert operation is not None
        assert operation.state == "prepared"
        expiry = datetime.fromisoformat(operation.lease_expires_at)
    finally:
        store.close()
    await asyncio.sleep(
        max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05
    )

    replay = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert replay["status"] == "completed", replay
    assert [item["approval_request_id"] for item in api.approvals] == ["apr-auto1"]
    assert (
        len([url for url, body in api.calls if url.endswith("/approvals") and body])
        == 1
    )
    assert len(_approval_calls(api, "/resolve")) == 1
    assert len(_approval_calls(api, "/approvals/apr-auto1/consume")) == 1
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_status", ["pending", "approved"])
async def test_preexisting_automatic_approval_is_reused_without_touching_manual_request(
    tmp_path, monkeypatch, approval_status
):
    record, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )
    automatic = {
        "approval_request_id": "apr-existing",
        "ticket_id": "T-788",
        "kind": "benchmark_run_file",
        "validation_id": "val-788",
        "presented_run_file_digest": record["runfile_fingerprint"],
        "execution_intent_digest": record["execution_intent_digest"],
        "summary": "Automatic run-file approval (user_pre_run_approval=false)",
        "session_id": "session-788",
        "session_epoch": "epoch-1",
        "claim_id": "claim-788",
        "ticket_attempt": "claim-788",
        "status": approval_status,
        "consumed_at": None,
    }
    api.approvals.extend(
        [
            automatic,
            {
                **automatic,
                "approval_request_id": "apr-manual",
                "summary": "Manual benchmark review",
                "status": "pending",
            },
        ]
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "completed", result
    assert not [url for url, body in api.calls if url.endswith("/approvals") and body]
    assert len(_approval_calls(api, "/approvals/apr-existing/resolve")) == (
        1 if approval_status == "pending" else 0
    )
    assert not _approval_calls(api, "/approvals/apr-manual/resolve")
    assert api.approvals[1]["status"] == "pending"
    assert len([call for call in calls if call[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_unrelated_consumed_automatic_approval_cannot_authorize_launch(
    tmp_path, monkeypatch
):
    record, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )
    api.approvals.append(
        {
            "approval_request_id": "apr-unrelated-consumed",
            "ticket_id": "T-788",
            "kind": "benchmark_run_file",
            "validation_id": "val-788",
            "presented_run_file_digest": record["runfile_fingerprint"],
            "execution_intent_digest": record["execution_intent_digest"],
            "summary": "Automatic run-file approval (user_pre_run_approval=false)",
            "session_id": "session-788",
            "session_epoch": "epoch-1",
            "claim_id": "claim-788",
            "ticket_attempt": "claim-788",
            "status": "approved",
            "consumed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["reason_code"] == "approval_required"
    assert not _approval_calls(api, "/consume")
    assert not [call for call in calls if call[0] == "launch"]


@pytest.mark.asyncio
async def test_stale_automatic_approval_fence_fails_closed(tmp_path, monkeypatch):
    _, calls, api = _wire(
        monkeypatch, tmp_path, directives={"user_pre_run_approval": False}
    )
    api.approvals.append(
        {
            "approval_request_id": "apr-old",
            "ticket_id": "T-788",
            "kind": "benchmark_run_file",
            "validation_id": "val-788",
            "presented_run_file_digest": benchmark_server._runfile_fingerprint(
                {"benchmarks": [{"name": "uperf"}], "tags": {}}
            ),
            "execution_intent_digest": "e" * 64,
            "summary": "Automatic run-file approval (user_pre_run_approval=false)",
            "session_id": "old-session",
            "session_epoch": "epoch-1",
            "claim_id": "claim-788",
            "ticket_attempt": "claim-788",
            "status": "approved",
            "consumed_at": None,
        }
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["reason_code"] == "approval_required"
    assert "stale" in result["auto_approval_error"]
    assert not [url for url, body in api.calls if url.endswith("/approvals") and body]
    assert not _approval_calls(api, "/resolve")
    assert not [call for call in calls if call[0] == "launch"]


@pytest.mark.asyncio
async def test_create_dedup_returning_manual_pending_approval_is_not_resolved(
    tmp_path, monkeypatch
):
    _, calls, api = _wire(
        monkeypatch,
        tmp_path,
        directives={"user_pre_run_approval": False},
        create_returns_manual=True,
    )

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["reason_code"] == "approval_required"
    assert "unrelated" in result["auto_approval_error"]
    assert api.approvals[0]["status"] == "pending"
    assert not _approval_calls(api, "/resolve")
    assert not [call for call in calls if call[0] == "launch"]


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
async def test_null_directives_keep_the_hard_gate(tmp_path, monkeypatch):
    _, calls, api = _wire(monkeypatch, tmp_path, directives=None)

    result = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "approval_required"
    assert api.calls == []
    assert not [call for call in calls if call[0] == "launch"]


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
    assert (
        len([url for url, body in api.calls if url.endswith("/approvals") and not body])
        == 1
    )
    assert not [c for c in calls if c[0] == "launch"]

    replay = json.loads(
        await benchmark_server.execute_benchmark("controller", validation_id="val-788")
    )
    assert replay["status"] == "rejected"
    assert replay["reason_code"] == "approval_required"
    assert replay["existing_operation"] is True
    assert (
        len([url for url, body in api.calls if url.endswith("/approvals") and body])
        == 1
    )
