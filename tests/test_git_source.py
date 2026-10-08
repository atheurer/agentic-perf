"""Tests for organization Git source parsing and credential handling."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from providers.secrets.base import SecretsProvider
from providers.skills.git_source import (
    GitSourceError,
    _git_environment,
    parse_git_source,
    prepare_git_source,
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
            "ssh://git@git.example.org/~/team/skills.git",
            "default",
        ),
        (
            {"kind": "git", "url": "git@git.example.org:~/team/skills.git"},
            "ssh://git@git.example.org/~/team/skills.git",
            "default",
        ),
        (
            {"kind": "git", "url": "git@git.example.org:~builder/team/skills.git"},
            "ssh://git@git.example.org/~builder/team/skills.git",
            "default",
        ),
        (
            {"kind": "git", "url": "git@git.example.org:team/%2Fskills.git"},
            "ssh://git@git.example.org/~/team/%252Fskills.git",
            "default",
        ),
        (
            {"kind": "git", "url": "git@git.example.org:team/a:b@c;d.git"},
            "ssh://git@git.example.org/~/team/a%3Ab%40c%3Bd.git",
            "default",
        ),
        (
            {"kind": "git", "url": "git@git.example.org:/srv/team/skills.git"},
            "ssh://git@git.example.org/srv/team/skills.git",
            "default",
        ),
        (
            {
                "kind": "git",
                "url": "http://git.example.org/team/skills.git",
                "ref": "main",
            },
            "http://git.example.org/team/skills.git",
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
            "url": "http://user:token@git.example.org/team/skills.git",
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
        {
            "kind": "git",
            "url": "http://git.example.org/team/skills.git",
            "auth": {
                "kind": "https-token",
                "secret_ref": "organization/git-token",
            },
        },
        {"kind": "git", "url": "git@git.example.org:~"},
        {"kind": "git", "url": "git@git.example.org:~builder"},
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
async def test_pinned_git_source_uses_local_checkout_without_auth_or_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import providers.skills.git_source as git_source

    monkeypatch.setattr(git_source, "AGENTIC_PERF_HOME", tmp_path)
    source = {
        "kind": "git",
        "url": "https://git.example.org/team/skills.git",
        "ref": "main",
    }
    config = parse_git_source(source)
    checkout = (
        tmp_path
        / "organization-git"
        / "checkouts"
        / f"{config.cache_key}-"
    )
    repository = tmp_path / "working-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.org"],
        check=True,
    )
    (repository / "note.md").write_text("Pinned checkout.\n")
    subprocess.run(
        ["git", "-C", str(repository), "add", "note.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "test"],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    checkout = checkout.with_name(checkout.name + revision)
    subprocess.run(
        ["git", "clone", "-q", str(repository), str(checkout)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--detach", "-q", revision],
        check=True,
    )

    @asynccontextmanager
    async def reject_network_auth(*_args, **_kwargs):
        raise AssertionError("pinned resume must not resolve auth or fetch")
        yield {}

    monkeypatch.setattr(git_source, "_git_environment", reject_network_auth)
    prepared = await prepare_git_source(source, pinned_commit=revision)
    assert prepared.root == checkout
    assert prepared.commit == revision
    assert prepared.identity == config.identity


@pytest.mark.asyncio
async def test_cancelled_pinned_git_lock_wait_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import providers.skills.git_source as git_source

    monkeypatch.setattr(git_source, "AGENTIC_PERF_HOME", tmp_path)
    opened: list[int] = []
    original_open_descriptor = git_source.AuditedFilesystem.open_descriptor

    def record_descriptor(self, *args, **kwargs):
        descriptor = original_open_descriptor(self, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(
        git_source.AuditedFilesystem, "open_descriptor", record_descriptor
    )
    waiting = asyncio.Event()

    async def wait_until_cancelled(_descriptor: int) -> None:
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(git_source, "_acquire_lock", wait_until_cancelled)
    task = asyncio.create_task(
        prepare_git_source(
            {
                "kind": "git",
                "url": "https://git.example.org/team/skills.git",
                "ref": "main",
            },
            pinned_commit="a" * 40,
        )
    )
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


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
