from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.quads import QuadsClient
from providers.resource.quads import QuadsResourceProvider


async def test_missing_key_pair_is_reported_without_generating(tmp_path, monkeypatch):
    runner = MagicMock()
    monkeypatch.setattr("providers.quads.AuditedSubprocessRunner", runner)
    key = tmp_path / "id_ed25519"
    client = QuadsClient(
        "quads.example", "user", "password", "owner", str(key), "password"
    )
    try:
        result = await client.setup_ssh(["host.example"])
        assert result["status"] == "failed"
        assert str(key) in result["message"]
        assert str(key) + ".pub" in result["message"]
        runner.assert_not_called()
    finally:
        await client.close()


async def test_missing_identity_blocks_reservation_before_assignment():
    client = MagicMock()
    client.validate_ssh_identity.side_effect = ValueError(
        "SSH identity files not found"
    )
    client.create_assignment = AsyncMock()
    provider = QuadsResourceProvider(client)
    with pytest.raises(ValueError, match="SSH identity files not found"):
        await provider.reserve(
            {"hostnames": ["host.example"], "wipe": False}, "test", ticket_id="PERF-1"
        )
    client.create_assignment.assert_not_awaited()


@pytest.mark.parametrize(("scheme", "expected"), [("https", True), ("http", False)])
async def test_quads_transport_property(scheme, expected):
    client = QuadsClient(
        "quads.example",
        "user",
        "password",
        "owner",
        "/unused",
        "password",
        api_scheme=scheme,
    )
    try:
        assert client.uses_https is expected
    finally:
        await client.close()


async def test_key_copy_is_idempotent_and_quotes_public_key(tmp_path, monkeypatch):
    import os
    import subprocess
    from types import SimpleNamespace

    captured = []
    runner = MagicMock()
    runner.run = AsyncMock(
        side_effect=lambda args, **kwargs: captured.append((args, kwargs))
        or SimpleNamespace(stdout=b"added\n", stderr=b"", returncode=0)
    )
    monkeypatch.setattr("providers.quads.AuditedSubprocessRunner", lambda: runner)
    client = QuadsClient(
        "quads.example", "user", "password", "owner", "/unused", "root-secret"
    )
    marker = tmp_path / "injected"
    public_key = f"ssh-ed25519 AAAAB3 comment'$(touch {marker})"
    try:
        assert await client._copy_ssh_key("host.example", public_key) == "added"
    finally:
        await client.close()
    args, kwargs = captured[0]
    assert args[0] == "python3"
    assert "pexpect.spawn('/usr/bin/ssh', sys.argv[1:]" in args[2]
    assert args[-2] == "root@host.example"
    assert "root-secret" not in repr(args)
    assert kwargs["stdin"] == b"root-secret\n"
    env = {**os.environ, "HOME": str(tmp_path)}
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            ["/bin/sh", "-c", args[-1]], check=True, env=env, capture_output=True
        )
        outputs.append(result.stdout.decode().strip())
    assert (tmp_path / ".ssh" / "authorized_keys").read_text().splitlines() == [
        f"{public_key} {client.PROVISIONING_KEY_COMMENT}"
    ]
    assert outputs == ["KEY_ADDED", "KEY_PRESENT"]
    assert not marker.exists()


async def test_key_copy_preserves_an_existing_plain_key(tmp_path, monkeypatch):
    import os
    import subprocess
    from types import SimpleNamespace

    captured = []
    runner = MagicMock()
    runner.run = AsyncMock(
        side_effect=lambda args, **kwargs: captured.append((args, kwargs))
        or SimpleNamespace(stdout=b"existing\n", stderr=b"", returncode=0)
    )
    monkeypatch.setattr("providers.quads.AuditedSubprocessRunner", lambda: runner)
    client = QuadsClient(
        "quads.example", "user", "password", "owner", "/unused", "root-secret"
    )
    public_key = "ssh-ed25519 AAAAB3 existing"
    keys = tmp_path / ".ssh" / "authorized_keys"
    keys.parent.mkdir(mode=0o700)
    keys.write_text(f"{public_key}\n")
    try:
        assert await client._copy_ssh_key("host.example", public_key) == "existing"
    finally:
        await client.close()
    args, _kwargs = captured[0]
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", "-c", args[-1]], check=True, env=env, capture_output=True
    )
    assert result.stdout.decode().strip() == "KEY_PRESENT"
    assert keys.read_text().splitlines() == [public_key]


async def test_key_copy_rejects_shell_metacharacters_in_hostname(monkeypatch):
    runner = MagicMock()
    monkeypatch.setattr("providers.quads.AuditedSubprocessRunner", runner)
    client = QuadsClient(
        "quads.example", "user", "password", "owner", "/unused", "password"
    )
    try:
        with pytest.raises(ValueError, match="host identity"):
            await client._copy_ssh_key("host;id", "ssh-ed25519 AAAA")
        runner.assert_not_called()
    finally:
        await client.close()


async def test_key_copy_failure_does_not_expose_remote_output(monkeypatch):
    from types import SimpleNamespace

    runner = MagicMock()
    runner.run = AsyncMock(
        return_value=SimpleNamespace(
            stdout=b"unexpected: token secret", stderr=b"root-secret", returncode=1
        )
    )
    monkeypatch.setattr("providers.quads.AuditedSubprocessRunner", lambda: runner)
    client = QuadsClient(
        "quads.example", "user", "password", "owner", "/unused", "root-secret"
    )
    try:
        result = await client._copy_ssh_key("host.example", "ssh-ed25519 AAAA")
    finally:
        await client.close()
    assert result == "failed: SSH key bootstrap failed"
    assert "secret" not in result
