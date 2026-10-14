from __future__ import annotations

import logging
import re
from typing import Any

from .base import ResourceProvider

logger = logging.getLogger(__name__)


class QuadsResourceProvider(ResourceProvider):
    """ResourceProvider adapter for the QUADS bare-metal self-service system.

    Wraps the existing QuadsClient without modifying it.
    """

    provider_name = "quads"

    def __init__(self, client) -> None:
        from providers.quads import QuadsClient

        self._client: QuadsClient = client

    @classmethod
    async def from_secrets(cls, secrets_provider) -> QuadsResourceProvider:
        from providers.quads import QuadsClient

        client = await QuadsClient.from_secrets(secrets_provider)
        return cls(client)

    async def check_available(self, requirements: dict[str, Any]) -> dict[str, Any]:
        hosts = await self._client.get_available(
            model_filter=requirements.get("model_filter"),
            vendor_filter=requirements.get("nic_vendor")
            or requirements.get("vendor_filter"),
            speed_filter=requirements.get("nic_speed")
            or requirements.get("speed_filter"),
            disk_type_filter=requirements.get("disk_type")
            or requirements.get("disk_type_filter"),
            duration_hours=requirements.get("duration_hours", 36),
        )
        min_cores = self._numeric_requirement(requirements, "min_cores")
        min_memory_gb = self._numeric_requirement(
            requirements,
            "min_memory_gb",
            "min_ram_gb",
        )
        if min_cores is not None:
            hosts = [
                host
                for host in hosts
                if self._numeric_value(host.get("cores")) is not None
                and self._numeric_value(host.get("cores")) >= min_cores
            ]
        if min_memory_gb is not None:
            hosts = [
                host
                for host in hosts
                if self._numeric_value(host.get("memory_gb")) is not None
                and self._numeric_value(host.get("memory_gb")) >= min_memory_gb
            ]
        return {
            "provider": self.provider_name,
            "available_count": len(hosts),
            "options": hosts,
            "message": f"{len(hosts)} bare-metal hosts available from Scale Lab QUADS",
        }

    @staticmethod
    def _numeric_value(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            match = re.match(r"\s*(\d+(?:\.\d+)?)", value)
            if match:
                return float(match.group(1))
        return None

    @classmethod
    def _numeric_requirement(
        cls,
        requirements: dict[str, Any],
        key: str,
        alias: str | None = None,
    ) -> float | None:
        value = requirements.get(key)
        if value is None and alias:
            value = requirements.get(alias)
        if value is None:
            return None
        parsed = cls._numeric_value(value)
        if parsed is None:
            raise ValueError(f"QUADS {key} requirement must be numeric")
        return parsed

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        selection = self.validate_reservation_selection(selection)
        hostnames = selection["hostnames"]

        logger.info(f"[quads-provider] Creating assignment: {description}")
        assignment = await self._client.create_assignment(description)
        logger.info(
            f"[quads-provider] Assignment created: id={assignment['id']} "
            f"cloud={assignment['cloud_name']}"
        )

        scheduled = []
        for hostname in hostnames:
            logger.info(
                f"[quads-provider] Scheduling {hostname} -> {assignment['cloud_name']}"
            )
            sched = await self._client.schedule_host(
                assignment["cloud_name"], hostname, duration_hours=duration_hours
            )
            scheduled.append(sched)

        logger.info(
            f"[quads-provider] Waiting for validation of assignment {assignment['id']}..."
        )
        await self._client.poll_until_validated(assignment["id"])
        logger.info(f"[quads-provider] Assignment {assignment['id']} validated")

        logger.info(f"[quads-provider] Setting up SSH access to {len(hostnames)} hosts")
        ssh_result = await self._client.setup_ssh(hostnames)

        return {
            "status": "success",
            "reservation_id": str(assignment["id"]),
            "hosts": hostnames,
            "ssh_user": "root",
            "ssh_key_path": self._client.ssh_key_path,
            "lease_expiration": scheduled[0].get("end") if scheduled else None,
            "provider": self.provider_name,
            "provider_metadata": {
                "assignment_id": assignment["id"],
                "cloud_name": assignment["cloud_name"],
                "ticket": assignment.get("ticket"),
                "ssh_setup": ssh_result,
            },
            "message": f"Reserved {len(hostnames)} hosts via QUADS",
        }

    async def get_reservation_status(
        self, reservation_id: str, provider_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        status = await self._client.get_assignment_status(int(reservation_id))
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "ready": status.get("validated", False),
            "details": status,
        }

    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        assignment_id = provider_metadata.get("assignment_id") or int(reservation_id)
        result = await self._client.terminate_assignment(assignment_id)
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "status": "terminated",
            "details": result,
        }

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        return await self._client.setup_ssh(hosts)

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        return await self._client.cleanup_ssh_keys(hosts)

    async def close(self) -> None:
        await self._client.close()
