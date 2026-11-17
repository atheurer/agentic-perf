import httpx
import pytest

from providers.execution import AuditedAsyncHTTPClient
from providers.redfish import RedfishClient, RedfishError
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


async def _noop_emit(e):
    return None


def client_for(handler):
    transport = httpx.MockTransport(handler)
    http = AuditedAsyncHTTPClient(
        client=httpx.AsyncClient(transport=transport),
        supports_idempotency=True,
        emit=_noop_emit,
    )
    return RedfishClient("https://bmc.example", "admin", "secret", client=http)


def test_rejects_http():
    with pytest.raises(RedfishError, match="HTTPS"):
        RedfishClient("http://bmc.example", "u", "p")


def test_rejects_missing_creds():
    with pytest.raises(RedfishError, match="credentials"):
        RedfishClient("https://bmc.example", "", "p")


async def test_get_power_state():
    client = client_for(lambda r: httpx.Response(200, json={"PowerState": "On"}))
    try:
        assert await client.get_power_state() == "On"
    finally:
        await client.close()


async def test_auth_failure():
    client = client_for(lambda r: httpx.Response(401, text="secret"))
    try:
        with pytest.raises(RedfishError) as e:
            await client.get_power_state()
        assert e.value.category == "authentication"
        assert "secret" not in str(e.value)
    finally:
        await client.close()


async def test_404():
    client = client_for(lambda r: httpx.Response(404))
    try:
        with pytest.raises(RedfishError, match="not found"):
            await client.get_power_state()
    finally:
        await client.close()


async def test_set_one_shot_pxe_verifies():
    def handler(r):
        if r.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "Boot": {
                    "BootSourceOverrideTarget": "Pxe",
                    "BootSourceOverrideEnabled": "Once",
                }
            },
        )

    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-test", agent_id="platform")
    )
    client = client_for(handler)
    try:
        await client.set_one_shot_pxe()
    finally:
        await client.close()
        reset_trace_context(token)


async def test_set_one_shot_pxe_fails_if_not_verified():
    def handler(r):
        if r.method == "PATCH":
            return httpx.Response(200)
        return httpx.Response(200, json={"Boot": {"BootSourceOverrideTarget": "Hdd"}})

    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-test", agent_id="platform")
    )
    client = client_for(handler)
    try:
        with pytest.raises(RedfishError):
            await client.set_one_shot_pxe()
    finally:
        await client.close()
        reset_trace_context(token)


async def test_reset_invalid_type():
    client = client_for(lambda r: httpx.Response(200))
    try:
        with pytest.raises(RedfishError, match="ResetType"):
            await client.reset("BadType")
    finally:
        await client.close()


async def test_poll_timeout():
    client = client_for(lambda r: httpx.Response(200, json={"PowerState": "On"}))
    try:

        async def never():
            return False

        with pytest.raises(RedfishError, match="Timed out"):
            await client.poll_until_ssh_reachable(never, timeout=0.1, interval=0.05)
    finally:
        await client.close()
