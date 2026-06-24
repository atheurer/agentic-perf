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
        root_volume_gb: int = 50,
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
        self._default_root_volume_gb = root_volume_gb
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
            root_volume_gb=config.get("root_volume_gb", 50),
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

    async def check_available(self, requirements: dict[str, Any]) -> dict[str, Any]:
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

    async def _get_fallback_subnets(self, ec2, preferred_subnet: str) -> list[str]:
        """Return subnets across AZs, starting with the preferred one."""
        response = await asyncio.to_thread(
            ec2.describe_subnets,
            Filters=[
                {
                    "Name": "vpc-id",
                    "Values": [await self._get_vpc_id(ec2, preferred_subnet)],
                },
            ],
        )
        subnets = [preferred_subnet]
        seen_azs = set()
        # Get the AZ of the preferred subnet so we skip duplicates
        for s in response["Subnets"]:
            if s["SubnetId"] == preferred_subnet:
                seen_azs.add(s["AvailabilityZone"])
                break
        for s in response["Subnets"]:
            if (
                s["SubnetId"] != preferred_subnet
                and s["AvailabilityZone"] not in seen_azs
            ):
                subnets.append(s["SubnetId"])
                seen_azs.add(s["AvailabilityZone"])
        return subnets

    async def _get_vpc_id(self, ec2, subnet_id: str) -> str:
        response = await asyncio.to_thread(ec2.describe_subnets, SubnetIds=[subnet_id])
        return response["Subnets"][0]["VpcId"]

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        ec2 = self._get_ec2_client()
        instance_type = selection.get("instance_type", self._default_instance_type)
        ami = selection.get("ami", self._default_ami)
        count = selection.get("count", selection.get("instance_count", 1))
        root_volume_gb = selection.get("root_volume_gb", self._default_root_volume_gb)

        logger.info(
            f"[aws-provider] Launching {count}x {instance_type} "
            f"(AMI: {ami}, root_volume: {root_volume_gb}GB, region: {self._region})"
        )

        if ticket_id:
            instance_name = f"agentic-perf-{ticket_id}"
        else:
            instance_name = f"agentic-perf-{description[:50]}"

        tags = [
            {"Key": "Name", "Value": instance_name},
            {"Key": "agentic-perf", "Value": "true"},
            {"Key": "Description", "Value": description[:255]},
        ]
        if ticket_id:
            tags.append({"Key": "ticket-id", "Value": ticket_id})

        ami_info = await asyncio.to_thread(ec2.describe_images, ImageIds=[ami])
        root_device = ami_info["Images"][0].get("RootDeviceName", "/dev/sda1")

        run_kwargs: dict[str, Any] = {
            "ImageId": ami,
            "InstanceType": instance_type,
            "KeyName": self._ssh_key_name,
            "MinCount": count,
            "MaxCount": count,
            "SecurityGroupIds": [self._security_group_id],
            "SubnetId": self._subnet_id,
            "BlockDeviceMappings": [
                {
                    "DeviceName": root_device,
                    "Ebs": {
                        "VolumeSize": root_volume_gb,
                        "VolumeType": "gp3",
                    },
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": tags,
                }
            ],
        }

        subnets = await self._get_fallback_subnets(ec2, self._subnet_id)
        response = None
        last_error = None
        for subnet_id in subnets:
            run_kwargs["SubnetId"] = subnet_id
            try:
                response = await asyncio.to_thread(ec2.run_instances, **run_kwargs)
                logger.info(f"[aws-provider] Launched in subnet {subnet_id}")
                break
            except Exception as e:
                error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if error_code == "InsufficientInstanceCapacity":
                    logger.warning(
                        f"[aws-provider] No capacity in subnet {subnet_id}, "
                        f"trying next AZ..."
                    )
                    last_error = e
                    continue
                raise

        if response is None:
            raise last_error or RuntimeError(
                f"No capacity for {count}x {instance_type} in any AZ in {self._region}"
            )

        instance_ids = [i["InstanceId"] for i in response["Instances"]]
        logger.info(f"[aws-provider] Launched instances: {instance_ids}")

        # AWS eventual consistency: instance IDs may not be findable immediately
        await asyncio.sleep(5)
        await self._poll_until_running(ec2, instance_ids)
        ips = await self._get_instance_ips(ec2, instance_ids)
        public_ips = ips["public"]
        private_ips = ips["private"]

        # Bootstrap root SSH via the public IPs (orchestrator → instance)
        await self.setup_ssh(public_ips)

        return {
            "status": "success",
            "reservation_id": ",".join(instance_ids),
            "hosts": public_ips,
            "ssh_user": "root",
            "ssh_key_path": self._ssh_key_path,
            "lease_expiration": None,
            "provider": self.provider_name,
            "provider_metadata": {
                "instance_ids": instance_ids,
                "region": self._region,
                "instance_type": instance_type,
                "ami": ami,
                "cloud_login_user": self._ssh_user,
                "public_ips": public_ips,
                "private_ips": private_ips,
                "ip_mapping": dict(zip(public_ips, private_ips)),
            },
            "message": f"Launched {count}x {instance_type} in {self._region}",
        }

    async def _poll_until_running(
        self, ec2, instance_ids: list[str], interval: int = 15, timeout: int = 300
    ) -> None:
        logger.info("[aws-provider] Waiting for instances to reach 'running' state...")
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

        raise TimeoutError(f"EC2 instances {instance_ids} not running after {timeout}s")

    async def _get_instance_ips(
        self, ec2, instance_ids: list[str]
    ) -> dict[str, list[str]]:
        """Return both public and private IPs for instances."""
        response = await asyncio.to_thread(
            ec2.describe_instances, InstanceIds=instance_ids
        )
        public_ips = []
        private_ips = []
        for reservation in response["Reservations"]:
            for inst in reservation["Instances"]:
                pub = inst.get("PublicIpAddress")
                priv = inst.get("PrivateIpAddress")
                if pub:
                    public_ips.append(pub)
                if priv:
                    private_ips.append(priv)
        return {"public": public_ips, "private": private_ips}

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
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-i",
                    self._ssh_key_path,
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
        instance_ids = provider_metadata.get("instance_ids", reservation_id.split(","))
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
        """Bootstrap root SSH access on cloud instances.

        Connects as the cloud login user (e.g. ec2-user), uses sudo to
        enable root login via SSH, and installs the provisioning key for
        root. After this, all downstream agents can SSH as root directly.
        """
        await self._wait_for_ssh(hosts)

        results: dict[str, str] = {}
        pubkey = await self._get_public_key()

        for host in hosts:
            try:
                await self._enable_root_ssh(host, pubkey)
                results[host] = "root_enabled"
            except Exception as e:
                logger.warning(f"[aws-provider] Root bootstrap failed on {host}: {e}")
                results[host] = f"failed: {e}"

        return {
            "status": "success"
            if all("root" in v for v in results.values())
            else "partial",
            "ssh_key_path": self._ssh_key_path,
            "hosts": results,
        }

    async def _get_public_key(self) -> str:
        """Get the SSH public key, deriving from private key if needed."""
        pubkey_path = Path(f"{self._ssh_key_path}.pub")
        if pubkey_path.exists():
            return pubkey_path.read_text().strip()

        # Derive public key from private key
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen",
            "-y",
            "-f",
            self._ssh_key_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to derive public key: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def _enable_root_ssh(self, host: str, pubkey: str) -> None:
        """Enable root SSH login and install our key."""
        bootstrap_cmds = [
            # RHEL10 uses sshd_config.d/ drop-ins that override the main config.
            # Remove any PermitRootLogin overrides from drop-ins first.
            "sudo sed -i '/^PermitRootLogin/d' /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true",
            "sudo sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config",
            # Ensure PermitRootLogin is set even if the sed didn't match
            "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' | sudo tee -a /etc/ssh/sshd_config > /dev/null",
            "sudo sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config",
            "sudo mkdir -p /root/.ssh",
            "sudo chmod 700 /root/.ssh",
            # Cloud AMIs put a command= restriction on root's authorized_keys
            # that blocks login. Replace the file with just our key.
            f"echo '{pubkey}' | sudo tee /root/.ssh/authorized_keys > /dev/null",
            "sudo chmod 600 /root/.ssh/authorized_keys",
            # Fix SELinux labels on root's .ssh directory
            "sudo restorecon -Rv /root/.ssh 2>/dev/null || true",
            "sudo systemctl restart sshd",
            # EC2 cgroup v2 + systemd cgroup manager triggers eBPF device
            # filter errors in podman/crun. Use cgroupfs instead.
            "sudo mkdir -p /etc/containers",
            "echo '[engine]\ncgroup_manager = \"cgroupfs\"' | sudo tee /etc/containers/containers.conf > /dev/null",
        ]
        for cmd in bootstrap_cmds:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-i",
                self._ssh_key_path,
                f"{self._ssh_user}@{host}",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Bootstrap cmd failed (exit {proc.returncode}): {cmd} — "
                    f"{stderr.decode().strip()}"
                )

        # Give sshd time to restart fully
        await asyncio.sleep(5)

        # Verify root SSH works
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-i",
            self._ssh_key_path,
            f"root@{host}",
            "echo ROOT_OK",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or b"ROOT_OK" not in stdout:
            raise RuntimeError("Root SSH verification failed after bootstrap")
        logger.info(f"[aws-provider] Root SSH enabled on {host}")

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        return {
            "status": "success",
            "hosts": {h: "skipped (cloud instance will be terminated)" for h in hosts},
        }
