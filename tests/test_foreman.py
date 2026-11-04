"""Tests for the Foreman client and QUADS Foreman credential derivation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from providers import foreman as foreman_module
from providers import quads as quads_module
from providers.foreman import ForemanAPIError, ForemanClient
from providers.quads import QuadsClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _ScriptedHTTPClient:
    """Records requests and replays scripted responses in order."""

    instances: list[_ScriptedHTTPClient] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        _ScriptedHTTPClient.instances.append(self)

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return _ScriptedHTTPClient.responses.pop(0)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def scripted_http(monkeypatch: pytest.MonkeyPatch) -> type[_ScriptedHTTPClient]:
    _ScriptedHTTPClient.instances = []
    _ScriptedHTTPClient.responses = []
    monkeypatch.setattr(foreman_module, "AuditedAsyncHTTPClient", _ScriptedHTTPClient)
    return _ScriptedHTTPClient


def _client() -> ForemanClient:
    return ForemanClient(
        api_url="https://foreman.example.test/api/v2/",
        username="cloud05",
        password="lab@1234",
    )


class TestForemanClient:
    @pytest.mark.asyncio
    async def test_requests_use_basic_auth_and_normalized_url(self, scripted_http):
        scripted_http.responses = [_FakeResponse(200, {"results": []})]
        client = _client()
        await client.get_operatingsystems()
        request = scripted_http.instances[0].requests[0]
        assert request["url"] == (
            "https://foreman.example.test/api/v2/operatingsystems"
        )
        assert request["auth"] == ("cloud05", "lab@1234")

    @pytest.mark.asyncio
    async def test_error_response_raises_with_foreman_message(self, scripted_http):
        scripted_http.responses = [
            _FakeResponse(403, {"error": {"message": "Access denied"}})
        ]
        client = _client()
        with pytest.raises(ForemanAPIError) as exc_info:
            await client.get_host("host1.example.test")
        assert exc_info.value.status_code == 403
        assert "Access denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_find_operatingsystem_matches_title_loosely(self, scripted_http):
        scripted_http.responses = [
            _FakeResponse(
                200,
                {"results": [{"id": 4, "title": "RHEL 9.5"}, {"id": 5}]},
            )
        ]
        client = _client()
        entry = await client.find_operatingsystem("rhel  9.5")
        assert entry == {"id": 4, "title": "RHEL 9.5"}

    @pytest.mark.asyncio
    async def test_find_operatingsystem_returns_none_when_missing(self, scripted_http):
        scripted_http.responses = [
            _FakeResponse(200, {"results": [{"id": 4, "title": "RHEL 9.5"}]})
        ]
        client = _client()
        assert await client.find_operatingsystem("Fedora 44") is None

    @pytest.mark.asyncio
    async def test_set_host_provisioning_marks_for_build(self, scripted_http):
        scripted_http.responses = [_FakeResponse(200, {})]
        client = _client()
        await client.set_host_provisioning("host1.example.test", 4, 9, 125)
        request = scripted_http.instances[0].requests[0]
        assert request["method"] == "PUT"
        assert request["url"].endswith("/hosts/host1.example.test")
        assert request["json"] == {
            "host": {
                "operatingsystem_id": 4,
                "medium_id": 9,
                "ptable_id": 125,
                "build": True,
            }
        }

    @pytest.mark.asyncio
    async def test_get_build_flag_reads_host_build_state(self, scripted_http):
        scripted_http.responses = [
            _FakeResponse(200, {"name": "host1", "build": True}),
            _FakeResponse(200, {"name": "host1", "build": False}),
        ]
        client = _client()
        assert await client.get_build_flag("host1.example.test") is True
        assert await client.get_build_flag("host1.example.test") is False


def _quads_client(**overrides: Any) -> QuadsClient:
    kwargs: dict[str, Any] = {
        "api_host": "quads.example.test",
        "email": "quads@example.test",
        "password": "test-password",
        "owner": "test-owner",
        "ssh_key_path": "/tmp/test-key",
        "default_root_password": "test-root-password",
        **overrides,
    }
    return QuadsClient(**kwargs)


class TestQuadsForemanIntegration:
    def test_foreman_config_must_be_set_together(self):
        with pytest.raises(ValueError, match="must be set together"):
            _quads_client(foreman_api_url="https://foreman.example.test/api/v2")
        with pytest.raises(ValueError, match="must be set together"):
            _quads_client(infra_location="lab")

    def test_unconfigured_client_refuses_foreman_access(self):
        client = _quads_client()
        assert client.supports_reprovision is False
        with pytest.raises(ValueError, match="not configured"):
            client.foreman_for_assignment("cloud05", "1234")

    def test_foreman_credentials_derive_from_cloud_and_ticket(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            foreman_module, "AuditedAsyncHTTPClient", _ScriptedHTTPClient
        )
        client = _quads_client(
            foreman_api_url="https://foreman.example.test/api/v2",
            infra_location="lab",
        )
        assert client.supports_reprovision is True
        foreman = client.foreman_for_assignment("cloud05", "1234")
        assert foreman._username == "cloud05"
        assert foreman._password == "lab@1234"

    def test_foreman_access_requires_cloud_and_ticket(self):
        client = _quads_client(
            foreman_api_url="https://foreman.example.test/api/v2",
            infra_location="lab",
        )
        with pytest.raises(ValueError, match="cloud name and ticket"):
            client.foreman_for_assignment("", "1234")
        with pytest.raises(ValueError, match="cloud name and ticket"):
            client.foreman_for_assignment("cloud05", "")

    @pytest.mark.asyncio
    async def test_from_secrets_rejects_blank_foreman_fields(self):
        class _Secrets:
            async def get_secret(self, path: str) -> str:
                return json.dumps(
                    {
                        "api_host": "quads.example.test",
                        "email": "quads@example.test",
                        "password": "pw",
                        "owner": "owner",
                        "ssh_key_path": "/tmp/test-key",
                        "default_root_password": "root-pw",
                        "foreman_api_url": "",
                        "infra_location": "lab",
                    }
                )

        with pytest.raises(ValueError, match="foreman_api_url"):
            await QuadsClient.from_secrets(_Secrets())

    @pytest.mark.asyncio
    async def test_reboot_host_confirms_detached_reboot(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured: dict[str, Any] = {}

        class _FakeResult:
            returncode = 0
            stdout = b"@@rebooting\n"
            stderr = b""

        class _FakeRunner:
            async def run(self, argv: list[str], **kwargs: Any) -> _FakeResult:
                captured["argv"] = argv
                return _FakeResult()

        monkeypatch.setattr(quads_module, "AuditedSubprocessRunner", _FakeRunner)
        client = _quads_client()
        assert await client.reboot_host("host1.example.test") is True
        assert captured["argv"][0] == "ssh"
        assert "root@host1.example.test" in captured["argv"]

    @pytest.mark.asyncio
    async def test_reboot_host_fails_without_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        class _FakeResult:
            returncode = 0
            stdout = b""
            stderr = b""

        class _FakeRunner:
            async def run(self, argv: list[str], **kwargs: Any) -> _FakeResult:
                return _FakeResult()

        monkeypatch.setattr(quads_module, "AuditedSubprocessRunner", _FakeRunner)
        client = _quads_client()
        assert await client.reboot_host("host1.example.test") is False
