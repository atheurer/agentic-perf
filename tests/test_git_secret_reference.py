from __future__ import annotations

import io
import json
import ssl
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPSHandler,
    Request,
)

import pytest

import providers.secrets.git_reference as git_reference
from providers.secrets.base import SecretsProvider
from providers.secrets.git_reference import (
    GitSecretReferenceError,
    GitSecretReferenceProvider,
    parse_git_secret_reference,
)
from providers.skills.git_source import GitSourceError, parse_git_source

_POINTER = (
    "git-secret+https://gitlab.example/group/repo.git"
    "?ref=master&path=service-config/harness/config.json"
)
_TOKEN = "mocked-git-secret-value"


class _EmptySecrets(SecretsProvider):
    async def get_secret(self, path: str) -> str | None:
        return None

    async def get_secret_file(self, path: str) -> Path | None:
        return None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return []


class _MockResponse:
    def __init__(
        self,
        content: bytes,
        *,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self._content = content
        self._position = 0
        self.read_calls: list[int] = []
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.status = status
        self.closed = False

    def __enter__(self) -> _MockResponse:
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def getcode(self) -> int:
        return self.status

    def read(self, size: int) -> bytes:
        self.read_calls.append(size)
        result = self._content[self._position : self._position + size]
        self._position += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class _MockOpener:
    def __init__(
        self,
        response: _MockResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or _MockResponse(_TOKEN.encode())
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, *, timeout: float) -> _MockResponse:
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.response


def _provider(
    tmp_path: Path,
    *,
    response: _MockResponse | None = None,
    error: Exception | None = None,
) -> tuple[GitSecretReferenceProvider, _MockOpener]:
    opener = _MockOpener(response=response, error=error)

    provider = GitSecretReferenceProvider(
        _EmptySecrets(),
        temp_root=tmp_path,
        opener_factory=lambda: opener,
    )
    return provider, opener


def test_pointer_parser_converts_marker_to_https_and_validates_file() -> None:
    parsed = parse_git_secret_reference(_POINTER)

    assert parsed is not None
    assert parsed.file_url == (
        "https://gitlab.example/api/v4/projects/group%2Frepo"
        "/repository/files/service-config%2Fharness%2Fconfig.json/raw?ref=master"
    )
    assert parsed.repository == "gitlab.example/group/repo.git"
    assert parsed.ref == "master"
    assert parsed.path == "service-config/harness/config.json"


def test_pointer_parser_rejects_non_gitlab_hosts() -> None:
    pointer = _POINTER.replace("gitlab.example", "github.com")

    with pytest.raises(GitSecretReferenceError, match="only GitLab"):
        parse_git_secret_reference(pointer)


def test_organization_git_auth_accepts_https_pointer_and_rejects_ssh_pointer() -> None:
    source = {
        "kind": "git",
        "url": "https://gitlab.example/group/skills.git",
        "ref": "main",
        "auth": {"kind": "https-token", "secret_ref": _POINTER},
    }

    assert parse_git_source(source).secret_ref == _POINTER
    source["auth"]["secret_ref"] = _POINTER.replace(
        "git-secret+https", "git-secret+ssh"
    )
    with pytest.raises(GitSourceError, match="SSH fetching is disabled"):
        parse_git_source(source)


def test_agent_secret_provider_builder_installs_git_reference_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agents import server_utils

    monkeypatch.setenv("SECRETS_BACKEND", "local")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path))
    monkeypatch.delenv("TICKET_ID", raising=False)
    monkeypatch.setattr(server_utils, "_load_vault_config", lambda: None)

    provider = server_utils.build_secrets_provider()

    assert isinstance(provider, GitSecretReferenceProvider)


def test_dispatcher_wraps_shared_and_user_ticket_providers(tmp_path: Path) -> None:
    from orchestrator.dispatcher import Dispatcher
    from providers.secrets.local import LocalSecretsProvider

    class UserStore:
        def get_user(self, _username: str) -> SimpleNamespace:
            return SimpleNamespace(username="alice", groups=[])

    dispatcher = Dispatcher(
        state_store_url="http://localhost:8090",
        llm_provider=object(),
        skill_provider=object(),
        secrets_provider=LocalSecretsProvider(tmp_path),
        user_store=UserStore(),
        secrets_root=tmp_path,
    )

    assert isinstance(
        dispatcher._get_secrets_for_ticket(None), GitSecretReferenceProvider
    )
    assert isinstance(
        dispatcher._get_secrets_for_ticket({"created_by": "alice"}),
        GitSecretReferenceProvider,
    )


