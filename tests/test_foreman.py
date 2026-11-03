from __future__ import annotations

import httpx
import pytest

from providers.execution import AuditedAsyncHTTPClient
from providers.foreman import ForemanClient, ForemanError


def client_for(handler):
    transport = httpx.MockTransport(handler)
    http = AuditedAsyncHTTPClient(client=httpx.AsyncClient(transport=transport))
    return ForemanClient("https://foreman.example", "user", "secret", client=http)


async def test_catalog_fetches_every_page():
    def handler(request):
        page = int(request.url.params["page"])
        items = [{"id": n} for n in range(1, 101)] if page == 1 else [{"id": 101}]
        return httpx.Response(200, json={"total": 101, "results": items})

    client = client_for(handler)
    try:
        result = await client.list_operating_systems()
        assert len(result) == 101
        assert result[-1]["id"] == 101
    finally:
        await client.close()


async def test_host_lookup_requires_exact_unique_match():
    def handler(request):
        if request.url.path.endswith("/hosts"):
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "results": [
                        {"id": 1, "name": "host.example"},
                        {"id": 2, "name": "host.example"},
                    ],
                },
            )
        raise AssertionError("ambiguous host must not be fetched")

    client = client_for(handler)
    try:
        with pytest.raises(ForemanError, match="ambiguous"):
            await client.find_host("host.example")
    finally:
        await client.close()


async def test_auth_error_is_sanitized():
    client = client_for(lambda request: httpx.Response(401, text="user secret"))
    try:
        with pytest.raises(ForemanError) as error:
            await client.list_operating_systems()
        assert error.value.category == "authentication"
        assert "secret" not in str(error.value)
    finally:
        await client.close()


def test_rejects_insecure_endpoint():
    with pytest.raises(ForemanError, match="HTTPS"):
        ForemanClient("http://foreman.example", "user", "secret")


async def test_options_revalidate_os_architecture_and_install_options():
    def handler(request):
        if request.url.path.endswith("/operatingsystems"):
            return httpx.Response(
                200, json={"total": 1, "results": [{"id": 17, "title": "RHEL 10.0"}]}
            )
        return httpx.Response(
            200,
            json={
                "architectures": [{"id": 2}],
                "media": [{"id": 3}],
                "ptables": [{"id": 4}],
            },
        )

    client = client_for(handler)
    try:
        options = await client.resolve_install_options(
            {"architecture_id": 2}, "RHEL 10.0", medium_id=3, ptable_id=4
        )
        assert options == {"operatingsystem_id": 17, "medium_id": 3, "ptable_id": 4}
        with pytest.raises(ForemanError, match="architecture"):
            await client.resolve_install_options(
                {"architecture_id": 9}, "RHEL 10.0", medium_id=3, ptable_id=4
            )
        with pytest.raises(ForemanError, match="ptables"):
            await client.resolve_install_options(
                {"architecture_id": 2}, "RHEL 10.0", medium_id=3, ptable_id=9
            )
        with pytest.raises(ForemanError, match="missing or ambiguous"):
            await client.resolve_install_options(
                {"architecture_id": 2}, "RHEL 10", medium_id=3, ptable_id=4
            )
    finally:
        await client.close()


async def test_incomplete_pagination_fails_closed():
    client = client_for(
        lambda request: httpx.Response(200, json={"total": 1, "results": []})
    )
    try:
        with pytest.raises(ForemanError, match="incomplete"):
            await client.list_operating_systems()
    finally:
        await client.close()


async def test_build_failure_after_send_is_unknown_and_redacted():
    class Transport:
        async def put(self, url, **kwargs):
            raise httpx.ReadTimeout("password secret")

        async def aclose(self):
            pass

    client = ForemanClient(
        "https://foreman.example", "user", "secret", client=Transport()
    )
    with pytest.raises(ForemanError) as error:
        await client.set_build(
            4, {"operatingsystem_id": 1, "medium_id": 2, "ptable_id": 3}
        )
    assert error.value.category == "build_unknown"
    assert "secret" not in str(error.value)


