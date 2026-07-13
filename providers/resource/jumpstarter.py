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
        "default_selector": "",
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

        # Ensure this client is set as the active/current
        # client in the jmp CLI config. Without this, the
        # jmp CLI and MCP server won't find the client
        # config even if the file exists.
        if config_path is not None:
            user_config = Path.home() / ".config" / "jumpstarter" / "config.yaml"
            needs_set = True
            if user_config.exists():
                try:
                    import yaml

                    uc = yaml.safe_load(user_config.read_text())
                    if uc and uc.get("config", {}).get("current-client") == client_name:
                        needs_set = False
                except Exception:
                    pass
            if needs_set:
                user_config.parent.mkdir(parents=True, exist_ok=True)
                user_config.write_text(
                    "apiVersion: jumpstarter.dev/v1alpha1\n"
                    "kind: UserConfig\n"
                    "config:\n"
                    f"  current-client: {client_name}\n"
                )
                logger.info(f"[jumpstarter] Set current client to {client_name}")

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

    async def list_targets(self) -> list[dict[str, Any]]:
        """List unique target types from all online exporters.

        Returns a list of dicts, each with:
        - target: the target label value (used as selector)
        - selector: full selector string for reserve/check calls
        - count: number of online devices with this target
        - example_device: name of one device (for reference)
        - labels: union of all labels seen for this target
        """
        await self._ensure_connected()
        assert self._service is not None

        exporters = await self._service.ListExporters()
        targets: dict[str, dict[str, Any]] = {}
        for e in exporters.exporters:
            labels = dict(e.labels)
            online = getattr(e, "online", False)
            enabled = labels.get("enabled", "true") == "true"
            status = getattr(e, "status", None)
            status_name = status.name if hasattr(status, "name") else str(status)
            if not (online and enabled and status_name == "AVAILABLE"):
                continue
            target = labels.get("target", "unknown")
            if target not in targets:
                targets[target] = {
                    "target": target,
                    "selector": f"target={target}",
                    "count": 0,
                    "example_device": e.name,
                    "labels": {},
                }
            targets[target]["count"] += 1
            # Merge labels (union of all seen values)
            for k, v in labels.items():
                if k not in targets[target]["labels"]:
                    targets[target]["labels"][k] = v

        return sorted(
            targets.values(),
            key=lambda t: t["count"],
            reverse=True,
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
            online = getattr(e, "online", False)
            enabled = labels.get("enabled", "true") == "true"
            status = getattr(e, "status", None)
            # A device is available when:
            # - online: exporter process connected
            # - enabled: not disabled by admin
            # - status AVAILABLE: not leased or offline
            status_name = status.name if hasattr(status, "name") else str(status)
            available = online and enabled and status_name == "AVAILABLE"
            all_devices.append(
                {
                    "name": e.name,
                    "labels": labels,
                    "online": online,
                    "enabled": enabled,
                    "status": status_name,
                    "available": available,
                }
            )

        # Require a selector — the agent must resolve the
        # user's platform to a target via list_jumpstarter_targets
        # before checking availability.
        selector = requirements.get(
            "jumpstarter_selector",
            self._default_selector,
        )
        if not selector:
            # Return available targets so the agent can pick one
            targets = await self.list_targets()
            return {
                "provider": "jumpstarter",
                "available": False,
                "error": (
                    "No jumpstarter_selector provided. Use "
                    "the available_targets list below to "
                    "find the correct target selector for "
                    "the user's platform, then call "
                    "check_available_resources again with "
                    "jumpstarter_selector set."
                ),
                "available_targets": targets,
            }

        key, _, value = selector.partition("=")
        matching = [
            d for d in all_devices if d["labels"].get(key) == value and d["available"]
        ]

        # Fleet exclusion: filter out already-tested hosts
        exclude = requirements.get("exclude_hosts", [])
        if exclude:
            excluded = []
            remaining = []
            for d in matching:
                if d["name"] in exclude:
                    excluded.append(d["name"])
                else:
                    remaining.append(d)
            matching = remaining

        count = requirements.get("count", 1)

        # No matches — distinguish between "wrong
        # selector" and "all devices already tested."
        if not matching:
            if exclude:
                # All matching devices have been tested
                return {
                    "provider": "jumpstarter",
                    "available": False,
                    "matching_devices": 0,
                    "requested": count,
                    "selector": selector,
                    "fleet_exhausted": True,
                    "tested_hosts": exclude,
                    "message": (
                        "All matching devices have "
                        "been tested. Fleet "
                        "investigation complete."
                    ),
                }
            targets = await self.list_targets()
            return {
                "provider": "jumpstarter",
                "available": False,
                "matching_devices": 0,
                "requested": count,
                "selector": selector,
                "error": (
                    f"No devices match selector "
                    f"'{selector}'. Use one of the "
                    f"available target selectors below."
                ),
                "available_targets": targets,
            }

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

        # Jumpstarter creates one lease → one exporter.
        # Reject count > 1 with a clear error so the LLM
        # doesn't build a multi-device target list from
        # a single-device lease.
        requested = selection.get("count", 1)
        if requested > 1:
            return {
                "provider": "jumpstarter",
                "error": (
                    f"Cannot reserve {requested} devices "
                    f"in one lease. Jumpstarter assigns "
                    f"one device per lease. Set count=1 "
                    f"and retry. For fleet investigations, "
                    f"the system iterates one board at a "
                    f"time automatically."
                ),
                "status": "rejected",
            }

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

        # Fleet iterations reuse the same ticket_id.
        # If a lease with this name already exists (e.g.
        # previous iteration wasn't fully cleaned up),
        # append an iteration suffix to avoid conflicts.
        if lease_id:
            base_id = lease_id
            suffix = 2
            while True:
                try:
                    lease = await self._service.CreateLease(
                        selector=selector,
                        duration=duration,
                        lease_id=lease_id,
                    )
                    break
                except Exception as exc:
                    if "already exists" in str(exc):
                        # Release the stale lease before
                        # creating a new one. This is the
                        # natural cleanup point — we have
                        # a live gRPC connection and know
                        # exactly which lease to release.
                        try:
                            await self._service.DeleteLease(
                                name=lease_id,
                            )
                            logger.info(
                                f"[jumpstarter] Released stale lease {lease_id}"
                            )
                            # Retry with the same name
                            continue
                        except Exception:
                            logger.debug(
                                f"[jumpstarter] Could not "
                                f"release {lease_id}, "
                                f"trying suffix",
                                exc_info=True,
                            )
                        lease_id = f"{base_id}-{suffix}"
                        suffix += 1
                        if suffix > 100:
                            raise
                        continue
                    raise
        else:
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
            # Check lease conditions for actual state
            status = "active"
            if hasattr(lease, "conditions"):
                for cond in lease.conditions:
                    ctype = getattr(cond, "type", "")
                    cstatus = getattr(cond, "status", "")
                    if ctype == "Ready" and cstatus != "True":
                        status = "pending"
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": status,
                "lease": str(lease),
            }
        except Exception as e:
            err = str(e)
            # The controller returns FAILED_PRECONDITION
            # for released leases.
            status = "released" if "already been released" in err else "unknown"
            return {
                "provider": "jumpstarter",
                "lease_id": reservation_id,
                "status": status,
                "error": err,
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
