from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .base import ResourceProvider

logger = logging.getLogger(__name__)


class AWSResourceProvider(ResourceProvider):
    """ResourceProvider for AWS EC2 instances."""

    provider_name = "aws"

    def __init__(
        self,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        ssh_key_name: str,
        ssh_key_path: str,
        ssh_user: str,
        security_group_id: str,
        subnet_id: str,
        default_ami: str,
        default_instance_type: str,
        instance_type_map: dict[str, str] | None = None,
        session_token: str | None = None,
    ) -> None:
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._ssh_key_name = ssh_key_name
        self._ssh_key_path = str(Path(ssh_key_path).expanduser())
        self._ssh_user = ssh_user
        self._security_group_id = security_group_id
        self._subnet_id = subnet_id
        self._default_ami = default_ami
        self._default_instance_type = default_instance_type
        self._instance_type_map = instance_type_map or {}
        self._ec2_client = None

    @classmethod
    async def from_secrets(cls, secrets_provider) -> AWSResourceProvider:
        raw = await secrets_provider.get_secret("aws/config.json")
        if not raw:
            raise ValueError("AWS config not found at secrets/aws/config.json")
        config = json.loads(raw)
        required = [
            "region",
            "access_key_id",
            "secret_access_key",
            "ssh_key_name",
            "ssh_key_path",
            "ssh_user",
            "security_group_id",
            "subnet_id",
            "default_ami",
            "default_instance_type",
        ]
        missing = [k for k in required if k not in config]
        if missing:
            raise ValueError(f"AWS config missing required fields: {missing}")
        return cls(
            region=config["region"],
            access_key_id=config["access_key_id"],
            secret_access_key=config["secret_access_key"],
            ssh_key_name=config["ssh_key_name"],
            ssh_key_path=config["ssh_key_path"],
            ssh_user=config["ssh_user"],
            security_group_id=config["security_group_id"],
            subnet_id=config["subnet_id"],
            default_ami=config["default_ami"],
            default_instance_type=config["default_instance_type"],
            instance_type_map=config.get("instance_type_map"),
            session_token=config.get("session_token"),
        )

    def _get_ec2_client(self):
        if self._ec2_client is None:
            import boto3

            kwargs: dict[str, Any] = {
                "region_name": self._region,
                "aws_access_key_id": self._access_key_id,
                "aws_secret_access_key": self._secret_access_key,
            }
            if self._session_token:
                kwargs["aws_session_token"] = self._session_token
            self._ec2_client = boto3.client("ec2", **kwargs)
        return self._ec2_client

    def _match_instance_type(self, requirements: dict[str, Any]) -> str:
        """Map resource requirements to an EC2 instance type."""
        if requirements.get("instance_type"):
            return requirements["instance_type"]

        cores = requirements.get("min_cores", 0)
        nic_speed = requirements.get("nic_speed", 0)

        if nic_speed >= 100 and "network_100g" in self._instance_type_map:
            return self._instance_type_map["network_100g"]
        if nic_speed >= 25 and "network_25g" in self._instance_type_map:
            return self._instance_type_map["network_25g"]
        if cores >= 32 and "large" in self._instance_type_map:
            return self._instance_type_map["large"]
        if cores >= 16 and "medium" in self._instance_type_map:
            return self._instance_type_map["medium"]
        if "small" in self._instance_type_map:
            return self._instance_type_map["small"]

        return self._default_instance_type

    async def check_available(
        self, requirements: dict[str, Any]
    ) -> dict[str, Any]:
        recommended = self._match_instance_type(requirements)
        ami = requirements.get("ami", self._default_ami)
        count = requirements.get("count", 1)
        return {
            "provider": self.provider_name,
            "available_count": -1,
            "options": [
                {
                    "instance_type": recommended,
                    "ami": ami,
                    "region": self._region,
                    "count": count,
                }
            ],
            "message": (
                f"AWS EC2 ready — recommended instance type: {recommended}, "
                f"AMI: {ami}, region: {self._region}"
            ),
        }

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
    ) -> dict[str, Any]:
        ec2 = self._get_ec2_client()
        instance_type = selection.get("instance_type", self._default_instance_type)
        ami = selection.get("ami", self._default_ami)
        count = selection.get("count", 1)

        logger.info(
            f"[aws-provider] Launching {count}x {instance_type} "
            f"(AMI: {ami}, region: {self._region})"
        )

        response = await asyncio.to_thread(
            ec2.run_instances,
            ImageId=ami,
            InstanceType=instance_type,
            KeyName=self._ssh_key_name,
            MinCount=count,
            MaxCount=count,
            SecurityGroupIds=[self._security_group_id],
            SubnetId=self._subnet_id,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"agentic-perf-{description[:50]}",
                        },
                        {"Key": "agentic-perf", "Value": "true"},
                        {"Key": "Description", "Value": description[:255]},
                    ],
                }
            ],
        )

        instance_ids = [i["InstanceId"] for i in response["Instances"]]
        logger.info(f"[aws-provider] Launched instances: {instance_ids}")

        # AWS eventual consistency: instance IDs may not be findable immediately
        await asyncio.sleep(5)
        await self._poll_until_running(ec2, instance_ids)
        hosts = await self._get_public_ips(ec2, instance_ids)
        await self._wait_for_ssh(hosts)

        return {
            "status": "success",
            "reservation_id": ",".join(instance_ids),
            "hosts": hosts,
            "ssh_user": self._ssh_user,
            "ssh_key_path": self._ssh_key_path,
            "lease_expiration": None,
            "provider": self.provider_name,
            "provider_metadata": {
                "instance_ids": instance_ids,
                "region": self._region,
                "instance_type": instance_type,
                "ami": ami,
            },
            "message": f"Launched {count}x {instance_type} in {self._region}",
        }

    async def _poll_until_running(
        self, ec2, instance_ids: list[str], interval: int = 15, timeout: int = 300
    ) -> None:
        logger.info(
            f"[aws-provider] Waiting for instances to reach 'running' state..."
        )
        elapsed = 0
        while elapsed < timeout:
            response = await asyncio.to_thread(
                ec2.describe_instances, InstanceIds=instance_ids
            )
            states = []
            for reservation in response["Reservations"]:
                for inst in reservation["Instances"]:
                    states.append(inst["State"]["Name"])

            logger.info(
                f"[aws-provider] Instance states: {states} ({elapsed}s elapsed)"
            )
            if all(s == "running" for s in states):
                return

            await asyncio.sleep(interval)
            elapsed += interval

        raise TimeoutError(
            f"EC2 instances {instance_ids} not running after {timeout}s"
        )

    async def _get_public_ips(
        self, ec2, instance_ids: list[str]
    ) -> list[str]:
        response = await asyncio.to_thread(
            ec2.describe_instances, InstanceIds=instance_ids
        )
        ips = []
        for reservation in response["Reservations"]:
            for inst in reservation["Instances"]:
                ip = inst.get("PublicIpAddress")
                if not ip:
                    ip = inst.get("PrivateIpAddress")
                if ip:
                    ips.append(ip)
        return ips

    async def _wait_for_ssh(
        self,
        hosts: list[str],
        retries: int = 20,
        interval: int = 15,
    ) -> None:
        logger.info(f"[aws-provider] Waiting for SSH on {len(hosts)} hosts...")
        for host in hosts:
            for attempt in range(retries):
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o", "ConnectTimeout=5",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-i", self._ssh_key_path,
                    f"{self._ssh_user}@{host}",
                    "echo SSH_OK",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0 and b"SSH_OK" in stdout:
                    logger.info(f"[aws-provider] SSH ready on {host}")
                    break
                if attempt < retries - 1:
                    await asyncio.sleep(interval)
            else:
                logger.warning(
                    f"[aws-provider] SSH not ready on {host} after "
                    f"{retries * interval}s"
                )

    async def get_reservation_status(
        self, reservation_id: str, provider_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ec2 = self._get_ec2_client()
        instance_ids = reservation_id.split(",")
        response = await asyncio.to_thread(
            ec2.describe_instances, InstanceIds=instance_ids
        )
        states = {}
        for reservation in response["Reservations"]:
            for inst in reservation["Instances"]:
                states[inst["InstanceId"]] = inst["State"]["Name"]

        all_running = all(s == "running" for s in states.values())
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "ready": all_running,
            "details": {"instance_states": states},
        }

    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        ec2 = self._get_ec2_client()
        instance_ids = provider_metadata.get(
            "instance_ids", reservation_id.split(",")
        )
        logger.info(f"[aws-provider] Terminating instances: {instance_ids}")
        result = await asyncio.to_thread(
            ec2.terminate_instances, InstanceIds=instance_ids
        )
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "status": "terminated",
            "details": {
                "instances": [
                    {
                        "id": i["InstanceId"],
                        "previous_state": i["PreviousState"]["Name"],
                        "current_state": i["CurrentState"]["Name"],
                    }
                    for i in result.get("TerminatingInstances", [])
                ]
            },
        }

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        await self._wait_for_ssh(hosts)
        return {
            "status": "success",
            "ssh_key_path": self._ssh_key_path,
            "hosts": {h: "ok" for h in hosts},
        }

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        return {
            "status": "success",
            "hosts": {
                h: "skipped (cloud instance will be terminated)" for h in hosts
            },
        }
