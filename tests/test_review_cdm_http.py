"""CDM responses must reach the review agent without shell processing."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import agents.review.server as review
from providers.execution import AuditedAsyncHTTPClient
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


@pytest.fixture
def cdm_client(monkeypatch):
    events = []
    handler = None

    async def emit(event):
        events.append(event)

    def install(request_handler):
        nonlocal handler
        handler = request_handler

        def factory(**kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            return AuditedAsyncHTTPClient(client=client, emit=emit)

        monkeypatch.setattr(review, "AuditedAsyncHTTPClient", factory)

    monkeypatch.setattr(review, "_initialized", True)
    monkeypatch.setattr(review, "_skill_provider", None)
    monkeypatch.setattr(
        review,
        "_ticket",
        {
            "custom_fields": {
                "assigned_hardware_ips": {"controller": "controller.example"}
            }
        },
    )
    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-cdm", agent_id="review")
    )
    yield install
    reset_trace_context(token)


@pytest.mark.asyncio
async def test_cdm_queries_preserve_success_and_structured_http_error(cdm_client):
    def handler(request):
        assert request.url.host == "controller.example"
        assert request.url.port == 3000
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": ["metric-data"]})
        assert json.loads(request.content)["breakouts"] == ["cpu"]
        return httpx.Response(
            400,
            json={
                "code": "UNKNOWN_FIELDS",
                "error": "Unknown field(s): breakouts (did you mean: breakout?)",
                "details": {"valid": ["breakout"]},
            },
        )

    cdm_client(handler)
    result = json.loads(
        await review.cdm_api_requests(
            "controller.example",
            [
                {"label": "help", "method": "GET", "path": "/api/v1"},
                {
                    "label": "cpu",
                    "method": "POST",
                    "path": "/api/v1/metric-data",
                    "body": {"breakouts": ["cpu"]},
                },
            ],
        )
    )
    assert result["help"] == {"endpoints": ["metric-data"]}
    error = result["cpu"]
    assert error["status_code"] == 400
    assert "did you mean: breakout?" in error["error"]
    assert error["response_body"]["details"] == {"valid": ["breakout"]}


@pytest.mark.asyncio
async def test_cdm_text_error_and_non_json_success_are_not_truncated(cdm_client):
    body = "failure details " * 300
    cdm_client(lambda request: httpx.Response(503, text=body))
    result = json.loads(await review.cdm_api_request("controller.example", "/api/v1"))
    assert result["status_code"] == 503
    assert result["response_body"] == body
    cdm_client(lambda request: httpx.Response(200, text=body))
    result = json.loads(await review.cdm_api_request("controller.example", "/api/v1"))
    assert result["error"] == "Response is not valid JSON"
    assert result["raw_output"] == body


@pytest.mark.asyncio
async def test_cdm_connection_error_is_returned(cdm_client):
    def fail(request):
        raise httpx.ConnectError("cannot connect", request=request)

    cdm_client(fail)
    result = json.loads(await review.cdm_api_request("controller.example", "/api/v1"))
    assert result["status"] == "error"
    assert "ConnectError" in result["error"]


@pytest.mark.asyncio
async def test_cdm_timeout_is_returned_and_cancellation_propagates(cdm_client):
    def timeout(request):
        raise httpx.ReadTimeout("CDM timed out", request=request)

    cdm_client(timeout)
    result = json.loads(await review.cdm_api_request("controller.example", "/api/v1"))
    assert result["status"] == "error"
    assert "ReadTimeout" in result["error"]

    def cancel(_request):
        raise asyncio.CancelledError()

    cdm_client(cancel)
    with pytest.raises(asyncio.CancelledError):
        await review.cdm_api_requests(
            "controller.example",
            [{"label": "query", "method": "GET", "path": "/api/v1"}],
        )


@pytest.mark.asyncio
async def test_batch_preserves_each_unexpected_failure_label(cdm_client, monkeypatch):
    async def fail(controller, path, **kwargs):
        raise RuntimeError(f"failure for {path}")

    monkeypatch.setattr(review, "cdm_api_request", fail)
    result = json.loads(
        await review.cdm_api_requests(
            "controller.example",
            [
                {"label": "first", "path": "/api/v1/first"},
                {"label": "second", "path": "/api/v1/second"},
            ],
        )
    )
    assert "failure for /api/v1/first" in result["first"]["error"]
    assert "failure for /api/v1/second" in result["second"]["error"]


@pytest.mark.asyncio
async def test_compare_results_uses_http_and_preserves_failures(cdm_client):
    def handler(request):
        assert request.url.path == "/api/v1/iterations/metric-values"
        run = json.loads(request.content)["runIds"][0]
        return (
            httpx.Response(200, json={"value": 5})
            if run == "good"
            else httpx.Response(500, json={"error": "bad run"})
        )

    cdm_client(handler)
    result = json.loads(
        await review.compare_results("controller.example", ["good", "bad"])
    )
    assert result["results"]["good"] == {"value": 5}
    assert result["results"]["bad"]["status_code"] == 500
    assert result["results"]["bad"]["response_body"] == {"error": "bad run"}


@pytest.mark.asyncio
async def test_cdm_rejects_invalid_method_or_external_url(cdm_client):
    cdm_client(lambda request: pytest.fail("invalid requests must not be sent"))
    for method, path in [("DELETE", "/api/v1"), ("GET", "//other.example/api")]:
        result = json.loads(
            await review.cdm_api_request("controller.example", path, method)
        )
        assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cdm_rejects_unassigned_host_port_and_non_cdm_path(cdm_client):
    cdm_client(lambda request: pytest.fail("unapproved requests must not be sent"))
    for host, port, path in [
        ("other.example", 3000, "/api/v1"),
        ("controller.example", 8080, "/api/v1"),
        ("controller.example", 3000, "/admin"),
        ("controller.example", 3000, "/api/v1/../../admin"),
        ("controller.example", 3000, "/api/v1/%2e%2e/%2e%2e/admin"),
        ("controller.example", 3000, "/api/v1/%252e%252e/admin"),
        ("controller.example", 3000, "/api/v1/%2fadmin"),
    ]:
        result = json.loads(await review.cdm_api_request(host, path, port=port))
        assert result["status"] == "error"


@pytest.mark.asyncio
async def test_cdm_accepts_recorded_controller_aliases_and_configured_port(
    cdm_client, monkeypatch
):
    monkeypatch.setattr(
        review,
        "_ticket",
        {
            "custom_fields": {
                "crucible_controller_context": {"host": "private.example"},
                "ssh_hardware_ips": {"controller": "ssh.example"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    monkeypatch.setattr(review, "_skill_provider", object())
    monkeypatch.setattr(
        review, "skill_config_view", AsyncMock(return_value={"cdm_port": 3300})
    )
    cdm_client(
        lambda request: httpx.Response(
            200, json={"host": request.url.host, "port": request.url.port}
        )
    )
    for host in ("private.example", "ssh.example", "controller.example"):
        result = json.loads(await review.cdm_api_request(host, "/api/v1", port=3300))
        assert result == {"host": host, "port": 3300}
    rejected = json.loads(
        await review.cdm_api_request("controller.example", "/api/v1", port=3000)
    )
    assert rejected["status"] == "error"


@pytest.mark.asyncio
async def test_cdm_initializes_without_ssh_and_later_ssh_tools_still_initialize(
    cdm_client, monkeypatch
):
    ticket = {
        "custom_fields": {"assigned_hardware_ips": {"controller": "controller.example"}}
    }
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "store.example":
            return httpx.Response(200, json=ticket)
        return httpx.Response(200, json={"value": 5})

    cdm_client(handler)
    monkeypatch.setattr(review, "_initialized", False)
    monkeypatch.setattr(review, "_cdm_initialized", False)
    monkeypatch.setattr(review, "_cdm_init_lock", asyncio.Lock())
    monkeypatch.setattr(review, "_ticket", {})
    monkeypatch.setattr(review, "_ssh", None)
    monkeypatch.setattr(review, "_crucible_context", None)
    monkeypatch.setattr(review, "_repo_cache", None)
    monkeypatch.setenv("TICKET_ID", "PERF-CDM")
    monkeypatch.setenv("STATE_STORE_URL", "http://store.example")
    monkeypatch.setattr(review, "ticket_state_headers", lambda: {})
    ssh_init = AsyncMock(side_effect=AssertionError("CDM must not resolve SSH"))
    monkeypatch.setattr(review, "build_ssh_from_ticket", ssh_init)
    skill_init = AsyncMock(return_value=object())
    monkeypatch.setattr(review, "build_skill_provider_async", skill_init)
    monkeypatch.setattr(
        review, "skill_config_view", AsyncMock(return_value={"cdm_port": 3000})
    )

    first, second = await asyncio.gather(
        review.cdm_api_request("controller.example", "/api/v1/metric-data"),
        review.compare_results("controller.example", ["run-1"]),
    )
    assert json.loads(first) == {"value": 5}
    assert json.loads(second)["results"]["run-1"] == {"value": 5}
    config = json.loads(await review.get_review_config("crucible"))
    assert config["review_config"]["cdm_port"] == 3000
    assert calls.count("http://store.example/api/v1/tickets/PERF-CDM") == 1
    skill_init.assert_awaited_once()
    ssh_init.assert_not_awaited()
    assert not review._initialized

    ssh_executor = object()
    ssh_init.side_effect = None
    ssh_init.return_value = (ssh_executor, ticket)
    monkeypatch.setattr(
        review, "build_crucible_context_gateway", lambda **kwargs: object()
    )
    monkeypatch.setattr(review, "build_repo_cache", AsyncMock(return_value=object()))
    await review._ensure_init()
    ssh_init.assert_awaited_once()
    assert review._ssh is ssh_executor


@pytest.mark.asyncio
async def test_cdm_init_failure_does_not_publish_partial_state(cdm_client, monkeypatch):
    ticket = {
        "custom_fields": {"assigned_hardware_ips": {"controller": "controller.example"}}
    }
    cdm_client(lambda _request: httpx.Response(200, json=ticket))
    monkeypatch.setattr(review, "_initialized", False)
    monkeypatch.setattr(review, "_cdm_initialized", False)
    monkeypatch.setattr(review, "_cdm_init_lock", asyncio.Lock())
    monkeypatch.setattr(review, "_ticket", {})
    monkeypatch.setattr(review, "_skill_provider", None)
    monkeypatch.setenv("TICKET_ID", "PERF-CDM")
    monkeypatch.setenv("STATE_STORE_URL", "http://store.example")
    monkeypatch.setattr(review, "ticket_state_headers", lambda: {})
    provider = object()
    monkeypatch.setattr(
        review,
        "build_skill_provider_async",
        AsyncMock(side_effect=[RuntimeError("config unavailable"), provider]),
    )
    with pytest.raises(RuntimeError, match="config unavailable"):
        await review._ensure_cdm_init()
    assert not review._cdm_initialized
    assert review._ticket == {}
    assert review._skill_provider is None
    await review._ensure_cdm_init()
    assert review._ticket == ticket
    assert review._skill_provider is provider


@pytest.mark.asyncio
async def test_cdm_and_ssh_initializers_do_not_publish_stale_ticket(
    cdm_client, monkeypatch
):
    old_ticket = {
        "custom_fields": {"assigned_hardware_ips": {"controller": "old.example"}}
    }
    new_ticket = {
        "custom_fields": {"assigned_hardware_ips": {"controller": "new.example"}}
    }
    cdm_client(lambda _request: httpx.Response(200, json=old_ticket))
    monkeypatch.setattr(review, "_initialized", False)
    monkeypatch.setattr(review, "_cdm_initialized", False)
    monkeypatch.setattr(review, "_cdm_init_lock", asyncio.Lock())
    monkeypatch.setattr(review, "_ticket", {})
    monkeypatch.setattr(review, "_ssh", None)
    monkeypatch.setattr(review, "_crucible_context", None)
    monkeypatch.setattr(review, "_repo_cache", None)
    monkeypatch.setenv("TICKET_ID", "PERF-CDM")
    monkeypatch.setenv("STATE_STORE_URL", "http://store.example")
    monkeypatch.setattr(review, "ticket_state_headers", lambda: {})
    started, release = asyncio.Event(), asyncio.Event()
    providers = [object(), object()]

    async def build_provider(**_kwargs):
        provider = providers.pop(0)
        if len(providers) == 1:
            started.set()
            await release.wait()
        return provider

    ssh_init = AsyncMock(return_value=(object(), new_ticket))
    monkeypatch.setattr(review, "build_skill_provider_async", build_provider)
    monkeypatch.setattr(review, "build_ssh_from_ticket", ssh_init)
    monkeypatch.setattr(
        review, "build_crucible_context_gateway", lambda **_kwargs: object()
    )
    monkeypatch.setattr(review, "build_repo_cache", AsyncMock(return_value=object()))
    cdm_task = asyncio.create_task(review._ensure_cdm_init())
    await asyncio.wait_for(started.wait(), timeout=2)
    ssh_task = asyncio.create_task(review._ensure_init())
    await asyncio.sleep(0)
    ssh_init.assert_not_awaited()
    release.set()
    await asyncio.gather(cdm_task, ssh_task)
    assert review._ticket == new_ticket
    assert review._initialized
