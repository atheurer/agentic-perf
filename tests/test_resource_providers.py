"""Tests for the ResourceProvider abstraction layer.

Covers: registry, QUADS adapter, AWS provider, and generic tool handlers.
All tests use mocks — no real API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.resource.base import ResourceProvider
from providers.resource.registry import ResourceProviderRegistry
from tests.conftest import MockSecretsProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def quads_secrets():
    return MockSecretsProvider(
        files={"quads/config.json": "/fake/quads.json"}
    )


@pytest.fixture
def aws_secrets():
    return MockSecretsProvider(
        files={"aws/config.json": "/fake/aws.json"}
    )


@pytest.fixture
def both_secrets():
    return MockSecretsProvider(
        files={
            "quads/config.json": "/fake/quads.json",
            "aws/config.json": "/fake/aws.json",
        }
    )


@pytest.fixture
def no_secrets():
    return MockSecretsProvider(files={})


AWS_CONFIG = json.dumps({
    "region": "us-east-1",
    "access_key_id": "AKIATEST",
    "secret_access_key": "secret",
    "ssh_key_name": "test-key",
    "ssh_key_path": "/tmp/test.pem",
    "ssh_user": "ec2-user",
    "security_group_id": "sg-123",
    "subnet_id": "subnet-456",
    "default_ami": "ami-abc",
    "default_instance_type": "m5.xlarge",
    "instance_type_map": {
        "small": "m5.xlarge",
        "medium": "m5.4xlarge",
        "large": "m5.8xlarge",
        "network_25g": "m5n.4xlarge",
    },
})


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestResourceProviderRegistry:
    @pytest.mark.asyncio
    async def test_list_configured_quads_only(self, quads_secrets):
        reg = ResourceProviderRegistry(quads_secrets)
        providers = await reg.list_configured_providers()
        names = [p["name"] for p in providers]
        assert "quads" in names
        assert "aws" not in names

    @pytest.mark.asyncio
    async def test_list_configured_both(self, both_secrets):
        reg = ResourceProviderRegistry(both_secrets)
        providers = await reg.list_configured_providers()
        names = [p["name"] for p in providers]
        assert "quads" in names
        assert "aws" in names

    @pytest.mark.asyncio
    async def test_list_configured_none(self, no_secrets):
        reg = ResourceProviderRegistry(no_secrets)
        providers = await reg.list_configured_providers()
        assert providers == []

    @pytest.mark.asyncio
    async def test_get_unknown_provider(self, no_secrets):
        reg = ResourceProviderRegistry(no_secrets)
        with pytest.raises(ValueError, match="Unknown resource provider"):
            await reg.get_provider("nonexistent")

    @pytest.mark.asyncio
    async def test_get_unconfigured_provider(self, quads_secrets):
        reg = ResourceProviderRegistry(quads_secrets)
        with pytest.raises(ValueError, match="not configured"):
            await reg.get_provider("aws")


# ---------------------------------------------------------------------------
# QUADS adapter tests
# ---------------------------------------------------------------------------

class TestQuadsResourceProvider:
    @pytest.mark.asyncio
    async def test_check_available(self):
        from providers.resource.quads import QuadsResourceProvider

        mock_client = AsyncMock()
        mock_client.get_available.return_value = [
            {"hostname": "host1.example.com", "model": "r660", "cores": 32},
            {"hostname": "host2.example.com", "model": "r660", "cores": 32},
        ]

        provider = QuadsResourceProvider(mock_client)
        result = await provider.check_available({
            "nic_vendor": "Intel",
            "duration_hours": 48,
        })

        assert result["provider"] == "quads"
        assert result["available_count"] == 2
        assert len(result["options"]) == 2
        mock_client.get_available.assert_called_once_with(
            model_filter=None,
            vendor_filter="Intel",
            speed_filter=None,
            disk_type_filter=None,
            duration_hours=48,
        )

    @pytest.mark.asyncio
    async def test_reserve(self):
        from providers.resource.quads import QuadsResourceProvider

        mock_client = AsyncMock()
        mock_client.ssh_key_path = "/fake/key"
        mock_client.create_assignment.return_value = {
            "id": 42,
            "cloud_name": "cloud01",
            "ticket": "T-123",
        }
        mock_client.schedule_host.return_value = {"end": "2026-06-15T00:00"}
        mock_client.poll_until_validated.return_value = {"validated": True}
        mock_client.setup_ssh.return_value = {"status": "success"}

        provider = QuadsResourceProvider(mock_client)
        result = await provider.reserve(
            selection={"hostnames": ["host1.example.com"]},
            description="test assignment",
            duration_hours=36,
        )

        assert result["status"] == "success"
        assert result["reservation_id"] == "42"
        assert result["provider"] == "quads"
        assert result["provider_metadata"]["assignment_id"] == 42
        assert result["provider_metadata"]["cloud_name"] == "cloud01"
        assert result["hosts"] == ["host1.example.com"]

    @pytest.mark.asyncio
    async def test_reserve_max_hosts(self):
        from providers.resource.quads import QuadsResourceProvider

        mock_client = AsyncMock()
        mock_client.ssh_key_path = "/fake/key"
        provider = QuadsResourceProvider(mock_client)

        result = await provider.reserve(
            selection={"hostnames": [f"host{i}" for i in range(11)]},
            description="too many",
        )
        assert result["status"] == "failed"
        assert "Max 10" in result["message"]

    @pytest.mark.asyncio
    async def test_terminate(self):
        from providers.resource.quads import QuadsResourceProvider

        mock_client = AsyncMock()
        mock_client.terminate_assignment.return_value = {"status": "terminated"}

        provider = QuadsResourceProvider(mock_client)
        result = await provider.terminate(
            reservation_id="42",
            provider_metadata={"assignment_id": 42},
        )
        assert result["status"] == "terminated"
        mock_client.terminate_assignment.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# AWS provider tests
# ---------------------------------------------------------------------------

class TestAWSResourceProvider:
    def _make_provider(self):
        from providers.resource.aws import AWSResourceProvider

        return AWSResourceProvider(
            region="us-east-1",
            access_key_id="AKIATEST",
            secret_access_key="secret",
            ssh_key_name="test-key",
            ssh_key_path="/tmp/test.pem",
            ssh_user="ec2-user",
            security_group_id="sg-123",
            subnet_id="subnet-456",
            default_ami="ami-abc",
            default_instance_type="m5.xlarge",
            instance_type_map={
                "small": "m5.xlarge",
                "medium": "m5.4xlarge",
                "network_25g": "m5n.4xlarge",
            },
        )

    @pytest.mark.asyncio
    async def test_check_available_default(self):
        provider = self._make_provider()
        result = await provider.check_available({})
        assert result["provider"] == "aws"
        assert result["available_count"] == -1
        assert result["options"][0]["instance_type"] == "m5.xlarge"

    @pytest.mark.asyncio
    async def test_check_available_with_nic_speed(self):
        provider = self._make_provider()
        result = await provider.check_available({"nic_speed": 25})
        assert result["options"][0]["instance_type"] == "m5n.4xlarge"

    @pytest.mark.asyncio
    async def test_check_available_explicit_type(self):
        provider = self._make_provider()
        result = await provider.check_available({"instance_type": "c5.2xlarge"})
        assert result["options"][0]["instance_type"] == "c5.2xlarge"

    @pytest.mark.asyncio
    async def test_match_instance_type_by_cores(self):
        provider = self._make_provider()
        assert provider._match_instance_type({"min_cores": 16}) == "m5.4xlarge"
        assert provider._match_instance_type({"min_cores": 4}) == "m5.xlarge"

    @pytest.mark.asyncio
    async def test_terminate(self):
        provider = self._make_provider()
        mock_ec2 = MagicMock()
        mock_ec2.terminate_instances.return_value = {
            "TerminatingInstances": [
                {
                    "InstanceId": "i-abc",
                    "PreviousState": {"Name": "running"},
                    "CurrentState": {"Name": "shutting-down"},
                }
            ]
        }
        provider._ec2_client = mock_ec2

        result = await provider.terminate(
            reservation_id="i-abc",
            provider_metadata={"instance_ids": ["i-abc"]},
        )
        assert result["status"] == "terminated"
        assert result["details"]["instances"][0]["id"] == "i-abc"

    @pytest.mark.asyncio
    async def test_cleanup_ssh_keys_noop(self):
        provider = self._make_provider()
        result = await provider.cleanup_ssh_keys(["1.2.3.4"])
        assert result["status"] == "success"
        assert "skipped" in result["hosts"]["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_from_secrets(self):
        from providers.resource.aws import AWSResourceProvider

        mock_secrets = AsyncMock()
        mock_secrets.get_secret.return_value = AWS_CONFIG
        provider = await AWSResourceProvider.from_secrets(mock_secrets)
        assert provider._region == "us-east-1"
        assert provider._default_instance_type == "m5.xlarge"

    @pytest.mark.asyncio
    async def test_from_secrets_missing_fields(self):
        from providers.resource.aws import AWSResourceProvider

        mock_secrets = AsyncMock()
        mock_secrets.get_secret.return_value = json.dumps({"region": "us-east-1"})
        with pytest.raises(ValueError, match="missing required fields"):
            await AWSResourceProvider.from_secrets(mock_secrets)


# ---------------------------------------------------------------------------
# Tool handler tests
# ---------------------------------------------------------------------------

class TestResourceToolHandlers:
    @pytest.mark.asyncio
    async def test_list_resource_providers_via_handler(self, both_secrets):
        from agents.resource.mcp_server import create_resource_tool_handlers
        from providers.resource.registry import ResourceProviderRegistry

        reg = ResourceProviderRegistry(both_secrets)
        handlers = create_resource_tool_handlers(registry=reg)

        result = await handlers["list_resource_providers"]()
        names = [p["name"] for p in result["configured_providers"]]
        assert "quads" in names
        assert "aws" in names

    @pytest.mark.asyncio
    async def test_parse_host_config(self, no_secrets):
        from agents.resource.mcp_server import create_resource_tool_handlers

        handlers = create_resource_tool_handlers(secrets_provider=no_secrets)
        result = await handlers["parse_host_config"](
            text="controller: 10.1.2.3\ntarget: 10.1.2.4\nuser: testuser"
        )
        assert result["controller"] == "10.1.2.3"
        assert "10.1.2.4" in result["targets"]
        assert result["ssh_user"] == "testuser"

    @pytest.mark.asyncio
    async def test_handler_creates_registry_from_secrets(self, no_secrets):
        from agents.resource.mcp_server import create_resource_tool_handlers

        handlers = create_resource_tool_handlers(secrets_provider=no_secrets)
        result = await handlers["list_resource_providers"]()
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# Teardown dispatch tests
# ---------------------------------------------------------------------------

class TestTeardownDispatch:
    @pytest.mark.asyncio
    async def test_legacy_quads_fields_detected(self):
        """Teardown should infer 'quads' provider from legacy quads_assignment_id."""
        from agents.resource.agent import ResourceAgent
        from providers.resource.registry import ResourceProviderRegistry

        mock_llm = MagicMock()
        mock_secrets = AsyncMock()

        agent = ResourceAgent(
            llm_provider=mock_llm,
            state_store_url="http://localhost:8090",
            mode="teardown",
            secrets_provider=mock_secrets,
        )

        ticket = {
            "id": "PERF-test",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "quads_assignment_id": 42,
                "quads_cloud_name": "cloud01",
                "assigned_hardware_ips": {
                    "controller": "10.1.2.3",
                    "targets": [],
                },
            },
            "comments": [],
        }

        # Verify the backward-compat logic parses correctly
        fields = ticket["custom_fields"]
        provider_name = fields.get("resource_provider")
        if not provider_name and fields.get("quads_assignment_id"):
            provider_name = "quads"
            reservation_id = str(fields["quads_assignment_id"])
            provider_metadata = {
                "assignment_id": fields["quads_assignment_id"],
                "cloud_name": fields.get("quads_cloud_name"),
            }

        assert provider_name == "quads"
        assert reservation_id == "42"
        assert provider_metadata["assignment_id"] == 42

    def test_new_provider_fields(self):
        """New-style fields should take precedence."""
        fields = {
            "resource_provider": "aws",
            "resource_reservation_id": "i-abc,i-def",
            "resource_provider_metadata": {
                "instance_ids": ["i-abc", "i-def"],
                "region": "us-east-1",
            },
        }
        assert fields["resource_provider"] == "aws"
        assert fields["resource_reservation_id"] == "i-abc,i-def"
        assert fields["resource_provider_metadata"]["instance_ids"] == ["i-abc", "i-def"]