@pytest.mark.parametrize(
    "pointer",
    [
        "git-secret+ssh://git@gitlab.example/group/repo.git?ref=main&path=token",
        "git-secret+https://user:pass@gitlab.example/group/repo.git?ref=main&path=token",
        "git-secret+https://gitlab.example/group/repo.git?ref=main&ref=other&path=token",
        "git-secret+https://gitlab.example/group/repo.git?ref=main&path=../token",
        "git-secret+https://gitlab.example/group/repo.git?ref=main&path=%2e%2e%2ftoken",
        "git-secret+https://gitlab.example/group/repo.git?ref=main&path=token%GG",
        "git-secret+https://gitlab.example/group/repo.git?ref=../main&path=token",
        "git-secret+https://gitlab.example/group/repo.git?ref=main&path=token#fragment",
        "git-secret+https://github.com/group/repo.git?ref=main&path=token",
    ],
)
def test_invalid_pointer_is_rejected(pointer: str) -> None:
    with pytest.raises(GitSecretReferenceError):
        parse_git_secret_reference(pointer)


@pytest.mark.asyncio
async def test_get_secret_requests_only_the_encoded_gitlab_file_over_verified_https(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, opener = _provider(tmp_path)

    assert await provider.get_secret(_POINTER) == _TOKEN
    assert opener.request.full_url == parse_git_secret_reference(_POINTER).file_url
    assert opener.request.get_header("Accept-encoding") == "identity"
    assert opener.request.get_header("Authorization") is None
    assert opener.timeout == 30
    assert opener.response.closed
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(git_reference, "getproxies", lambda: {})
    real_opener = git_reference._build_https_opener()
    assert any(
        isinstance(handler, HTTPSHandler)
        and handler._context.verify_mode == ssl.CERT_REQUIRED
        and handler._context.check_hostname
        for handler in real_opener.handlers
    )
    assert not any(
        isinstance(handler, (HTTPBasicAuthHandler, HTTPDigestAuthHandler))
        for handler in real_opener.handlers
    )


@pytest.mark.asyncio
async def test_home_netrc_and_git_config_are_not_used_for_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(
        "machine gitlab.example login ambient-user password ambient-secret\n"
    )
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    monkeypatch.setenv("HOME", str(home))
    provider, opener = _provider(tmp_path)

    assert await provider.get_secret(_POINTER) == _TOKEN
    assert opener.request.get_header("Authorization") is None
    assert "ambient-secret" not in opener.request.full_url


@pytest.mark.asyncio
async def test_oversized_stream_is_closed_after_reading_only_limit_plus_one(
    tmp_path: Path,
) -> None:
    size_limit = 64 * 1024
    response = _MockResponse(b"x" * (size_limit + 100_000))
    provider, _opener = _provider(tmp_path, response=response)

    with pytest.raises(GitSecretReferenceError, match="64 KiB size limit"):
        await provider.get_secret(_POINTER)

    assert response._position == size_limit + 1
    assert sum(response.read_calls) == size_limit + 1
    assert response.closed


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_reading_body(
    tmp_path: Path,
) -> None:
    response = _MockResponse(b"secret", headers={"Content-Length": "65537"})
    provider, _opener = _provider(tmp_path, response=response)

    with pytest.raises(GitSecretReferenceError, match="64 KiB size limit"):
        await provider.get_secret(_POINTER)

    assert response.read_calls == []
    assert response.closed


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_and_identity_is_requested(
    tmp_path: Path,
) -> None:
    response = _MockResponse(
        b"compressed-secret",
        headers={"Content-Encoding": "gzip", "Content-Length": "17"},
    )
    provider, opener = _provider(tmp_path, response=response)

    with pytest.raises(GitSecretReferenceError, match="non-identity encoded"):
        await provider.get_secret(_POINTER)

    assert opener.request.get_header("Accept-encoding") == "identity"
    assert response.read_calls == []
    assert response.closed


def test_redirect_handler_allows_only_credential_free_https_redirects() -> None:
    handler = git_reference._HTTPSOnlyRedirectHandler()
    request = Request("https://gitlab.example/api/v4/file")

    assert (
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://files.example/token"
        )
        is None
    )
    assert (
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://user:pass@files.example/token",
        )
        is None
    )
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://files.example/token"
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_credential_bearing_proxy_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git_reference,
        "getproxies",
        lambda: {"https": "http://proxy-user:proxy-secret@proxy.example:8080"},
    )

    with pytest.raises(GitSecretReferenceError, match="Credential-bearing proxies"):
        git_reference._build_https_opener()


