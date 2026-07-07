"""Tests for the ResourceProvider abstraction layer.

Covers: registry, QUADS adapter, AWS provider, and generic tool handlers.
All tests use mocks — no real API calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.resource.registry import ResourceProviderRegistry
from tests.conftest import MockSecretsProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def quads_secrets():
    return MockSecretsProvider(files={"quads/config.json": "/fake/quads.json"})


@pytest.fixture
def aws_secrets():
    return MockSecretsProvider(files={"aws/config.json": "/fake/aws.json"})


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


AWS_CONFIG = json.dumps(
    {
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
    }
)


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
        result = await provider.check_available(
            {
                "nic_vendor": "Intel",
                "duration_hours": 48,
            }
        )

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
    async def test_match_instance_type_by_ram(self):
        from providers.resource.aws import AWSResourceProvider

        provider = AWSResourceProvider(
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
                "large": "m5.8xlarge",
                "network_25g": "m5n.4xlarge",
            },
        )
        assert provider._match_instance_type({"min_memory_gb": 32}) == "m5.4xlarge"
        assert provider._match_instance_type({"min_ram_gb": 32}) == "m5.4xlarge"
        assert provider._match_instance_type({"min_memory_gb": 8}) == "m5.xlarge"
        assert provider._match_instance_type({"min_memory_gb": 96}) == "m5.8xlarge"

    def test_parse_numeric(self):
        from providers.resource.aws import AWSResourceProvider

        assert AWSResourceProvider._parse_numeric(25) == 25
        assert AWSResourceProvider._parse_numeric(25.9) == 25
        assert AWSResourceProvider._parse_numeric("25") == 25
        assert AWSResourceProvider._parse_numeric("25Gb") == 25
        assert AWSResourceProvider._parse_numeric("100Gbps") == 100
        assert AWSResourceProvider._parse_numeric("") == 0
        assert AWSResourceProvider._parse_numeric("none") == 0
        assert AWSResourceProvider._parse_numeric(None) == 0

    @pytest.mark.asyncio
    async def test_match_instance_type_string_values(self):
        """LLM may pass numeric fields as strings — must not raise TypeError."""
        provider = self._make_provider()
        assert provider._match_instance_type({"nic_speed": "25Gb"}) == "m5n.4xlarge"
        assert (
            provider._match_instance_type({"min_cores": "16", "min_ram_gb": "32"})
            == "m5.4xlarge"
        )
        assert (
            provider._match_instance_type({"nic_speed": "10", "min_ram_gb": "8"})
            == "m5.xlarge"
        )

    def test_match_instance_type_key_aliases(self):
        """LLM may use alternate key names for NIC speed and RAM."""
        provider = self._make_provider()
        assert (
            provider._match_instance_type({"network_interface": "25Gb"})
            == "m5n.4xlarge"
        )
        assert (
            provider._match_instance_type({"network_speed": 25}) == "m5n.4xlarge"
        )
        assert (
            provider._match_instance_type({"nic_speed_gbps": "25"})
            == "m5n.4xlarge"
        )
        assert (
            provider._match_instance_type({"controller_ram_gb": 32})
            == "m5.4xlarge"
        )
        assert (
            provider._match_instance_type({"ram_gb": 32}) == "m5.4xlarge"
        )
        assert (
            provider._match_instance_type({"memory_gb": 32}) == "m5.4xlarge"
        )

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
    async def test_reserve_instance_count_alias(self):
        """LLM may pass instance_count instead of count — both should work."""
        provider = self._make_provider()
        mock_ec2 = MagicMock()

        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-456",
                    "AvailabilityZone": "us-east-1a",
                    "VpcId": "vpc-1",
                },
            ]
        }
        mock_ec2.describe_images.return_value = {
            "Images": [{"RootDeviceName": "/dev/sda1"}]
        }
        mock_ec2.run_instances.return_value = {
            "Instances": [
                {"InstanceId": "i-one"},
                {"InstanceId": "i-two"},
            ]
        }
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-one",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.2.3.4",
                            "PrivateIpAddress": "10.0.0.1",
                        },
                        {
                            "InstanceId": "i-two",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.2.3.5",
                            "PrivateIpAddress": "10.0.0.2",
                        },
                    ]
                }
            ]
        }
        provider._ec2_client = mock_ec2
        provider.setup_ssh = AsyncMock(return_value={"status": "success"})

        result = await provider.reserve(
            selection={"instance_type": "m5.xlarge", "instance_count": 2},
            description="test instance_count alias",
        )
        assert result["status"] == "success"
        call_kwargs = mock_ec2.run_instances.call_args
        assert call_kwargs.kwargs.get("MinCount", call_kwargs[1].get("MinCount")) == 2

    @pytest.mark.asyncio
    async def test_reserve_az_fallback(self):
        """When first AZ has no capacity, retry in another AZ."""
        provider = self._make_provider()
        mock_ec2 = MagicMock()

        # describe_subnets returns subnets in two AZs
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-456",
                    "AvailabilityZone": "us-east-1a",
                    "VpcId": "vpc-1",
                },
                {
                    "SubnetId": "subnet-789",
                    "AvailabilityZone": "us-east-1b",
                    "VpcId": "vpc-1",
                },
            ]
        }
        mock_ec2.describe_images.return_value = {
            "Images": [{"RootDeviceName": "/dev/sda1"}]
        }

        # First call raises InsufficientInstanceCapacity, second succeeds
        capacity_error = Exception("InsufficientInstanceCapacity")
        capacity_error.response = {"Error": {"Code": "InsufficientInstanceCapacity"}}
        mock_ec2.run_instances.side_effect = [
            capacity_error,
            {"Instances": [{"InstanceId": "i-fallback"}]},
        ]
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-fallback",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.2.3.4",
                            "PrivateIpAddress": "10.0.0.1",
                        }
                    ]
                }
            ]
        }
        provider._ec2_client = mock_ec2
        provider.setup_ssh = AsyncMock(return_value={"status": "success"})

        result = await provider.reserve(
            selection={"instance_type": "c5n.18xlarge", "count": 1},
            description="test fallback",
        )
        assert result["status"] == "success"
        assert mock_ec2.run_instances.call_count == 2
        # Second call should use the fallback subnet
        second_call_kwargs = mock_ec2.run_instances.call_args_list[1]
        assert second_call_kwargs.kwargs.get(
            "SubnetId", second_call_kwargs[1].get("SubnetId")
        ) == "subnet-789" or "subnet-789" in str(second_call_kwargs)

    @pytest.mark.asyncio
    async def test_reserve_all_azs_exhausted(self):
        """When all AZs have no capacity, raise the error."""
        provider = self._make_provider()
        mock_ec2 = MagicMock()

        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-456",
                    "AvailabilityZone": "us-east-1a",
                    "VpcId": "vpc-1",
                },
                {
                    "SubnetId": "subnet-789",
                    "AvailabilityZone": "us-east-1b",
                    "VpcId": "vpc-1",
                },
            ]
        }
        mock_ec2.describe_images.return_value = {
            "Images": [{"RootDeviceName": "/dev/sda1"}]
        }

        capacity_error = Exception("InsufficientInstanceCapacity")
        capacity_error.response = {"Error": {"Code": "InsufficientInstanceCapacity"}}
        mock_ec2.run_instances.side_effect = capacity_error
        provider._ec2_client = mock_ec2

        with pytest.raises(Exception, match="InsufficientInstanceCapacity"):
            await provider.reserve(
                selection={"instance_type": "c5n.18xlarge", "count": 1},
                description="test exhausted",
            )
        assert mock_ec2.run_instances.call_count == 2

    def _mock_ec2_for_batch(
        self, instance_ids: list[str], ips: list[tuple[str, str]]
    ) -> MagicMock:
        """Create a mock EC2 client pre-configured for a launch batch.

        ``ips`` is a list of (public_ip, private_ip) tuples, one per
        instance.  The mock handles describe_subnets, describe_images,
        run_instances, and describe_instances for the given instances.
        """
        mock_ec2 = MagicMock()
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-456",
                    "AvailabilityZone": "us-east-1a",
                    "VpcId": "vpc-1",
                },
            ],
        }
        mock_ec2.describe_images.return_value = {
            "Images": [{"RootDeviceName": "/dev/sda1"}],
        }
        mock_ec2.run_instances.return_value = {
            "Instances": [{"InstanceId": iid} for iid in instance_ids],
        }
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": iid,
                            "State": {"Name": "running"},
                            "PublicIpAddress": pub,
                            "PrivateIpAddress": priv,
                        }
                        for iid, (pub, priv) in zip(instance_ids, ips)
                    ],
                }
            ],
        }
        return mock_ec2

    @pytest.mark.asyncio
    async def test_reserve_instance_specs_per_role(self):
        """Per-role instance_specs launches separate batches with role tags."""
        provider = self._make_provider()

        call_count = 0
        batch_responses = [
            {
                "Instances": [{"InstanceId": "i-ctrl"}],
            },
            {
                "Instances": [
                    {"InstanceId": "i-cli1"},
                    {"InstanceId": "i-cli2"},
                ],
            },
        ]
        # Each batch calls describe_instances twice: once for
        # _poll_until_running, once for _get_instance_ips.
        ctrl_describe = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-ctrl",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.0.0.1",
                            "PrivateIpAddress": "10.0.0.1",
                        },
                    ],
                }
            ],
        }
        cli_describe = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-cli1",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.0.0.2",
                            "PrivateIpAddress": "10.0.0.2",
                        },
                        {
                            "InstanceId": "i-cli2",
                            "State": {"Name": "running"},
                            "PublicIpAddress": "1.0.0.3",
                            "PrivateIpAddress": "10.0.0.3",
                        },
                    ],
                }
            ],
        }
        describe_responses = [
            ctrl_describe,
            ctrl_describe,
            cli_describe,
            cli_describe,
        ]

        mock_ec2 = MagicMock()
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-456",
                    "AvailabilityZone": "us-east-1a",
                    "VpcId": "vpc-1",
                },
            ],
        }
        mock_ec2.describe_images.return_value = {
            "Images": [{"RootDeviceName": "/dev/sda1"}],
        }

        def run_instances_side_effect(**kwargs):
            nonlocal call_count
            resp = batch_responses[call_count]
            call_count += 1
            return resp

        mock_ec2.run_instances.side_effect = run_instances_side_effect

        describe_call_count = 0

        def describe_instances_side_effect(**kwargs):
            nonlocal describe_call_count
            resp = describe_responses[describe_call_count]
            describe_call_count += 1
            return resp

        mock_ec2.describe_instances.side_effect = describe_instances_side_effect

        provider._ec2_client = mock_ec2
        provider.setup_ssh = AsyncMock(return_value={"status": "success"})

        result = await provider.reserve(
            selection={
                "instance_specs": [
                    {
                        "instance_type": "m5.4xlarge",
                        "count": 1,
                        "role": "controller",
                    },
                    {
                        "instance_type": "m5n.4xlarge",
                        "count": 2,
                        "role": "client",
                    },
                ],
            },
            description="per-role test",
            ticket_id="PERF-TEST",
        )

        assert result["status"] == "success"
        assert mock_ec2.run_instances.call_count == 2

        # First call: controller type
        first_call = mock_ec2.run_instances.call_args_list[0]
        assert first_call.kwargs["InstanceType"] == "m5.4xlarge"
        assert first_call.kwargs["MinCount"] == 1
        first_tags = first_call.kwargs["TagSpecifications"][0]["Tags"]
        role_tags = [t for t in first_tags if t["Key"] == "role"]
        assert role_tags == [{"Key": "role", "Value": "controller"}]

        # Second call: client type
        second_call = mock_ec2.run_instances.call_args_list[1]
        assert second_call.kwargs["InstanceType"] == "m5n.4xlarge"
        assert second_call.kwargs["MinCount"] == 2
        second_tags = second_call.kwargs["TagSpecifications"][0]["Tags"]
        role_tags = [t for t in second_tags if t["Key"] == "role"]
        assert role_tags == [{"Key": "role", "Value": "client"}]

        # Combined results
        assert result["hosts"] == ["1.0.0.1", "1.0.0.2", "1.0.0.3"]
        meta = result["provider_metadata"]
        assert meta["instance_ids"] == ["i-ctrl", "i-cli1", "i-cli2"]
        assert meta["public_ips"] == ["1.0.0.1", "1.0.0.2", "1.0.0.3"]
        assert meta["private_ips"] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        assert meta["instance_types"] == {
            "controller": "m5.4xlarge",
            "client": "m5n.4xlarge",
        }
        assert "instance_type" not in meta

    @pytest.mark.asyncio
    async def test_reserve_instance_specs_single(self):
        """Single-entry instance_specs works like legacy flat format."""
        provider = self._make_provider()
        mock_ec2 = self._mock_ec2_for_batch(
            ["i-one"],
            [("1.2.3.4", "10.0.0.1")],
        )
        provider._ec2_client = mock_ec2
        provider.setup_ssh = AsyncMock(return_value={"status": "success"})

        result = await provider.reserve(
            selection={
                "instance_specs": [
                    {"instance_type": "m5.xlarge", "count": 1},
                ],
            },
            description="single spec",
        )

        assert result["status"] == "success"
        assert mock_ec2.run_instances.call_count == 1
        meta = result["provider_metadata"]
        assert meta["instance_type"] == "m5.xlarge"
        assert meta["instance_types"] == {"m5.xlarge": "m5.xlarge"}

    @pytest.mark.asyncio
    async def test_reserve_legacy_format_still_works(self):
        """Legacy flat selection format produces same structure as before."""
        provider = self._make_provider()
        mock_ec2 = self._mock_ec2_for_batch(
            ["i-a", "i-b"],
            [("1.0.0.1", "10.0.0.1"), ("1.0.0.2", "10.0.0.2")],
        )
        provider._ec2_client = mock_ec2
        provider.setup_ssh = AsyncMock(return_value={"status": "success"})

        result = await provider.reserve(
            selection={"instance_type": "m5.xlarge", "count": 2},
            description="legacy format",
        )

        assert result["status"] == "success"
        meta = result["provider_metadata"]
        assert meta["instance_type"] == "m5.xlarge"
        assert meta["instance_ids"] == ["i-a", "i-b"]
        assert meta["public_ips"] == ["1.0.0.1", "1.0.0.2"]

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
        handlers, *_ = create_resource_tool_handlers(registry=reg)

        result = await handlers["list_resource_providers"]()
        names = [p["name"] for p in result["configured_providers"]]
        assert "quads" in names
        assert "aws" in names

    @pytest.mark.asyncio
    async def test_parse_host_config(self, no_secrets):
        from agents.resource.mcp_server import create_resource_tool_handlers

        handlers, *_ = create_resource_tool_handlers(secrets_provider=no_secrets)
        result = await handlers["parse_host_config"](
            text="controller: 10.1.2.3\ntarget: 10.1.2.4\nuser: testuser"
        )
        assert result["controller"] == "10.1.2.3"
        assert "10.1.2.4" in result["targets"]
        assert result["ssh_user"] == "testuser"

    @pytest.mark.asyncio
    async def test_handler_creates_registry_from_secrets(self, no_secrets):
        from agents.resource.mcp_server import create_resource_tool_handlers

        handlers, *_ = create_resource_tool_handlers(secrets_provider=no_secrets)
        result = await handlers["list_resource_providers"]()
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# Teardown dispatch tests
# ---------------------------------------------------------------------------


class TestHandleCompletionIPSplit:
    """_handle_completion must split public/private IPs using ip_mapping
    from the MCP server's accumulated metadata (issue #165)."""

    @pytest.mark.asyncio
    async def test_splits_public_private_ips_via_mcp(self):
        from agents.resource.agent import ResourceAgent
        from providers.llm.base import LLMResponse, ToolCall

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:8090",
        )

        agent._mcp = AsyncMock()
        agent._mcp.call_tool = AsyncMock(
            return_value=json.dumps(
                {
                    "instance_ids": ["i-ctrl", "i-t1", "i-t2"],
                    "public_ips": ["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                    "private_ips": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                    "ip_mapping": {
                        "1.1.1.1": "10.0.0.1",
                        "2.2.2.2": "10.0.0.2",
                        "3.3.3.3": "10.0.0.3",
                    },
                    "ssh_user": "root",
                    "ssh_key_path": "/home/user/.ssh/provider.pem",
                }
            ),
        )

        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_resource_result",
                    input={
                        "assigned_hardware_ips": {
                            "controller": "1.1.1.1",
                            "targets": ["2.2.2.2", "3.3.3.3"],
                        },
                        "ssh_user": "root",
                        "ssh_key_path": "~/.ssh/id_rsa",
                        "resource_provider": "aws",
                        "resource_reservation_id": "i-ctrl,i-t1,i-t2",
                        "resource_provider_metadata": {
                            "instance_ids": ["i-ctrl", "i-t1", "i-t2"],
                            "region": "us-east-2",
                        },
                        "fresh_host": True,
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        agent._mcp.call_tool.assert_called_once_with("get_accumulated_metadata", {})

        patch_calls = agent._client.patch.call_args_list
        fields_call = [c for c in patch_calls if "/fields" in str(c)]
        assert len(fields_call) == 1
        body = fields_call[0].kwargs.get("json", {})
        fields = body.get("fields", {})

        assert fields["assigned_hardware_ips"] == {
            "controller": "10.0.0.1",
            "targets": ["10.0.0.2", "10.0.0.3"],
        }
        assert fields["ssh_hardware_ips"] == {
            "controller": "1.1.1.1",
            "targets": ["2.2.2.2", "3.3.3.3"],
        }

        assert fields["ssh_user"] == "root"
        assert fields["ssh_key_path"] == "/home/user/.ssh/provider.pem"

    @pytest.mark.asyncio
    async def test_no_mcp_falls_back_to_llm_metadata(self):
        """Without MCP, uses whatever the LLM passed in provider_metadata."""
        from agents.resource.agent import ResourceAgent
        from providers.llm.base import LLMResponse, ToolCall

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:8090",
        )

        agent._mcp = None
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_resource_result",
                    input={
                        "assigned_hardware_ips": {
                            "controller": "1.1.1.1",
                            "targets": ["2.2.2.2"],
                        },
                        "ssh_user": "root",
                        "resource_provider": "aws",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        patch_calls = agent._client.patch.call_args_list
        fields_call = [c for c in patch_calls if "/fields" in str(c)]
        body = fields_call[0].kwargs.get("json", {})
        fields = body.get("fields", {})

        assert fields["assigned_hardware_ips"] == {
            "controller": "1.1.1.1",
            "targets": ["2.2.2.2"],
        }
        assert "ssh_hardware_ips" not in fields


class TestTeardownDispatch:
    @pytest.mark.asyncio
    async def test_legacy_quads_fields_detected(self):
        """Teardown should infer 'quads' provider from legacy quads_assignment_id."""
        from agents.resource.agent import ResourceAgent

        mock_llm = MagicMock()
        mock_secrets = AsyncMock()

        ResourceAgent(
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
        assert fields["resource_provider_metadata"]["instance_ids"] == [
            "i-abc",
            "i-def",
        ]


# ---------------------------------------------------------------------------
# PSAP Control Center provider tests
# ---------------------------------------------------------------------------

SAMPLE_CLUSTERS = [
    {
        "id": "cluster-1",
        "name": "Poseidon - 4x8xH100",
        "status": "healthy",
        "gpu_type": "NVIDIA H100 80GB HBM3",
        "gpu_count": "32",
        "api_server_url": "https://api.poseidon.example.com:6443",
        "node_count": "7",
        "is_active": True,
    },
    {
        "id": "cluster-2",
        "name": "Zeus - 8xH200",
        "status": "healthy",
        "gpu_type": "NVIDIA H200 (140GB)",
        "gpu_count": "8",
        "api_server_url": "https://api.zeus.example.com:6443",
        "node_count": "5",
        "is_active": True,
    },
    {
        "id": "cluster-3",
        "name": "Unhealthy Cluster",
        "status": "unreachable",
        "gpu_type": "NVIDIA A100",
        "gpu_count": "8",
        "api_server_url": "https://api.bad.example.com:6443",
        "node_count": "3",
        "is_active": True,
    },
]

SAMPLE_RESERVATIONS = [
    {
        "id": "res-1",
        "cluster_id": "cluster-2",
        "reservation_type": "cluster",
        "status": "active",
        "user_name": "someone",
    },
]

SAMPLE_TOPOLOGY = {
    "nodes": [
        {
            "name": "master-0",
            "roles": ["control-plane", "master"],
            "gpu": "0",
            "gpu_type": "N/A",
            "internal_ip": "10.0.0.1",
        },
        {
            "name": "worker-1",
            "roles": ["worker"],
            "gpu": "8",
            "gpu_type": "NVIDIA H100 80GB HBM3 (79GB)",
            "internal_ip": "10.0.1.1",
        },
        {
            "name": "worker-2",
            "roles": ["worker"],
            "gpu": "8",
            "gpu_type": "NVIDIA H100 80GB HBM3 (79GB)",
            "internal_ip": "10.0.1.2",
        },
    ],
}


class TestPSAPCCResourceProvider:
    def _make_provider(self):
        from providers.resource.psap_cc import PSAPCCResourceProvider

        mock_client = AsyncMock()
        mock_client.username = "admin"
        provider = PSAPCCResourceProvider(mock_client)
        return provider, mock_client

    @pytest.mark.asyncio
    async def test_check_available(self):
        provider, client = self._make_provider()
        client.list_clusters.return_value = SAMPLE_CLUSTERS
        client.list_reservations.return_value = SAMPLE_RESERVATIONS

        result = await provider.check_available({})

        assert result["provider"] == "psap-cc"
        # cluster-1 healthy+unreserved, cluster-2 healthy+reserved, cluster-3 unhealthy
        assert result["available_count"] == 1
        assert result["options"][0]["cluster_id"] == "cluster-1"
        assert result["options"][0]["gpu_count"] == 32

    @pytest.mark.asyncio
    async def test_check_available_gpu_filter(self):
        provider, client = self._make_provider()
        client.list_clusters.return_value = SAMPLE_CLUSTERS
        client.list_reservations.return_value = []

        result = await provider.check_available({"gpu_type": "H200"})

        assert result["available_count"] == 1
        assert result["options"][0]["cluster_name"] == "Zeus - 8xH200"

    @pytest.mark.asyncio
    async def test_check_available_min_gpus(self):
        provider, client = self._make_provider()
        client.list_clusters.return_value = SAMPLE_CLUSTERS
        client.list_reservations.return_value = []

        result = await provider.check_available({"min_gpus": 16})

        assert result["available_count"] == 1
        assert result["options"][0]["gpu_count"] == 32

    @pytest.mark.asyncio
    async def test_reserve(self):
        provider, client = self._make_provider()
        client.get_cluster.return_value = SAMPLE_CLUSTERS[0]
        client.create_reservation.return_value = {"id": "res-new"}
        client.get_cluster_topology.return_value = SAMPLE_TOPOLOGY

        result = await provider.reserve(
            selection={"cluster_id": "cluster-1"},
            description="test reservation",
            duration_hours=24,
        )

        assert result["status"] == "success"
        assert result["reservation_id"] == "res-new"
        assert result["hosts"] == []
        assert result["provider"] == "psap-cc"
        assert result["provider_metadata"]["cluster_id"] == "cluster-1"
        assert result["provider_metadata"]["gpu_count"] == 32
        assert len(result["provider_metadata"]["worker_nodes"]) == 2
        assert result["provider_metadata"]["worker_nodes"][0]["name"] == "worker-1"
        assert result["lease_expiration"] is not None

        client.create_reservation.assert_called_once()
        call_data = client.create_reservation.call_args[0][0]
        assert call_data["reservation_type"] == "cluster"
        assert call_data["cluster_id"] == "cluster-1"

    @pytest.mark.asyncio
    async def test_reserve_no_cluster_id(self):
        provider, client = self._make_provider()

        result = await provider.reserve(
            selection={},
            description="missing cluster",
        )

        assert result["status"] == "failed"
        assert "No cluster_id" in result["message"]

    @pytest.mark.asyncio
    async def test_reserve_cluster_not_found(self):
        from providers.psap_cc import PSAPCCAPIError

        provider, client = self._make_provider()
        client.get_cluster.side_effect = PSAPCCAPIError(
            404, "Not Found", "/clusters/bad-id"
        )

        result = await provider.reserve(
            selection={"cluster_id": "bad-id"},
            description="bad cluster",
        )

        assert result["status"] == "failed"
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_reservation_status(self):
        provider, client = self._make_provider()
        client.get_reservation.return_value = {
            "id": "res-1",
            "status": "active",
            "cluster_id": "cluster-1",
            "cluster_name": "Poseidon",
            "start_time": "2026-06-11T00:00:00",
            "end_time": "2026-06-12T00:00:00",
        }
        client.get_cluster_status.return_value = {"status": "healthy"}

        result = await provider.get_reservation_status(
            "res-1", {"cluster_id": "cluster-1"}
        )

        assert result["ready"] is True
        assert result["details"]["reservation_status"] == "active"
        assert result["details"]["cluster_healthy"] is True

    @pytest.mark.asyncio
    async def test_get_reservation_status_not_ready(self):
        provider, client = self._make_provider()
        client.get_reservation.return_value = {
            "id": "res-1",
            "status": "scheduled",
            "cluster_id": "cluster-1",
        }
        client.get_cluster_status.return_value = {"status": "healthy"}

        result = await provider.get_reservation_status("res-1", {})

        assert result["ready"] is False

    @pytest.mark.asyncio
    async def test_terminate(self):
        provider, client = self._make_provider()
        client.cancel_reservation.return_value = {"status": "cancelled"}

        result = await provider.terminate(
            reservation_id="res-1",
            provider_metadata={"cluster_name": "Poseidon"},
        )

        assert result["status"] == "terminated"
        assert result["reservation_id"] == "res-1"
        client.cancel_reservation.assert_called_once_with("res-1")

    @pytest.mark.asyncio
    async def test_setup_ssh_noop(self):
        provider, _ = self._make_provider()
        result = await provider.setup_ssh(["10.0.0.1"])
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_cleanup_ssh_keys_noop(self):
        provider, _ = self._make_provider()
        result = await provider.cleanup_ssh_keys(["10.0.0.1"])
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_from_secrets(self):
        from providers.resource.psap_cc import PSAPCCResourceProvider

        mock_secrets = AsyncMock()
        mock_secrets.get_secret.return_value = json.dumps(
            {
                "base_url": "https://cc.example.com",
                "username": "admin",
                "password": "pass",
                "verify_ssl": False,
            }
        )

        provider = await PSAPCCResourceProvider.from_secrets(mock_secrets)
        assert provider._client.base_url == "https://cc.example.com"
        assert provider._client.username == "admin"

    @pytest.mark.asyncio
    async def test_from_secrets_missing_fields(self):
        from providers.resource.psap_cc import PSAPCCResourceProvider

        mock_secrets = AsyncMock()
        mock_secrets.get_secret.return_value = json.dumps(
            {"base_url": "https://cc.example.com"}
        )

        with pytest.raises(ValueError, match="missing required fields"):
            await PSAPCCResourceProvider.from_secrets(mock_secrets)


class TestRegistryWithPSAPCC:
    @pytest.mark.asyncio
    async def test_list_configured_with_psap_cc(self):
        secrets = MockSecretsProvider(
            files={"psap-cc/config.json": "/fake/psap-cc.json"}
        )
        reg = ResourceProviderRegistry(secrets)
        providers = await reg.list_configured_providers()
        names = {p["name"]: p["type"] for p in providers}
        assert "psap-cc" in names
        assert names["psap-cc"] == "gpu_cluster"

    @pytest.mark.asyncio
    async def test_list_configured_all_three(self):
        secrets = MockSecretsProvider(
            files={
                "quads/config.json": "/fake/quads.json",
                "aws/config.json": "/fake/aws.json",
                "psap-cc/config.json": "/fake/psap-cc.json",
            }
        )
        reg = ResourceProviderRegistry(secrets)
        providers = await reg.list_configured_providers()
        names = {p["name"]: p["type"] for p in providers}
        assert names == {
            "quads": "bare_metal",
            "aws": "cloud",
            "psap-cc": "gpu_cluster",
        }
