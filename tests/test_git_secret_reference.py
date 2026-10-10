from __future__ import annotations

import asyncio
import json
import ssl
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
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
    "git-secret+https://gitlab.cee.redhat.com/group/repo.git"
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


class _MockStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        delay: float = 0,
        started: asyncio.Event | None = None,
    ) -> None:
        self.chunks = chunks
        self.delay = delay
        self.started = started
        self.bytes_yielded = 0
        self.iterated = False
        self.closed = False

    async def __aiter__(self):
        self.iterated = True
        if self.started is not None:
            self.started.set()
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        stream: _MockStream | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.stream = stream or _MockStream([_TOKEN.encode()])
        self.error = error
        self.request = None
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if self.error is not None:
            raise self.error
        return httpx.Response(
            status_code=self.status,
            headers=self.headers,
            stream=self.stream,
            request=request,
        )

    async def aclose(self) -> None:
        self.closed = True


def _provider(
    tmp_path: Path,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    stream: _MockStream | None = None,
    error: Exception | None = None,
) -> tuple[GitSecretReferenceProvider, _MockTransport]:
    transport = _MockTransport(
        status=status,
        headers=headers,
        stream=stream,
        error=error,
    )

    provider = GitSecretReferenceProvider(
        _EmptySecrets(),
        temp_root=tmp_path,
        transport_factory=lambda: transport,
    )
    return provider, transport


def test_pointer_parser_converts_marker_to_https_and_validates_file() -> None:
    parsed = parse_git_secret_reference(_POINTER)

    assert parsed is not None
    assert parsed.file_url == (
        "https://gitlab.cee.redhat.com/api/v4/projects/group%2Frepo"
        "/repository/files/service-config%2Fharness%2Fconfig.json/raw?ref=master"
    )
    assert parsed.repository == "gitlab.cee.redhat.com/group/repo.git"
    assert parsed.ref == "master"
    assert parsed.path == "service-config/harness/config.json"


def test_pointer_parser_rejects_non_gitlab_hosts() -> None:
    pointer = _POINTER.replace(
        "gitlab.cee.redhat.com", "gitlab.cee.redhat.com.evil.example"
    )

    with pytest.raises(GitSecretReferenceError, match="only GitLab"):
        parse_git_secret_reference(pointer)


def test_organization_git_auth_accepts_https_pointer_and_rejects_ssh_pointer() -> None:
    source = {
        "kind": "git",
        "url": "https://gitlab.cee.redhat.com/group/skills.git",
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
        "git-secret+ssh://git@gitlab.cee.redhat.com/group/repo.git?ref=main&path=token",
        "git-secret+https://user:pass@gitlab.cee.redhat.com/group/repo.git?ref=main&path=token",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=main&ref=other&path=token",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=main&path=../token",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=main&path=%2e%2e%2ftoken",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=main&path=token%GG",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=../main&path=token",
        "git-secret+https://gitlab.cee.redhat.com/group/repo.git?ref=main&path=token#fragment",
        "git-secret+https://github.com/group/repo.git?ref=main&path=token",
        "git-secret+https://gitlab.cee.redhat.com.evil.example/group/repo.git?ref=main&path=token",
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
    provider, transport = _provider(tmp_path)

    assert await provider.get_secret(_POINTER) == _TOKEN
    assert str(transport.request.url) == parse_git_secret_reference(_POINTER).file_url
    assert transport.request.headers["accept-encoding"] == "identity"
    assert "authorization" not in transport.request.headers
    assert transport.stream.closed
    assert transport.closed
    assert list(tmp_path.iterdir()) == []

    settings = {}
    original_client = httpx.AsyncClient

    def record_client(**kwargs):
        settings.update(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(git_reference.httpx, "AsyncClient", record_client)
    client = git_reference._build_http_client()
    assert isinstance(settings["verify"], ssl.SSLContext)
    assert settings["verify"].verify_mode == ssl.CERT_REQUIRED
    assert settings["verify"].check_hostname
    assert settings["trust_env"] is False
    assert settings["follow_redirects"] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_home_netrc_and_git_config_are_not_used_for_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(
        "machine gitlab.cee.redhat.com login ambient-user password ambient-secret\n"
    )
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "HTTPS_PROXY", "http://ambient-user:ambient-secret@proxy.invalid"
    )
    provider, transport = _provider(tmp_path)

    assert await provider.get_secret(_POINTER) == _TOKEN
    assert "authorization" not in transport.request.headers
    assert "ambient-secret" not in str(transport.request.url)


@pytest.mark.asyncio
async def test_oversized_stream_is_closed_after_reading_only_limit_plus_one(
    tmp_path: Path,
) -> None:
    size_limit = 64 * 1024
    stream = _MockStream([b"x" * 8192 for _ in range(20)])
    provider, _transport = _provider(tmp_path, stream=stream)

    with pytest.raises(GitSecretReferenceError, match="64 KiB size limit"):
        await provider.get_secret(_POINTER)

    assert stream.bytes_yielded == size_limit + 8192
    assert stream.closed


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_reading_body(
    tmp_path: Path,
) -> None:
    stream = _MockStream([b"secret"])
    provider, _transport = _provider(
        tmp_path, headers={"Content-Length": "65537"}, stream=stream
    )

    with pytest.raises(GitSecretReferenceError, match="64 KiB size limit"):
        await provider.get_secret(_POINTER)

    assert not stream.iterated
    assert stream.closed


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_and_identity_is_requested(
    tmp_path: Path,
) -> None:
    stream = _MockStream([b"compressed-secret"])
    provider, transport = _provider(
        tmp_path,
        headers={"Content-Encoding": "gzip", "Content-Length": "17"},
        stream=stream,
    )

    with pytest.raises(GitSecretReferenceError, match="non-identity encoded"):
        await provider.get_secret(_POINTER)

    assert transport.request.headers["accept-encoding"] == "identity"
    assert not stream.iterated
    assert stream.closed


@pytest.mark.asyncio
async def test_redirect_is_rejected_without_reading_its_unbounded_body(
    tmp_path: Path,
) -> None:
    stream = _MockStream([b"x" * 1_000_000])
    provider, _transport = _provider(tmp_path, status=302, stream=stream)

    with pytest.raises(GitSecretReferenceError, match="redirect HTTP 302"):
        await provider.get_secret(_POINTER)

    assert not stream.iterated
    assert stream.bytes_yielded == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_overall_deadline_stops_a_trickling_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_reference, "_MAX_TOTAL_TIMEOUT", 0.06)
    stream = _MockStream([b"a", b"b", b"c"], delay=0.04)
    provider, _transport = _provider(tmp_path, stream=stream)
    started_at = time.monotonic()

    with pytest.raises(GitSecretReferenceError, match="overall HTTPS request deadline"):
        await provider.get_secret(_POINTER)

    assert time.monotonic() - started_at < 0.5
    assert stream.closed


