from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import ResourceProvider

logger = logging.getLogger(__name__)


class PSAPCCResourceProvider(ResourceProvider):
    """ResourceProvider for PSAP Control Center GPU clusters.

    Reserves full K8s/OCP clusters with GPUs. Unlike bare-metal or cloud
    providers, this does not provide SSH-accessible hosts — it provides
    access to a pre-existing cluster via its API server URL.
    """

    provider_name = "psap-cc"

    def __init__(self, client) -> None:
        from providers.psap_cc import PSAPControlCenterClient

        self._client: PSAPControlCenterClient = client

    @classmethod
    async def from_secrets(cls, secrets_provider) -> PSAPCCResourceProvider:
        from providers.psap_cc import PSAPControlCenterClient

        client = await PSAPControlCenterClient.from_secrets(secrets_provider)
        return cls(client)

    async def check_available(self, requirements: dict[str, Any]) -> dict[str, Any]:
        clusters = await self._client.list_clusters(active_only=True)
        healthy = [c for c in clusters if c.get("status") == "healthy"]

        reservations = await self._client.list_reservations()
        reserved_cluster_ids = {
            r["cluster_id"]
            for r in reservations
            if r.get("reservation_type") == "cluster"
            and r.get("status") in ("active", "scheduled")
        }

        available = [c for c in healthy if c["id"] not in reserved_cluster_ids]

        min_gpus = requirements.get("min_gpus")
        if min_gpus:
            available = [c for c in available if int(c.get("gpu_count", 0)) >= min_gpus]

        gpu_type = requirements.get("gpu_type")
        if gpu_type:
            gpu_type_lower = gpu_type.lower()
            available = [
                c
                for c in available
                if gpu_type_lower in (c.get("gpu_type") or "").lower()
            ]

        return {
            "provider": self.provider_name,
            "available_count": len(available),
            "options": [
                {
                    "cluster_id": c["id"],
                    "cluster_name": c["name"],
                    "gpu_type": c.get("gpu_type", "unknown"),
                    "gpu_count": int(c.get("gpu_count", 0)),
                    "api_server_url": c.get("api_server_url", ""),
                    "node_count": c.get("node_count"),
                }
                for c in available
            ],
            "message": f"{len(available)} GPU cluster(s) available for reservation",
        }

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
    ) -> dict[str, Any]:
        selection = self.validate_reservation_selection(selection)
        cluster_id = selection.get("cluster_id")
        if not cluster_id:
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "",
                "ssh_key_path": "",
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": "No cluster_id in selection",
            }

        try:
            cluster = await self._client.get_cluster(cluster_id)
        except Exception as e:
            return {
                "status": "failed",
                "reservation_id": "",
                "hosts": [],
                "ssh_user": "",
                "ssh_key_path": "",
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": f"Cluster {cluster_id} not found: {e}",
            }

        now = datetime.now(timezone.utc)
        end_time = now + timedelta(hours=duration_hours)

        reservation_data = {
            "cluster_id": cluster_id,
            "title": description,
            "user_name": self._client.username,
            "start_time": now.isoformat(),
            "end_time": end_time.isoformat(),
            "reservation_type": "cluster",
            "purpose": description,
        }

        logger.info(
            f"[psap-cc] Creating cluster reservation on "
            f"{cluster.get('name', cluster_id)}"
        )

        reservation = await self._client.create_reservation(reservation_data)
        reservation_id = reservation.get("id", "")

        worker_nodes = []
        try:
            topology = await self._client.get_cluster_topology(cluster_id)
            for node in topology.get("nodes", []):
                roles = node.get("roles", [])
                if "worker" in roles and int(node.get("gpu", 0)) > 0:
                    worker_nodes.append(
                        {
                            "name": node["name"],
                            "ip": node.get("internal_ip", ""),
                            "gpu": node.get("gpu", "0"),
                            "gpu_type": node.get("gpu_type", "unknown"),
                        }
                    )
        except Exception:
            logger.warning(f"[psap-cc] Could not fetch topology for {cluster_id}")

        cluster_name = cluster.get("name", cluster_id)
        gpu_type = cluster.get("gpu_type", "unknown")
        gpu_count = int(cluster.get("gpu_count", 0))
        api_server_url = cluster.get("api_server_url", "")

        return {
            "status": "success",
            "reservation_id": reservation_id,
            "hosts": [],
            "ssh_user": "",
            "ssh_key_path": "",
            "lease_expiration": end_time.isoformat(),
            "provider": self.provider_name,
            "provider_metadata": {
                "cluster_id": cluster_id,
                "cluster_name": cluster_name,
                "api_server_url": api_server_url,
                "gpu_type": gpu_type,
                "gpu_count": gpu_count,
                "reservation_id": reservation_id,
                "worker_nodes": worker_nodes,
            },
            "message": (f"Reserved cluster {cluster_name} ({gpu_count}x {gpu_type})"),
        }

    async def get_reservation_status(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reservation = await self._client.get_reservation(reservation_id)
        status = reservation.get("status", "unknown")

        cluster_id = (provider_metadata or {}).get(
            "cluster_id", reservation.get("cluster_id")
        )
        cluster_healthy = False
        if cluster_id:
            try:
                cluster_status = await self._client.get_cluster_status(cluster_id)
                cluster_healthy = cluster_status.get("status") == "healthy"
            except Exception:
                pass

        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "ready": status == "active" and cluster_healthy,
            "details": {
                "reservation_status": status,
                "cluster_healthy": cluster_healthy,
                "cluster_name": reservation.get("cluster_name"),
                "start_time": reservation.get("start_time"),
                "end_time": reservation.get("end_time"),
            },
        }

    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(f"[psap-cc] Cancelling reservation {reservation_id}")
        result = await self._client.cancel_reservation(reservation_id)
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "status": "terminated",
            "details": {
                "cluster_name": provider_metadata.get("cluster_name"),
                "api_response": result,
            },
        }

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        return {
            "status": "skipped",
            "ssh_key_path": "",
            "hosts": {},
            "message": ("psap-cc provides K8s cluster access, not SSH hosts"),
        }

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        return {
            "status": "skipped",
            "hosts": {},
            "message": ("psap-cc provides K8s cluster access, not SSH hosts"),
        }

    async def close(self) -> None:
        await self._client.close()
