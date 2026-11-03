from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.foreman import ForemanError
from providers.foreman_config import foreman_credentials, resolve_foreman_config
from providers.resource.quads_foreman import verify_quads_ownership


def site_config():
    return {
        "sites": {
            "lab": {
                "endpoint": "https://example.test",
                "auth": {
                    "strategy": "quads",
                    "convention_verified": True,
                    "infra_location": "lab",
                },
            }
        }
    }


async def test_local_config_replaces_organization_config():
    resolver = MagicMock()
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value=json.dumps(site_config()))
    config, source = await resolve_foreman_config(resolver, secrets)
    assert config["endpoint"] == "https://example.test"
    assert source == {"source": "installation-local", "site": "lab"}
    resolver.get_runtime_config.assert_not_called()


async def test_organization_config_and_ambiguous_sites():
    resolver = MagicMock()
    resolver.has_runtime_config.return_value = True
    resolver.get_runtime_config.return_value = site_config()
    resolver.bootstrap.return_value = {
        "revision": "abc",
        "runtime_config_sources": ["org"],
    }
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value=None)
    config, source = await resolve_foreman_config(resolver, secrets, "lab")
    assert config["endpoint"] == "https://example.test"
    assert source["revision"] == "abc"
    resolver.get_runtime_config.return_value["sites"]["other"] = config
    with pytest.raises(ForemanError, match="ambiguous"):
        await resolve_foreman_config(resolver, secrets)


async def test_invalid_local_configuration_does_not_fall_back():
    resolver = MagicMock()
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value="{}")
    with pytest.raises(ForemanError):
        await resolve_foreman_config(resolver, secrets)
    resolver.get_runtime_config.assert_not_called()


async def test_quads_credentials_require_verified_ticket_and_location():
    config = site_config()["sites"]["lab"]
    creds = await foreman_credentials(
        config, None, {"cloud_name": "cloud1", "ticket": "123"}
    )
    assert creds == ("cloud1", "lab@123")
    with pytest.raises(ForemanError):
        await foreman_credentials(config, None, {"cloud_name": "cloud1"})
    config["auth"]["convention_verified"] = False
    with pytest.raises(ForemanError):
        await foreman_credentials(
            config, None, {"cloud_name": "cloud1", "ticket": "123"}
        )


async def test_verify_ownership_rejects_inactive_or_wrong_cloud():
    metadata = {"assignment_id": 7, "cloud_name": "cloud1", "ticket": "123"}
    client = MagicMock()
    client.get_foreman_assignment_identity = AsyncMock(
        return_value={
            "active": True,
            "validated": True,
            "cloud": "cloud1",
            "ticket": "123",
        }
    )
    client.get_host_cloud = AsyncMock(return_value="cloud1")
    await verify_quads_ownership(client, metadata, ["host.example"])
    client.get_host_cloud.return_value = "cloud2"
    with pytest.raises(ForemanError, match="not in"):
        await verify_quads_ownership(client, metadata, ["host.example"])
    client.get_foreman_assignment_identity.return_value["active"] = False
    with pytest.raises(ForemanError, match="not active"):
        await verify_quads_ownership(client, metadata, ["host.example"])


async def test_real_gateway_path_and_conflict(tmp_path):
    from providers.skills.gateway import OrganizationSkillResolver, SkillGatewayError

    sources = []
    for name, endpoint in (("one", "https://one.test"), ("two", "https://two.test")):
        root = tmp_path / name
        path = root / "service-config" / "service" / "foreman.json"
        path.parent.mkdir(parents=True)
        config = site_config()
        config["sites"]["lab"]["endpoint"] = endpoint
        path.write_text(json.dumps(config))
        sources.append({"id": name, "kind": "path", "path": str(root)})
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value=None)
    resolver = OrganizationSkillResolver.from_instance_config(
        {"skill_gateway": {"organization": {"sources": sources[:1]}}}
    )
    config, provenance = await resolve_foreman_config(resolver, secrets, "lab")
    assert config["endpoint"] == "https://one.test"
    assert provenance["sources"] == ["one"]
    resolver = OrganizationSkillResolver.from_instance_config(
        {"skill_gateway": {"organization": {"sources": sources}}}
    )
    with pytest.raises(SkillGatewayError, match="conflict"):
        await resolve_foreman_config(resolver, secrets, "lab")


async def test_static_secret_references_are_resolved_only_by_provider():
    config = {
        "sites": {
            "lab": {
                "endpoint": "https://example.test",
                "auth": {
                    "strategy": "secret",
                    "username_secret": "foreman/user",
                    "password_secret": "foreman/password",
                },
            }
        }
    }
    secrets = MagicMock()

    async def get_secret(path):
        return {
            "foreman/config.json": json.dumps(config),
            "foreman/user": "operator",
            "foreman/password": "credential",
        }.get(path)

    secrets.get_secret = AsyncMock(side_effect=get_secret)
    selected, source = await resolve_foreman_config(None, secrets)
    username, password = await foreman_credentials(selected, secrets, {})
    assert (username, password) == ("operator", "credential")
    assert "credential" not in repr(source)
    assert "credential" not in repr(selected)


async def test_plaintext_auth_fields_are_rejected():
    config = site_config()
    config["sites"]["lab"]["auth"] = {
        "strategy": "secret",
        "username": "operator",
        "password": "credential",
    }
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value=json.dumps(config))
    with pytest.raises(ForemanError) as error:
        await resolve_foreman_config(None, secrets)
    assert "credential" not in str(error.value)