@pytest.mark.asyncio
async def test_cancellation_closes_an_in_progress_response(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    stream = _MockStream([b"secret"], delay=60, started=started)
    provider, _transport = _provider(tmp_path, stream=stream)
    task = asyncio.create_task(provider.get_secret(_POINTER))

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed


@pytest.mark.asyncio
async def test_secret_file_materializes_securely_only_for_context_lifetime(
    tmp_path: Path,
) -> None:
    provider, _transport = _provider(tmp_path)

    async with provider.secret_file(_POINTER) as secret_file:
        assert secret_file is not None
        assert secret_file.read_text() == _TOKEN
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert secret_file.parent.stat().st_mode & 0o777 == 0o700

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("root_kind", ["relative", "symlink"])
async def test_secret_file_canonicalizes_relative_and_symlink_tmpdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_kind: str,
) -> None:
    actual_root = tmp_path / "temp-root"
    actual_root.mkdir()
    if root_kind == "relative":
        monkeypatch.chdir(tmp_path)
        configured_root = Path("temp-root")
    else:
        configured_root = tmp_path / "temp-root-link"
        configured_root.symlink_to(actual_root, target_is_directory=True)

    monkeypatch.setenv("TMPDIR", str(configured_root))
    monkeypatch.setattr(git_reference.tempfile, "tempdir", None)
    transport = _MockTransport()
    provider = GitSecretReferenceProvider(
        _EmptySecrets(),
        transport_factory=lambda: transport,
    )

    async with provider.secret_file(_POINTER) as secret_file:
        assert secret_file is not None
        assert secret_file.read_text() == _TOKEN
        assert secret_file.parent.parent == actual_root

    assert list(actual_root.iterdir()) == []


@pytest.mark.asyncio
async def test_get_secret_file_never_returns_a_pointer_path(
    tmp_path: Path,
) -> None:
    provider, transport = _provider(tmp_path)

    assert await provider.get_secret_file(_POINTER) is None
    assert transport.request is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "HTTP 401; anonymous read access is required"),
        (403, "HTTP 403; confirm the project allows anonymous reads"),
        (404, "HTTP 404; check the project, ref, and file path"),
        (302, "redirect HTTP 302 was rejected"),
    ],
)
async def test_resolution_failure_is_actionable_and_never_reports_missing(
    tmp_path: Path,
    status: int,
    expected: str,
) -> None:
    body = b"response body with secret payload that must not leak"
    stream = _MockStream([body])
    provider, _transport = _provider(tmp_path, status=status, stream=stream)

    with pytest.raises(GitSecretReferenceError) as error:
        await provider.get_secret(_POINTER)

    message = str(error.value)
    assert "gitlab.cee.redhat.com/group/repo.git@master" in message
    assert "anonymous HTTPS read access" in message
    assert "TLS trust" in message
    assert "network access" in message
    assert "project/ref/file path" in message
    assert expected in message
    assert body.decode() not in message
    assert "not found" not in message.lower()
    assert list(tmp_path.iterdir()) == []
    assert not stream.iterated
    assert stream.closed


@pytest.mark.asyncio
async def test_network_error_is_actionable_without_response_details(
    tmp_path: Path,
) -> None:
    provider, _opener = _provider(
        tmp_path,
        error=RuntimeError("mock failure with secret payload that must not leak"),
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

    provider, _transport = _provider(tmp_path, status=404)
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
