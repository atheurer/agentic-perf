from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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


class _MockGitRunner:
    instances: list[_MockGitRunner] = []

    def __init__(self, *, output_limit: int, failure: str | None = None) -> None:
        self.output_limit = output_limit
        self.failure = failure
        self.calls: list[dict] = []
        type(self).instances.append(self)

    async def run(self, argv: list[str], **kwargs) -> SimpleNamespace:
        command_index = 1
        while argv[command_index] == "-c":
            command_index += 2
        if argv[command_index] == "--git-dir":
            command_index += 2
        command = argv[command_index:]
        self.calls.append({"argv": argv, "command": command, **kwargs})
        if self.failure == "fetch" and "fetch" in command:
            return SimpleNamespace(
                returncode=128,
                timed_out=False,
                stdout=b"",
                stderr=b"mock failure with secret payload that must not leak",
            )
        if "fetch" in command and self.failure == "filter-warning":
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout=b"",
                stderr=b"warning: filtering not recognized by server, ignoring",
            )
        if command[:2] == ["cat-file", "--batch-all-objects"]:
            count = sum(
                call["command"][:2] == ["cat-file", "--batch-all-objects"]
                for call in self.calls
            )
            object_types = b"commit\ntree\n" if count == 1 else b"commit\ntree\nblob\n"
            result = SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout=object_types,
                stderr=b"",
            )
        elif command and command[0] == "rev-parse":
            result = SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout=b"a" * 40 + b"\n",
                stderr=b"",
            )
        elif command[:2] == ["cat-file", "blob"]:
            output = (
                b"x" * (self.output_limit + 1)
                if self.failure == "large"
                else _TOKEN.encode()
            )
            result = SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout=output[: self.output_limit],
                stderr=b"",
            )
        else:
            result = SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout=b"",
                stderr=b"",
            )
        return result


def _provider(
    tmp_path: Path,
    *,
    failure: str | None = None,
) -> tuple[GitSecretReferenceProvider, _MockGitRunner]:
    runners = []

    def runner_factory(*, output_limit: int) -> _MockGitRunner:
        runner = _MockGitRunner(output_limit=output_limit, failure=failure)
        runners.append(runner)
        return runner

    provider = GitSecretReferenceProvider(
        _EmptySecrets(),
        temp_root=tmp_path,
        runner_factory=runner_factory,
    )
    return provider, runners


def test_pointer_parser_converts_marker_to_https_and_validates_file() -> None:
    parsed = parse_git_secret_reference(_POINTER)

    assert parsed is not None
    assert parsed.transport_url == "https://gitlab.example/group/repo.git"
    assert parsed.repository == "gitlab.example/group/repo.git"
    assert parsed.ref == "master"
    assert parsed.path == "service-config/harness/config.json"


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
    ],
)
def test_invalid_pointer_is_rejected(pointer: str) -> None:
    with pytest.raises(GitSecretReferenceError):
        parse_git_secret_reference(pointer)


@pytest.mark.asyncio
async def test_get_secret_requests_only_the_configured_https_branch_and_blob(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_reference.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    _MockGitRunner.instances.clear()
    provider, runners = _provider(tmp_path)

    assert await provider.get_secret(_POINTER) == _TOKEN
    runner = runners[0]
    calls = runner.calls
    argv = [call["argv"] for call in calls]
    remote_add = next(command for command in argv if "remote" in command)
    fetch = next(command for command in argv if "fetch" in command)
    read_blob = next(command for command in argv if command[-2:-1] == ["blob"])

    assert remote_add[-1] == "https://gitlab.example/group/repo.git"
    assert "+refs/heads/master:refs/heads/secret-source" in fetch
    assert "--filter=blob:none" in fetch
    assert "--depth=1" in fetch
    assert read_blob[-1] == f"{'a' * 40}:service-config/harness/config.json"
    assert all("http.sslVerify=true" in command for command in argv)
    assert not any("sslVerify=false" in command for command in argv)
    assert all("credential.helper=" in command for command in argv)
    env = calls[0]["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_SSL_NO_VERIFY" not in env
    assert "GIT_ASKPASS" not in env
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_secret_file_materializes_securely_only_for_context_lifetime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_reference.shutil, "which", lambda _name: "/usr/bin/git")
    provider, _runners = _provider(tmp_path)

    async with provider.secret_file(_POINTER) as secret_file:
        assert secret_file is not None
        assert secret_file.read_text() == _TOKEN
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert secret_file.parent.stat().st_mode & 0o777 == 0o700

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_get_secret_file_never_returns_a_pointer_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_reference.shutil, "which", lambda _name: "/usr/bin/git")
    provider, runners = _provider(tmp_path)

    assert await provider.get_secret_file(_POINTER) is None
    assert runners == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fetch", "filter-warning", "large"])
async def test_resolution_failure_is_actionable_and_never_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    monkeypatch.setattr(git_reference.shutil, "which", lambda _name: "/usr/bin/git")
    provider, _runners = _provider(tmp_path, failure=failure)

    with pytest.raises(GitSecretReferenceError) as error:
        await provider.get_secret(_POINTER)

    message = str(error.value)
    assert "gitlab.example/group/repo.git@master" in message
    assert "anonymous HTTPS read access" in message
    assert "TLS trust" in message
    assert "network access" in message
    assert "ref/path" in message
    assert "secret payload that must not leak" not in message
    assert "not found" not in message.lower()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_install_harness_tool_surfaces_git_secret_source_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agents.provisioning import server as provisioning_server

    monkeypatch.setattr(git_reference.shutil, "which", lambda _name: "/usr/bin/git")
    provider, _runners = _provider(tmp_path, failure="fetch")
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
    assert "Git secret source" in message
    assert "anonymous HTTPS read access" in message
    assert "not found" not in message.lower()
