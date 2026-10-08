from __future__ import annotations

import logging
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
        return {
            "provider": self.provider_name,
            "available_count": len(hosts),
            "options": hosts,
            "message": f"{len(hosts)} bare-metal hosts available from Scale Lab QUADS",
        }

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        hostnames = selection["hostnames"]
        if len(set(hostnames)) != len(hostnames):
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "root",
                "ssh_key_path": self._client.ssh_key_path,
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": "QUADS reservation requires distinct hostnames",
            }
        if len(hostnames) > 10:
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "root",
                "ssh_key_path": self._client.ssh_key_path,
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": "Max 10 hosts per QUADS assignment",
            }

        os_name = selection.get("os")
        if os_name is not None and (
            not isinstance(os_name, str) or not os_name.strip()
        ):
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "root",
                "ssh_key_path": self._client.ssh_key_path,
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": "QUADS OS must be a non-empty operating-system title",
            }

        wipe = selection.get("wipe", True)
        if not isinstance(wipe, bool):
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "root",
                "ssh_key_path": self._client.ssh_key_path,
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": "QUADS wipe choice must be a boolean",
            }

        logger.info(f"[quads-provider] Creating assignment: {description}")
        assignment_options = {}
        if os_name:
            assignment_options["os_name"] = os_name.strip()
        if "wipe" in selection:
            assignment_options["wipe"] = wipe
        assignment = await self._client.create_assignment(
            description, **assignment_options
        )
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
                "ostype": assignment.get("ostype"),
                "wipe": wipe,
                "ssh_setup": ssh_result,
            },
            "message": (
                f"Reserved {len(hostnames)} hosts via QUADS"
                + (
                    f" with {assignment['ostype']}"
                    if assignment.get("ostype")
                    else ""
                )
                + (" (wiped)" if wipe else " (not wiped)")
            ),
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