@pytest.mark.asyncio
async def test_secret_file_materializes_securely_only_for_context_lifetime(
    tmp_path: Path,
) -> None:
    provider, _opener = _provider(tmp_path)

    async with provider.secret_file(_POINTER) as secret_file:
        assert secret_file is not None
        assert secret_file.read_text() == _TOKEN
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert secret_file.parent.stat().st_mode & 0o777 == 0o700

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_get_secret_file_never_returns_a_pointer_path(
    tmp_path: Path,
) -> None:
    provider, opener = _provider(tmp_path)

    assert await provider.get_secret_file(_POINTER) is None
    assert opener.request is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "HTTP 401; anonymous read access is required"),
        (403, "HTTP 403; confirm the project allows anonymous reads"),
        (404, "HTTP 404; check the project, ref, and file path"),
        (302, "redirect was rejected"),
    ],
)
async def test_resolution_failure_is_actionable_and_never_reports_missing(
    tmp_path: Path,
    status: int,
    expected: str,
) -> None:
    body = b"response body with secret payload that must not leak"
    error = HTTPError(_POINTER, status, "mock status", {}, io.BytesIO(body))
    provider, _opener = _provider(tmp_path, error=error)

    with pytest.raises(GitSecretReferenceError) as error:
        await provider.get_secret(_POINTER)

    message = str(error.value)
    assert "gitlab.example/group/repo.git@master" in message
    assert "anonymous HTTPS read access" in message
    assert "TLS trust" in message
    assert "network access" in message
    assert "project/ref/file path" in message
    assert expected in message
    assert body.decode() not in message
    assert "not found" not in message.lower()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_network_error_is_actionable_without_response_details(
    tmp_path: Path,
) -> None:
    provider, _opener = _provider(
        tmp_path,
        error=URLError("mock failure with secret payload that must not leak"),
    )

    with pytest.raises(GitSecretReferenceError) as error:
        await provider.get_secret(_POINTER)

    assert "GitLab HTTPS secret source" in str(error.value)
    assert "TLS trust" in str(error.value)
    assert "secret payload that must not leak" not in str(error.value)


@pytest.mark.asyncio
async def test_install_harness_tool_surfaces_git_secret_source_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agents.provisioning import server as provisioning_server

    provider, _opener = _provider(
        tmp_path,
        error=HTTPError(_POINTER, 404, "mock not found", {}, io.BytesIO(b"")),
    )
    monkeypatch.setattr(provisioning_server, "_secrets_provider", provider)

    class SkillProvider:
        async def get_all_private_config(self, _harness_name: str) -> dict:
            return {
                "install_contract": {
                    "secret_files": [
                        {
                            "secret_key": "token",
                            "remote_path": "/tmp/token",
                        }
                    ]
                },
                "secrets": {"token": _POINTER},
            }

    async def ensure_initialized() -> None:
        return None

    async def install_one(host, _name, config, _provisioning, _constraints, _branch):
        return await provisioning_server._validate_and_deploy_contract(host, config)

    monkeypatch.setattr(provisioning_server, "_skill_provider", SkillProvider())
    monkeypatch.setattr(provisioning_server, "_ensure_init", ensure_initialized)
    monkeypatch.setattr(provisioning_server, "_install_harness_one", install_one)

    result = json.loads(await provisioning_server.install_harness(["host"], "demo"))
    message = result["results"]["host"]["message"]
    assert "GitLab HTTPS secret source" in message
    assert "anonymous HTTPS read access" in message
    assert "HTTP 404" in message
