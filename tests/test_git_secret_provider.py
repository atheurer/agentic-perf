"""Tests for direct Git-backed secret references."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from providers.secrets.base import SecretsBackendError
from providers.secrets.git import GitSecretsProvider, _secret_ref_parts
from providers.secrets.local import LocalSecretsProvider


def test_parse_direct_ssh_secret_reference():
    source, path = _secret_ref_parts(
        "git-secret+ssh://git@git.example.org/team/credentials.git"
        "?ref=master&path=secrets/client-token.json"
    )

    assert source.url == "ssh://git@git.example.org/team/credentials.git"
    assert source.ref == "master"
    assert source.auth_kind == "default"
    assert path == "secrets/client-token.json"


def test_parse_direct_anonymous_http_secret_reference():
    source, path = _secret_ref_parts(
        "git-secret+http://git.example.org/team/credentials.git"
        "?ref=main&path=secrets/client-token.json"
    )

    assert source.url == "http://git.example.org/team/credentials.git"
    assert source.ref == "main"
    assert source.auth_kind == "default"
    assert path == "secrets/client-token.json"


@pytest.mark.asyncio
async def test_list_uppercase_http_secret_reference_uses_git_provider(
    tmp_path, monkeypatch
):
    provider = GitSecretsProvider(fallback=LocalSecretsProvider(tmp_path))
    reference = (
        "GIT-SECRET+HTTP://git.example.org/team/credentials.git"
        "?ref=main&path=secrets/client-token.json"
    )
    resolved_references = []

    @asynccontextmanager
    async def fake_secret_file(value: str) -> AsyncIterator[Path | None]:
        resolved_references.append(value)
        yield tmp_path / "secret"

    monkeypatch.setattr(provider, "secret_file", fake_secret_file)

    assert await provider.list_secrets(reference) == [reference]
    assert resolved_references == [reference]


@pytest.mark.asyncio
async def test_list_rejects_unsupported_reserved_git_secret_scheme(tmp_path):
    provider = GitSecretsProvider(fallback=LocalSecretsProvider(tmp_path))

    with pytest.raises(SecretsBackendError, match="Invalid Git secret reference"):
        await provider.list_secrets(
            "git-secret+ftp://git.example.org/team/credentials.git?path=secret"
        )


@pytest.mark.asyncio
async def test_local_secret_name_with_question_mark_falls_back(tmp_path):
    secret_path = tmp_path / "token?draft"
    secret_path.write_text("local-value", encoding="utf-8")
    provider = GitSecretsProvider(fallback=LocalSecretsProvider(tmp_path))

    assert await provider.get_secret("token?draft") == "local-value"


@pytest.mark.asyncio
async def test_local_secret_name_with_malformed_url_authority_falls_back(tmp_path):
    secret_path = tmp_path / "http:" / "[broken"
    secret_path.parent.mkdir()
    secret_path.write_text("local-value", encoding="utf-8")
    provider = GitSecretsProvider(fallback=LocalSecretsProvider(tmp_path))

    assert await provider.get_secret("http://[broken") == "local-value"


def test_parse_direct_https_secret_reference_with_token_auth():
    source, path = _secret_ref_parts(
        "git-secret+https://git.example.org/team/credentials.git"
        "?ref=stable&path=registry/auth.json&auth_kind=https-token"
        "&auth_ref=organization/git-token&username=oauth2"
    )

    assert source.url == "https://git.example.org/team/credentials.git"
    assert source.ref == "stable"
    assert source.auth_kind == "https-token"
    assert source.secret_ref == "organization/git-token"
    assert source.username == "oauth2"
    assert path == "registry/auth.json"


@pytest.mark.parametrize(
    "reference",
    [
        "git-secret+ftp://git.example.org/team/credentials.git?path=token",
        "git-secret+http://git.example.org/team/credentials.git?path=token"
        "&auth_kind=https-token&auth_ref=organization/git-token",
        "git-secret+https://user:password@git.example.org/team/credentials.git?path=token",
        "git-secret+https://git.example.org/team/credentials.git?path=../token",
        "git-secret+https://git.example.org/team/credentials.git?path=token&path=other",
        "git-secret+https://git.example.org/team/credentials.git?path=token&extra=value",
        "git-secret+https://git.example.org/team/credentials.git?path=token"
        "&auth_ref=organization/git-token",
        "git-secret+ssh://git@git.example.org/team/credentials.git?path=token"
        "&auth_kind=https-token&auth_ref=../git-token",
    ],
)
def test_reject_invalid_git_secret_references(reference):
    with pytest.raises(SecretsBackendError, match="Invalid Git secret reference"):
        _secret_ref_parts(reference)
