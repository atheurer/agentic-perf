from __future__ import annotations

"""Image builder agent — deterministic custom image builds.

One job: build a custom OS image from ticket directives using
a configured build provider, store the resulting image URL on
the ticket, and advance to hardware acquisition.

This agent is fully deterministic — no LLM calls. It reads
the ``image_build`` field from ticket custom_fields, delegates
to the appropriate build provider, and stores the result.

Build providers are pluggable — see ``providers/image_build/``
for the abstract interface and available implementations.
"""


import json
import logging
from typing import Any

from providers.events import EventBus
from providers.image_build.base import BuildResult, BuildSpec

logger = logging.getLogger(__name__)

# Provider registry — maps provider names to classes.
# New providers register here.
_PROVIDERS: dict[str, str] = {
    "caib": "providers.image_build.caib.CAIBProvider",
}


def _load_provider(name: str):
    """Lazily load a build provider by name."""
    path = _PROVIDERS.get(name)
    if not path:
        raise ValueError(
            f"Unknown image build provider: {name!r}. "
            f"Available: {list(_PROVIDERS.keys())}"
        )
    module_path, class_name = path.rsplit(".", 1)
    import importlib

    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)()


class ImageBuilderAgent:
    """Deterministic image builder — no LLM needed."""

    agent_name = "image-builder"

    def __init__(
        self,
        *,
        llm_provider: Any = None,
        state_store_url: str = "http://localhost:8090",
        event_bus: EventBus | None = None,
    ) -> None:
        self.store_url = state_store_url
        self._events = event_bus

        import httpx

        from state_store.auth import read_token_from_file

        token = read_token_from_file()
        self._client = httpx.AsyncClient(
            base_url=state_store_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def run(self, ticket_id: str) -> None:
        """Build a custom image from ticket directives."""
        try:
            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            image_build = cf.get("image_build", {})
            self._emit(
                ticket_id,
                "agent_started",
                {
                    "provider": image_build.get("provider", "unknown"),
                    "customizations": image_build.get("customizations", {}),
                },
            )
            await self._build(ticket_id)
        except Exception as e:
            logger.error(
                f"[image-builder] {ticket_id}: {e}",
                exc_info=True,
            )
            self._emit(
                ticket_id,
                "agent_error",
                {"reason": str(e)},
            )
            await self._add_comment(
                ticket_id,
                f"**Image build failed:** {e}",
            )
            await self._transition(
                ticket_id,
                "awaiting_customer_guidance",
                f"Image build error: {e}",
            )
        finally:
            self._emit(ticket_id, "agent_finished", {})
            await self._client.aclose()

    async def _build(self, ticket_id: str) -> None:
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        image_build = cf.get("image_build", {})

        if not image_build:
            await self._add_comment(
                ticket_id,
                "**Skipping image build:** no image_build "
                "directives found. Using default image.",
            )
            await self._transition(
                ticket_id,
                "awaiting_hardware",
                "No custom build needed",
            )
            return

        # Select build provider
        provider_name = image_build.get("provider")
        if not provider_name:
            provider_name = list(_PROVIDERS.keys())[0] if _PROVIDERS else ""
            logger.info(
                "[image-builder] No provider specified, using %s",
                provider_name,
            )
        provider = _load_provider(provider_name)

        # Resolve target and build mode via provider interface
        board_selector = directives.get("board_selector", "")
        target = image_build.get("target", provider.resolve_target(board_selector))
        # Build mode: explicit from directives, or provider
        # default. Both package (build-dev) and bootc (build)
        # work on all targets — the choice depends on test
        # requirements, not hardware.
        build_mode = image_build.get(
            "build_mode",
            provider.resolve_build_mode(board_selector),
        )

        # Build the spec
        # Build a descriptive tag for the image:
        # <short_target>-<mode>-<timestamp>-<ticket>
        image_mode = (
            "package"
            if directives.get("image_type", "regular") in ("regular", "package")
            else "ostree"
        )
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        ticket_short = ticket_id.lower().replace("_", "-")
        # Keep build name under 50 chars to avoid Kubernetes
        # Secret name length limits (63 chars, CAIB appends
        # a suffix). Use abbreviated components.
        short_target = target.replace("ride4_sa8775p_", "r4-").replace("_", "-")
        build_name = f"{short_target}-{image_mode[:3]}-{timestamp}-{ticket_short}"
        # Read image_build config from config file
        from orchestrator.config import _load_config_file

        build_cfg = _load_config_file().get("image_build", {})

        spec = BuildSpec(
            name=build_name,
            target=target,
            customizations=image_build.get("customizations", {}),
            timeout_minutes=image_build.get("timeout_minutes", 60),
            extra_options={
                "build_mode": build_mode,
                "image_mode": (
                    "package"
                    if directives.get("image_type", "regular") in ("regular", "package")
                    else "image"
                ),
                "push_registry": build_cfg.get("push_registry", ""),
                **image_build.get("extra_options", {}),
            },
        )

        await self._add_comment(
            ticket_id,
            f"**Building custom image**\n\n"
            f"- **Provider:** {provider_name}\n"
            f"- **Target:** {target}\n"
            f"- **Build name:** {build_name}\n"
            f"- **Customizations:** "
            f"{json.dumps(spec.customizations, indent=2)[:500]}",
        )

        # Run the build
        result: BuildResult = await provider.build(spec)

        # Store result on ticket
        await self._update_fields(
            ticket_id,
            {
                "image_build_result": {
                    "provider": provider_name,
                    "build_name": result.build_name,
                    "image_url": result.image_url,
                    "status": ("completed" if result.success else "failed"),
                    "error": result.error,
                    "details": result.details,
                },
            },
        )

        if result.success:
            self._emit(
                ticket_id,
                "build_complete",
                {
                    "build_name": result.build_name,
                    "image_url": result.image_url,
                },
            )
            await self._add_comment(
                ticket_id,
                f"**Image build complete**\n\n"
                f"- **Provider:** {provider_name}\n"
                f"- **Build:** {result.build_name}\n"
                f"- **Image URL:** {result.image_url or 'internal registry'}",
            )
            await self._transition(
                ticket_id,
                "awaiting_hardware",
                f"Custom image built: {result.build_name}",
            )
        else:
            self._emit(
                ticket_id,
                "build_failed",
                {
                    "build_name": result.build_name,
                    "error": result.error[:500],
                },
            )
            await self._add_comment(
                ticket_id,
                f"**Image build failed**\n\n"
                f"- **Provider:** {provider_name}\n"
                f"- **Error:** {result.error[:500]}",
            )
            await self._transition(
                ticket_id,
                "awaiting_customer_guidance",
                f"Build failed: {result.error[:100]}",
            )

    # --- HTTP helpers ---

    async def _get_ticket(self, ticket_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        return r.json()

    async def _update_fields(
        self,
        ticket_id: str,
        fields: dict[str, Any],
    ) -> None:
        r = await self._client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": fields},
        )
        r.raise_for_status()

    async def _add_comment(self, ticket_id: str, body: str) -> None:
        r = await self._client.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={
                "author": self.agent_name,
                "body": body,
            },
        )
        r.raise_for_status()

    async def _transition(
        self,
        ticket_id: str,
        status: str,
        comment: str,
    ) -> None:
        r = await self._client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"status": status},
        )
        r.raise_for_status()

    def _emit(
        self,
        ticket_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if self._events:
            self._events.emit(
                ticket_id,
                self.agent_name,
                event_type,
                {**data},
            )