async def test_implicit_install_options_must_be_unique():
    multiple = False

    def handler(request):
        if request.url.path.endswith("/operatingsystems"):
            return httpx.Response(
                200, json={"total": 1, "results": [{"id": 17, "title": "RHEL 10.0"}]}
            )
        media = [{"id": 3}, {"id": 9}] if multiple else [{"id": 3}]
        return httpx.Response(
            200,
            json={"architectures": [{"id": 2}], "media": media, "ptables": [{"id": 4}]},
        )

    client = client_for(handler)
    try:
        options = await client.resolve_install_options(
            {"architecture_id": 2}, "RHEL 10.0"
        )
        assert options["medium_id"] == 3
        multiple = True
        with pytest.raises(ForemanError, match="ambiguous.*3, 9"):
            await client.resolve_install_options({"architecture_id": 2}, "RHEL 10.0")
    finally:
        await client.close()


def test_tls_uses_configured_ca_bundle(monkeypatch):
    from unittest.mock import MagicMock

    import providers.foreman as module

    ssl_context = object()
    make_context = MagicMock(return_value=ssl_context)
    make_client = MagicMock()
    monkeypatch.setattr(module.ssl, "create_default_context", make_context)
    monkeypatch.setattr(module, "AuditedAsyncHTTPClient", make_client)
    ForemanClient(
        "https://foreman.example", "user", "secret", ca_bundle="/etc/lab-ca.pem"
    )
    make_context.assert_called_once_with(cafile="/etc/lab-ca.pem")
    assert make_client.call_args.kwargs["verify"] is ssl_context


async def test_build_is_audited_once_and_does_not_leak_basic_auth():
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    events = []
    requests = []

    async def emit(event):
        events.append(event)

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("provider secret", request=request)

    http = AuditedAsyncHTTPClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), emit=emit
    )
    client = ForemanClient(
        "https://foreman.example", "operator", "credential", client=http
    )
    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-test", agent_id="platform")
    )
    try:
        with pytest.raises(ForemanError) as error:
            await client.set_build(
                7, {"operatingsystem_id": 1, "medium_id": 2, "ptable_id": 3}
            )
        assert error.value.category == "build_unknown"
    finally:
        reset_trace_context(token)
        await client.close()
    assert len(requests) == 1
    serialized = "\n".join(event.model_dump_json() for event in events)
    assert "credential" not in serialized
    assert "operator" not in serialized
    operation = [event for event in events if event.action_id == events[0].action_id]
    assert [event.lifecycle.state.value for event in operation] == [
        "requested",
        "indeterminate",
    ]


async def test_host_lookup_verifies_detail_identity():
    def handler(request):
        if request.url.path.endswith("/hosts"):
            assert request.url.params["search"] == 'name = "host.example"'
            return httpx.Response(
                200, json={"total": 1, "results": [{"id": 7, "name": "host.example"}]}
            )
        return httpx.Response(200, json={"id": 7, "name": "other.example"})

    client = client_for(handler)
    try:
        with pytest.raises(ForemanError, match="identity changed"):
            await client.find_host("host.example")
    finally:
        await client.close()


async def test_build_http_failure_is_ambiguous_and_not_retried():
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    requests = []
    events = []

    async def emit(event):
        events.append(event)

    def handler(request):
        requests.append(request)
        return httpx.Response(500, text="credential")

    http = AuditedAsyncHTTPClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), emit=emit
    )
    client = ForemanClient(
        "https://foreman.example", "operator", "credential", client=http
    )
    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-test", agent_id="platform")
    )
    try:
        with pytest.raises(ForemanError) as error:
            await client.set_build(
                7, {"operatingsystem_id": 1, "medium_id": 2, "ptable_id": 3}
            )
    finally:
        reset_trace_context(token)
        await client.close()
    assert error.value.category == "build_unknown"
    assert "credential" not in str(error.value)
    assert len(requests) == 1
    assert "credential" not in "\n".join(event.model_dump_json() for event in events)


async def test_catalog_rejects_duplicate_ids_across_pages():
    client = client_for(
        lambda request: httpx.Response(200, json={"total": 2, "results": [{"id": 7}]})
    )
    try:
        with pytest.raises(ForemanError, match="duplicate identities"):
            await client.list_operating_systems()
    finally:
        await client.close()
