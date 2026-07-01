"""Jumpstarter resource provider.

Manages lab hardware provisioning via Jumpstarter — lease physical
devices from a Jumpstarter controller as an alternative to QUADS/AWS
for test scenarios requiring real embedded/automotive hardware.

Uses the jumpstarter Python package directly (not MCP) for the
standard ResourceProvider lifecycle: check_available → reserve →
get_reservation_status → terminate.

Configuration via ~/.agentic-perf/secrets/jumpstarter/config.json:
    {
        "client_name": "my-client",
        "controller_endpoint": "grpc.jumpstarter.example.com:443",
        "token": "...",
        "namespace": "jumpstarter-lab",
        "default_selector": "target=ride4_sa8775p_sx_r3",
        "default_lease_duration_seconds": 7200,
        "ssh_user": "root",
        "tls_insecure": true
    }

Or uses the jmp CLI config if available at
~/.config/jumpstarter/clients/<client_name>.yaml.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from .base import ResourceProvider

logger = logging.getLogger(__name__)


class JumpstarterResourceProvider(ResourceProvider):
    """Jumpstarter lab hardware resource provider."""

    provider_name = "jumpstarter"

    def __init__(
        self,
        client_name: str,
        config_path: Path | None = None,
        namespace: str = "",
        default_selector: str = "",
        default_lease_duration: int = 7200,
        ssh_user: str = "root",
    ) -> None:
        self._client_name = client_name
        self._config_path = config_path
        self._namespace = namespace
        self._default_selector = default_selector
        self._default_duration = default_lease_duration
        self._ssh_user = ssh_user
        self._channel: Any = None
        self._service: Any = None
        self._active_leases: dict[str, Any] = {}

    @classmethod
    async def from_secrets(
        cls,
        secrets_provider: Any,
    ) -> JumpstarterResourceProvider:
        """Create from secrets provider."""
        raw = await secrets_provider.get_secret("jumpstarter/config.json")
        if not raw:
            raise ValueError(
                "Jumpstarter not configured (missing secrets/jumpstarter/config.json)"
            )
        cfg = json.loads(raw) if isinstance(raw, str) else raw

        client_name = cfg.get("client_name", "agentic-perf")

        # Check for jmp CLI config file
        cli_config = (
            Path.home() / ".config" / "jumpstarter" / "clients" / f"{client_name}.yaml"
        )
        config_path = cli_config if cli_config.exists() else None

        return cls(
            client_name=client_name,
            config_path=config_path,
            namespace=cfg.get("namespace", ""),
            default_selector=cfg.get("default_selector", ""),
            default_lease_duration=cfg.get("default_lease_duration_seconds", 7200),
            ssh_user=cfg.get("ssh_user", "root"),
        )

    async def _ensure_connected(self) -> None:
        """Lazy-connect to the Jumpstarter controller."""
        if self._service is not None:
            return

        try:
            from jumpstarter.client.lease import ClientService
            from jumpstarter.config.client import (
                ClientConfigV1Alpha1,
            )
        except ImportError:
            raise ImportError(
                "jumpstarter package not installed. "
                "Install with: pip install jumpstarter"
            )

        if self._config_path and self._config_path.exists():
            cfg = ClientConfigV1Alpha1.from_file(str(self._config_path))
            self._channel = await cfg.channel()
            self._namespace = self._namespace or cfg.metadata.namespace
        else:
            raise ValueError(
                f"Jumpstarter CLI config not found at "
                f"{self._config_path}. Run "
                f"'jmp config client create {self._client_name}'"
            )

        self._service = ClientService(
            channel=self._channel,
            namespace=self._namespace,
        )
        logger.info(
            f"[jumpstarter] Connected to controller (namespace={self._namespace})"
        )

    async def check_available(
        self,
        requirements: dict[str, Any],
    ) -> dict[str, Any]:
        """Check available Jumpstarter exporters.

        Args:
            requirements: May include:
                - jumpstarter_selector: label selector string
                  (e.g., "target=ride4_sa8775p_sx_r3")
                - board_type: board type label to match
                - count: number of devices needed (default 1)
        """
        await self._ensure_connected()
        assert self._service is not None

        exporters = await self._service.ListExporters()
        all_devices = []
        for e in exporters.exporters:
            labels = dict(e.labels)
            all_devices.append(
                {
                    "name": e.name,
                    "labels": labels,
                    "online": getattr(e, "online", True),
                }
            )

        # Filter by selector if provided
        selector = requirements.get(
            "jumpstarter_selector",
            self._default_selector,
        )
        if selector:
            key, _, value = selector.partition("=")
            matching = [
                d for d in all_devices if d["labels"].get(key) == value and d["online"]
            ]
        else:
            matching = [d for d in all_devices if d["online"]]

        count = requirements.get("count", 1)

        return {
            "provider": "jumpstarter",
            "available": len(matching) >= count,
            "matching_devices": len(matching),
            "requested": count,
            "selector": selector,
            "devices": [
                {"name": d["name"], "labels": d["labels"]} for d in matching[:10]
            ],
        }

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str = "",
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve a Jumpstarter device via lease.

        Creates a lease for a device matching the selector.
        Returns the lease ID and device info.

        Args:
            selection: Must include jumpstarter_selector and
                optionally lease_duration_seconds.
            description: Reservation description (for logging).
            duration_hours: Fallback duration if not in selection
                (converted to seconds).
            ticket_id: Ticket ID for lease naming.
        """
        await self._ensure_connected()
        assert self._service is not None

        selector = selection.get(
            "jumpstarter_selector",
            self._default_selector,
        )
        # Prefer explicit seconds from selection, else use
        # duration_hours converted, else default.
        duration_sec = selection.get(
            "lease_duration_seconds",
            duration_hours * 3600 if duration_hours != 36 else self._default_duration,
        )
        duration = timedelta(seconds=duration_sec)

        # Create the lease. Kubernetes requires lowercase
        # alphanumeric names with hyphens.
        lease_id = None
        if ticket_id:
            lease_id = ticket_id.lower().replace("_", "-")

        lease = await self._service.CreateLease(
            selector=selector,
            duration=duration,
            lease_id=lease_id,
        )

        lease_name = lease.name if hasattr(lease, "name") else str(lease)
        self._active_leases[lease_name] = lease

        logger.info(
            f"[jumpstarter] Lease created: {lease_name} "
            f"(selector={selector}, duration={duration_sec}s)"
        )

        # Extract device info from lease
        exporter_name = ""
        if hasattr(lease, "exporter_name"):
            exporter_name = lease.exporter_name
        elif hasattr(lease, "status") and hasattr(lease.status, "exporter_name"):
            exporter_name = lease.status.exporter_name

        return {
            "provider": "jumpstarter",
            "lease_id": lease_name,
            "exporter_name": exporter_name,
            "selector": selector,
            "duration_seconds": duration_sec,
            "ssh_user": self._ssh_user,
            "status": "active",
            # The Jumpstarter MCP tunnel is available
            # immediately after lease creation. The
            # provisioning agent can connect and flash
            # without waiting for an external signal.
            "device_ready": True,
        }

    async def get_reservation_status(
        self,
        reservation_id: str,
    ) -> dict[str, Any]:
        """Check lease status."""
        await self._ensure_connected()
        assert self._service is not None

        try:
            lease = await self._service.GetLease(name=reservation_id)
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": "active",
                "lease": str(lease),
            }
        except Exception as e:
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": "unknown",
                "error": str(e),
            }

    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delete a lease and release the device."""
        await self._ensure_connected()
        assert self._service is not None

        try:
            await self._service.DeleteLease(name=reservation_id)
            self._active_leases.pop(reservation_id, None)
            logger.info(f"[jumpstarter] Lease deleted: {reservation_id}")
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": "terminated",
            }
        except Exception as e:
            logger.exception(f"[jumpstarter] Failed to delete lease {reservation_id}")
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": "error",
                "error": str(e),
            }

    async def setup_ssh(
        self,
        hosts: list[str],
    ) -> dict[str, Any]:
        """SSH setup for Jumpstarter devices.

        Jumpstarter devices may provide SSH access directly
        (IP from `jmp tcp address`) or via proxied SSH through
        the Jumpstarter connection. This method returns the
        SSH configuration for the leased device.
        """
        return {
            "provider": "jumpstarter",
            "ssh_user": self._ssh_user,
            "hosts": hosts,
            "note": (
                "SSH access depends on Jumpstarter exporter "
                "configuration. Use 'jmp tcp address' to get "
                "the device IP for direct SSH."
            ),
        }

    async def cleanup_ssh_keys(
        self,
        hosts: list[str],
    ) -> dict[str, Any]:
        """Clean up SSH keys from Jumpstarter devices."""
        return {
            "provider": "jumpstarter",
            "hosts": hosts,
            "status": "cleanup_skipped",
            "note": ("Jumpstarter lease termination handles device cleanup."),
        }

    async def close(self) -> None:
        """Close the gRPC channel."""
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception:
                pass
            self._channel = None
            self._service = None
