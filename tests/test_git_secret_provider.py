"""Tests for direct Git-backed secret references."""

from __future__ import annotations

import pytest

from providers.secrets.base import SecretsBackendError
from providers.secrets.git import _secret_ref_parts


def test_parse_direct_ssh_secret_reference():
    source, path = _secret_ref_parts(
        "git-secret+ssh://git@git.example.org/team/credentials.git"
        "?ref=master&path=secrets/client-token.json"
    )

    assert source.url == "ssh://git@git.example.org/team/credentials.git"
    assert source.ref == "master"
    assert source.auth_kind == "default"
    assert path == "secrets/client-token.json"


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
