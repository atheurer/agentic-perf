from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from providers.secrets.base import SecretsProvider

from .base import ResourceProvider

logger = logging.getLogger(__name__)

PROVIDER_REGISTRY: dict[str, dict[str, str]] = {
    "quads": {
        "class": "providers.resource.quads.QuadsResourceProvider",
        "secret": "quads/config.json",
    },
    "aws": {
        "class": "providers.resource.aws.AWSResourceProvider",
        "secret": "aws/config.json",
    },
    "psap-cc": {
        "class": "providers.resource.psap_cc.PSAPCCResourceProvider",
        "secret": "psap-cc/config.json",
    },
    "jumpstarter": {
        "class": "providers.resource.jumpstarter.JumpstarterResourceProvider",
        "secret": "jumpstarter/config.json",
    },
}


class ResourceProviderRegistry:
    """Discovers configured resource providers and lazy-loads them."""

    def __init__(
        self,
        secrets_provider: SecretsProvider,
        instance_name: str | None = None,
        organization_resolver: Any | None = None,
        organization_resolver_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._secrets = secrets_provider
        self._instance_name = instance_name
        self._organization_resolver = organization_resolver
        self._organization_resolver_factory = organization_resolver_factory
        self._providers: dict[str, ResourceProvider] = {}

    async def list_configured_providers(self) -> list[dict[str, Any]]:
        """Return metadata for providers that have secrets configured."""
        configured = []
        for name, entry in PROVIDER_REGISTRY.items():
            raw = await self._secrets.get_secret(entry["secret"])
            if raw:
                if name in ("quads", "jumpstarter"):
                    ptype = "bare_metal"
                elif name == "psap-cc":
                    ptype = "gpu_cluster"
                else:
                    ptype = "cloud"
                configured.append({"name": name, "type": ptype})
        return configured

    async def get_provider(self, name: str) -> ResourceProvider:
        """Get or create a provider instance by name."""
        if name in self._providers:
            return self._providers[name]

        entry = PROVIDER_REGISTRY.get(name)
        if not entry:
            raise ValueError(
                f"Unknown resource provider: {name}. "
                f"Available: {list(PROVIDER_REGISTRY.keys())}"
            )

        raw = await self._secrets.get_secret(entry["secret"])
        if not raw:
            raise ValueError(
                f"Resource provider '{name}' is not configured "
                f"(missing secrets at {entry['secret']})"
            )

        module_path, cls_name = entry["class"].rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, cls_name)
        if name == "aws":
            provider = await cls.from_secrets(
                self._secrets, instance_name=self._instance_name
            )
        elif name == "quads":
            provider = await cls.from_secrets(
                self._secrets,
                organization_config=await self._organization_service_config(
                    "resource/quads"
                ),
            )
        else:
            provider = await cls.from_secrets(self._secrets)
        self._providers[name] = provider
        logger.info(f"[registry] Loaded resource provider: {name}")
        return provider

    async def _organization_service_config(self, subject: str) -> dict[str, Any] | None:
        resolver = self._organization_resolver
        if resolver is None and self._organization_resolver_factory is not None:
            resolver = await self._organization_resolver_factory()
            self._organization_resolver = resolver
        if resolver is None or not resolver.has_runtime_config(subject):
            return None
        config = resolver.get_runtime_config(subject)
        if config is None:
            return None
        source = resolver.bootstrap(subject)
        logger.info(
            "[registry] Loaded organization service configuration through the "
            "skill gateway: subject=%s revision=%s sources=%s",
            subject,
            source.get("revision"),
            source.get("runtime_config_sources", []),
        )
        return config

    async def close_all(self) -> None:
        for name, provider in self._providers.items():
            try:
                await provider.close()
            except Exception:
                logger.exception(f"[registry] Failed to close provider: {name}")
        self._providers.clear()
