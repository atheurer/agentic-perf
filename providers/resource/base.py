from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

_RESERVATION_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "rejected", "cancelled", "canceled"}
)
_PROVIDER_RESERVATION_KEYS = {
    "jumpstarter": ("lease_id",),
    "aws": ("instance_ids",),
    "quads": ("assignment_id",),
    "psap-cc": ("reservation_id",),
}


def reservation_failed(result: dict[str, Any]) -> bool:
    """Return whether a provider reservation result reports failure."""
    if result.get("error"):
        return True
    status = str(result.get("status", "")).strip().lower()
    return status in _RESERVATION_FAILURE_STATUSES


def has_reservation_metadata(provider: str, metadata: Any) -> bool:
    """Check provider-specific metadata for evidence of an allocated resource."""
    if not isinstance(metadata, dict):
        return False
    keys = _PROVIDER_RESERVATION_KEYS.get(provider, ("lease_id", "reservation_id"))
    for key in (*keys, "reservation_id", "lease_id"):
        value = metadata.get(key)
        if key == "instance_ids":
            if isinstance(value, (list, tuple, set)) and value:
                return True
            if isinstance(value, str) and value.strip():
                return True
        elif value is not None and str(value).strip():
            return True
    return False


class ResourceProvider(ABC):
    """Base class for resource providers (QUADS, AWS EC2, GCP, etc.).

    Each provider implements a standard lifecycle:
    check_available → reserve → (get_reservation_status) → terminate
    with SSH setup/cleanup as needed.
    """

    provider_name: str

    @classmethod
    @abstractmethod
    async def from_secrets(cls, secrets_provider) -> ResourceProvider:
        """Factory that loads configuration from the secrets provider."""
        ...

    @abstractmethod
    async def check_available(self, requirements: dict[str, Any]) -> dict[str, Any]:
        """Check what resources are available matching the requirements.

        Args:
            requirements: Provider-agnostic dict that may include:
                - min_cores: int
                - min_memory_gb: int
                - nic_speed: int (Gbps)
                - nic_vendor: str
                - disk_type: str
                - count: int (number of hosts needed)
                - duration_hours: int
                Plus provider-specific keys (model_filter for QUADS,
                instance_type for AWS, etc.) — providers ignore unknown keys.

        Returns:
            {
                "provider": str,
                "available_count": int,  # -1 means effectively unlimited
                "options": list[dict],
                "message": str,
            }
        """
        ...

    @abstractmethod
    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve the selected resources.

        Args:
            selection: Provider-specific selection from check_available options
                (e.g., {"hostnames": [...]} for QUADS, {"instance_type": "...", "count": N} for AWS).
            description: Human-readable description for the reservation.
            duration_hours: Requested lease duration (providers may ignore if N/A).
            ticket_id: Jira ticket ID for tagging/traceability (optional).

        Returns:
            {
                "status": "success" | "failed",
                "reservation_id": str,
                "hosts": list[str],       # IPs or hostnames
                "ssh_user": str,
                "ssh_key_path": str,
                "lease_expiration": str | None,
                "provider": str,
                "provider_metadata": dict,  # provider-specific (instance_ids, cloud_name, etc.)
                "message": str,
            }
        """
        ...

    @abstractmethod
    async def get_reservation_status(
        self, reservation_id: str, provider_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Check the status of an existing reservation."""
        ...

    @abstractmethod
    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Terminate a reservation and release resources."""
        ...

    @abstractmethod
    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        """Set up SSH access to the reserved hosts."""
        ...

    @abstractmethod
    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        """Remove provisioning SSH keys from hosts."""
        ...

    async def close(self) -> None:
        """Release any held connections."""
        pass
