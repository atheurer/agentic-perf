from __future__ import annotations

import json
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
        self._eligible_hostnames_by_requirement: list[set[str]] | None = None
        self._availability_context_key: str | None = None

    @staticmethod
    def _normalize_os_name(value: str) -> str:
        return " ".join(value.split()).casefold()

    async def _resolve_os_type(self, requested_os: str) -> str:
        available_os = await self._client.get_os_list()
        for entry in available_os:
            if not isinstance(entry, dict):
                continue
            os_name = (
                entry.get("Title")
                or entry.get("name")
                or entry.get("title")
                or entry.get("os")
            )
            if not isinstance(os_name, str):
                continue
            if self._normalize_os_name(os_name) == self._normalize_os_name(
                requested_os
            ):
                return os_name
        available_names = sorted(
            str(
                entry.get("Title")
                or entry.get("name")
                or entry.get("title")
                or entry.get("os")
            )
            for entry in available_os
            if isinstance(entry, dict)
            and isinstance(
                entry.get("Title")
                or entry.get("name")
                or entry.get("title")
                or entry.get("os"),
                str,
            )
        )
        raise ValueError(
            f"Requested QUADS OS {requested_os!r} is unavailable. "
            f"Available OS choices: {', '.join(available_names) or '(none returned)'}"
        )

    @staticmethod
    def _context_key(ticket_context: dict[str, Any]) -> str:
        return json.dumps(
            ticket_context, sort_keys=True, separators=(",", ":"), default=str
        )

    def required_hosts_for_availability(
        self,
        requested_hosts: list[dict[str, Any]] | None,
        ticket_hosts: list[dict[str, Any]] | None,
        ticket_context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if isinstance(ticket_hosts, list):
            managed = [
                host
                for host in ticket_hosts
                if isinstance(host, dict) and not host.get("host")
            ]
            if managed:
                return managed
        return requested_hosts

    def availability_recommendation(
        self,
        requirement: dict[str, Any],
        result: dict[str, Any],
        ticket_context: dict[str, Any],
    ) -> dict[str, Any]:
        recommendation = super().availability_recommendation(
            requirement, result, ticket_context
        )
        recommendation["options"] = result.get("options", [])
        return recommendation

    def record_availability_context(
        self,
        results: list[dict[str, Any]],
        ticket_context: dict[str, Any],
    ) -> None:
        self._eligible_hostnames_by_requirement = [
            {
                str(option.get("hostname") or option.get("host")).casefold()
                for option in result.get("options", [])
                if isinstance(option, dict)
                and (option.get("hostname") or option.get("host"))
            }
            for result in results
        ]
        self._availability_context_key = self._context_key(ticket_context)

    def validate_reservation_selection(
        self,
        selection: Any,
        *,
        required_hosts: list[dict[str, Any]] | None = None,
        eligible_hostnames_by_requirement: list[set[str]] | None = None,
        require_eligible_hostnames: bool = False,
        ticket_context: dict[str, Any] | None = None,
        preflight: bool = False,
    ) -> dict[str, Any]:
        eligible = eligible_hostnames_by_requirement
        require_eligible = require_eligible_hostnames or preflight
        if preflight:
            context_key = (
                self._context_key(ticket_context)
                if ticket_context is not None
                else None
            )
            eligible = (
                self._eligible_hostnames_by_requirement
                if context_key == self._availability_context_key
                else None
            )
        return super().validate_reservation_selection(
            selection,
            required_hosts=required_hosts,
            eligible_hostnames_by_requirement=eligible,
            require_eligible_hostnames=require_eligible,
            ticket_context=ticket_context,
            preflight=False,
        )

    async def prepare_auto_reservation_selection(
        self,
        selection: Any,
        *,
        required_hosts: list[dict[str, Any]] | None,
        ticket_context: dict[str, Any],
    ) -> dict[str, Any]:
        host_requirements = [
            host
            for host in (required_hosts or [])
            if isinstance(host, dict) and not host.get("host")
        ]
        discovery_requirements = host_requirements or [{}]
        results = [
            await self.check_available(requirement)
            for requirement in discovery_requirements
        ]
        self.record_availability_context(results, ticket_context)
        return self.validate_reservation_selection(
            selection,
            required_hosts=required_hosts,
            ticket_context=ticket_context,
            preflight=True,
        )

    @classmethod
    async def from_secrets(
        cls,
        secrets_provider,
        *,
        organization_config: dict[str, Any] | None = None,
    ) -> QuadsResourceProvider:
        from providers.quads import QuadsClient

        client = await QuadsClient.from_secrets(
            secrets_provider,
            organization_config=organization_config,
        )
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
        os_type = (
            await self._resolve_os_type(selection["os"])
            if selection.get("os") is not None
            else None
        )

        ticket_key = ticket_id.strip() if isinstance(ticket_id, str) else ""
        if not ticket_key:
            raise ValueError("QUADS reservation requires an Agentic Perf ticket key")
        logger.info("[quads-provider] Creating assignment for ticket: %s", ticket_key)
        assignment = await self._client.create_assignment(
            ticket_key, os_type=os_type, wipe=selection["wipe"]
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
                "os": os_type,
                "wipe": selection["wipe"],
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
