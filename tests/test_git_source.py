"""Tests for organization Git source parsing and credential handling."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from providers.secrets.base import SecretsProvider
from providers.skills.git_source import (
    GitSourceError,
    _git_environment,
    parse_git_source,
)


class _FileSecretsProvider(SecretsProvider):
    def __init__(self, secret_path: Path) -> None:
        self.secret_path = secret_path

    async def get_secret(self, path: str) -> str | None:
        return None

    async def get_secret_file(self, path: str) -> Path | None:
        return self.secret_path if path == "organization/git-token" else None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return ["organization/git-token"]


@pytest.mark.parametrize(
    ("source", "expected_url", "expected_auth"),
    [
        (
            {
                "kind": "git",
                "url": "ssh://git@git.example.org/team/skills.git",
                "ref": "main",
            },
            "ssh://git@git.example.org/team/skills.git",
            "default",
        ),
        (
            {
                "kind": "git",
                "url": "git@git.example.org:team/skills.git",
                "ref": "release/2026.10",
            },
            "ssh://git@git.example.org/team/skills.git",
            "default",
        ),
        (
            {
                "kind": "git",
                "url": "https://git.example.org/team/skills.git",
                "auth": {
                    "kind": "https-token",
                    "secret_ref": "organization/git-token",
                },
            },
            "https://git.example.org/team/skills.git",
            "https-token",
        ),
    ],
)
def test_parse_supported_git_sources(source, expected_url, expected_auth):
    parsed = parse_git_source(source)

    assert parsed.url == expected_url
    assert parsed.auth_kind == expected_auth


@pytest.mark.parametrize(
    "source",
    [
        {
            "kind": "git",
            "url": "https://user:token@git.example.org/team/skills.git",
        },
        {
            "kind": "git",
            "url": "https://git.example.org/team/skills.git?token=secret",
        },
        {"kind": "git", "url": "file:///tmp/skills.git"},
        {
            "kind": "git",
            "url": "https://git.example.org/team/skills.git",
            "auth": {
                "kind": "https-token",
                "secret_ref": "../outside/token",
            },
        },
    ],
)
def test_reject_unsafe_git_sources(source):
    with pytest.raises(GitSourceError):
        parse_git_source(source)


@pytest.mark.asyncio
async def test_https_token_is_passed_to_git_by_secret_file(tmp_path):
    token_path = tmp_path / "git-token"
    token_path.write_text("test-token-value\n", encoding="utf-8")
    token_path.chmod(0o600)
    source = parse_git_source(
        {
            "kind": "git",
            "url": "https://git.example.org/team/skills.git",
            "auth": {
                "kind": "https-token",
                "secret_ref": "organization/git-token",
            },
        }
    )

    async with _git_environment(source, _FileSecretsProvider(token_path)) as env:
        assert env["GIT_ASKPASS_REQUIRE"] == "force"
        assert env["AGENTIC_PERF_GIT_TOKEN_FILE"] == str(token_path)
        assert "test-token-value" not in repr(env)
        assert shlex.split(env["GIT_ASKPASS"]) == [
            sys.executable,
            str(Path(__file__).parents[1] / "providers/skills/git_askpass.py"),
        ]


@pytest.mark.asyncio
async def test_ssh_private_key_uses_restricted_identity_file(tmp_path):
    key_path = tmp_path / "git-key"
    key_path.write_text("test-private-key", encoding="utf-8")
    key_path.chmod(0o600)
    source = parse_git_source(
        {
            "kind": "git",
            "url": "ssh://git@git.example.org/team/skills.git",
            "auth": {
                "kind": "ssh-key-secret",
                "secret_ref": "organization/git-token",
            },
        }
    )

    async with _git_environment(source, _FileSecretsProvider(key_path)) as env:
        ssh_command = shlex.split(env["GIT_SSH_COMMAND"])
        assert "IdentitiesOnly=yes" in ssh_command
        assert "StrictHostKeyChecking=yes" in ssh_command
        assert ssh_command[ssh_command.index("-i") + 1] == str(key_path)
        assert "test-private-key" not in repr(env)


@pytest.mark.asyncio
async def test_reject_group_readable_git_auth_secret(tmp_path):
    token_path = tmp_path / "git-token"
    token_path.write_text("test-token-value", encoding="utf-8")
    token_path.chmod(0o640)
    source = parse_git_source(
        {
            "kind": "git",
            "url": "https://git.example.org/team/skills.git",
            "auth": {
                "kind": "https-token",
                "secret_ref": "organization/git-token",
            },
        }
    )

    with pytest.raises(GitSourceError, match="authentication is unavailable"):
        async with _git_environment(source, _FileSecretsProvider(token_path)):
            pytest.fail("unsafe credential file should not be yielded to Git")
