"""Jumpstarter lease lifecycle management.

Extracted from orchestrator/main.py for modularity.
These functions handle lease cleanup, image resolution,
and pre-dispatch preparation — all deterministic code
that runs outside the LLM loop.

Functions are called by the orchestrator's poll loop
and agent task lifecycle. They share the auth_headers
helper via a parameter to avoid circular imports.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from state_store.models import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# Release leases only for truly terminal statuses.
LEASE_RELEASE_STATUSES = frozenset(s.value for s in TERMINAL_STATUSES)


async def release_lease_for_ticket(
    ticket: dict[str, Any],
    auth_headers: dict[str, str] | None = None,
) -> None:
    """Release a Jumpstarter lease if one exists on the ticket.

    Called before dispatching the resource agent at
    awaiting_hardware. Ensures stale leases from a
    previous provisioning attempt are cleaned up before
    acquiring a new board.
    """
    cf = ticket.get("custom_fields", {})
    if cf.get("resource_provider") != "jumpstarter":
        return

    lease_id = cf.get("resource_reservation_id") or cf.get(
        "resource_provider_metadata", {}
    ).get("lease_id", "")
    if not lease_id:
        return

    try:
        _r = subprocess.run(
            ["jmp", "delete", "leases", lease_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if _r.returncode == 0:
            logger.info(
                f"[lease-cleanup] Released {lease_id} before new resource acquisition"
            )
    except Exception:
        logger.debug(
            f"[lease-cleanup] Failed to release {lease_id}",
            exc_info=True,
        )


async def sweep_orphaned_leases(
    store_url: str,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """Release Jumpstarter leases whose tickets are terminal.

    Lists all leases from the jmp CLI, extracts ticket IDs
    from lease names (format: perf-<hex>), checks ticket
    status, and releases leases for terminal tickets.

    Lease lifecycle:
    - Normal: teardown agent calls terminate() → DeleteLease
    - Re-acquire: release_lease_for_ticket() at awaiting_hardware
    - Failsafe: this sweep catches orphans from crashed
      orchestrators, skipped teardown, or manual intervention.

    Runs each poll cycle. jmp get leases is a lightweight
    gRPC call; ticket status checks are batched.
    """
    try:
        result = subprocess.run(
            ["jmp", "get", "leases", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return

        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return
        # jmp get leases -o json returns
        # {"leases": [...]} or a bare list.
        if isinstance(data, dict):
            leases = data.get("leases", [])
        elif isinstance(data, list):
            leases = data
        else:
            return

        # Extract ticket IDs from lease names.
        # Lease names: perf-<8hex>, perf-<8hex>-<suffix>,
        # or UUIDs (not ours).
        _TICKET_RE = re.compile(r"^(perf-[0-9a-f]{8})(?:-|$)", re.IGNORECASE)

        to_release: list[tuple[str, str]] = []
        for lease in leases:
            name = lease.get("name", "") if isinstance(lease, dict) else str(lease)
            m = _TICKET_RE.match(name)
            if not m:
                continue
            # perf-abcd1234 → PERF-ABCD1234
            ticket_id = m.group(1).upper()
            to_release.append((name, ticket_id))

        if not to_release:
            return

        # Check ticket statuses in batch.
        async with httpx.AsyncClient(
            timeout=10.0, headers=auth_headers or {}
        ) as client:
            for lease_name, ticket_id in to_release:
                try:
                    r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
                    if r.status_code == 404:
                        status = "closed"
                    elif r.status_code == 200:
                        status = r.json().get("status", "")
                    else:
                        continue
                except Exception:
                    continue

                if status in LEASE_RELEASE_STATUSES:
                    try:
                        _dr = subprocess.run(
                            ["jmp", "delete", "leases", lease_name],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if _dr.returncode == 0:
                            logger.info(
                                f"[lease-sweep] Released "
                                f"orphaned lease {lease_name}"
                                f" (ticket {ticket_id} at "
                                f"{status})"
                            )
                    except Exception:
                        pass

    except Exception:
        logger.debug(
            "[lease-sweep] Sweep failed",
            exc_info=True,
        )


async def resolve_images(
    store_url: str,
    ticket_id: str,
    auth_headers: dict[str, str] | None = None,
    image_config: dict[str, Any] | None = None,
) -> None:
    """Resolve Jumpstarter image URLs before provisioning.

    Fetches the build server manifest and resolves the flash
    command for the board. Stores the result in
    custom_fields.jumpstarter_flash so the provisioning agent
    can flash without needing to resolve URLs itself.

    This is deterministic code — no LLM reasoning needed.
    """
    _headers = auth_headers or {}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers) as client:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            if r.status_code != 200:
                return
            cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return

        # Already resolved?
        if cf.get("jumpstarter_flash"):
            return

        directives = cf.get("directives", {})
        metadata = cf.get("resource_provider_metadata", {})

        # Resolve image source. No hardcoded OS defaults
        # — if the user didn't specify and there's no
        # config, the provisioning agent must ask.
        img_cfg = image_config or {}

        base_url = directives.get(
            "image_server",
            img_cfg.get(
                "server",
                "https://autosd.sig.centos.org/",
            ),
        )
        image_version = directives.get(
            "image_version",
            img_cfg.get("image_version", ""),
        )
        release = directives.get("release", "nightly")
        image_name = directives.get("image_name", "ps")
        image_type = directives.get("image_type", "regular")

        if not image_version:
            logger.info(
                f"[jumpstarter-images] No image_version "
                f"for {ticket_id} — provisioning agent "
                f"will need to ask the user"
            )
            async with httpx.AsyncClient(timeout=10.0, headers=_headers) as client:
                await client.patch(
                    f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                    json={
                        "fields": {
                            "jumpstarter_flash": {
                                "error": (
                                    "No OS image version"
                                    " specified. Set "
                                    "image_version in "
                                    "ticket directives "
                                    "(e.g., AutoSD-10, "
                                    "RHIVOS-2) or "
                                    "configure "
                                    "jumpstarter_images."
                                    "image_version in "
                                    "config.json."
                                ),
                            },
                        },
                    },
                )
            return

        # Board target from selector. The resource agent
        # may store it as 'selector' or 'jumpstarter_selector'.
        selector = (
            directives.get("board_selector")
            or metadata.get("selector", "")
            or metadata.get("jumpstarter_selector", "")
        )
        # Extract the board target from the selector.
        # Check both old (target) and new (board-type)
        # label keys, plus common LLM variants.
        board_target = ""
        for key in ("target", "board-type", "board", "device"):
            for part in selector.split(","):
                if part.strip().startswith(f"{key}="):
                    board_target = part.strip().split("=", 1)[1]
                    break
            if board_target:
                break
        if not board_target:
            board_target = selector.split(",")[0] if selector else ""
        # Device names include an instance suffix
        # (e.g., nxp-s32g-vnp-rdb3-01). Strip the
        # trailing -NN to get the board type.
        import re

        board_target = re.sub(r"-\d+$", "", board_target)
        # Normalize: manifest keys use underscores
        # (rcar_s4), labels may use hyphens and vendor
        # prefixes (renesas-rcar-s4).
        board_target = board_target.replace("-", "_")
        # Strip common vendor prefixes added in the
        # board-type label migration.
        for prefix in ("renesas_", "nxp_", "ti_jacinto_"):
            if board_target.startswith(prefix):
                stripped = board_target[len(prefix) :]
                # Only strip if the result is a known
                # pattern (contains letters + numbers)
                if stripped:
                    board_target = stripped
                    break

        from providers.resource.jumpstarter_images import (
            resolve_image_urls,
        )

        result = await resolve_image_urls(
            base_url=base_url,
            image_version=image_version,
            release=release,
            board_target=board_target,
            image_name=image_name,
            image_type=image_type,
        )

        # If exact match failed, try fallbacks
        if result.get("error") and result.get("available_variants"):
            variants = result["available_variants"]
            for v in variants:
                if v["image_name"] == image_name:
                    result = await resolve_image_urls(
                        base_url=base_url,
                        image_version=image_version,
                        release=release,
                        board_target=board_target,
                        image_name=v["image_name"],
                        image_type=v["image_type"],
                    )
                    if not result.get("error"):
                        break

        # Flash duration estimate. Check config first,
        # fall back to built-in defaults.
        _DEFAULTS: dict[str, int] = {
            "ride4_sa8775p_sx_r3": 8,
            "ride4_sa8775p_sx": 8,
            "ride4_sa8775p_sx_legacy": 8,
            "ride4_sa8650p_sx_r3": 8,
            "rcar_s4": 20,
            "s32g_vnp_rdb3": 20,
            "j784s4evm": 20,
        }
        flash_durations = img_cfg.get("flash_duration_mins", {})
        result["expected_duration_mins"] = flash_durations.get(
            board_target, _DEFAULTS.get(board_target, 15)
        )

        # Include the orchestrator's SSH public key so
        # the provisioning agent can inject it into the
        # board without needing a local file-read tool.
        try:
            pub_key_path = Path.home() / ".ssh" / "id_rsa.pub"
            if pub_key_path.exists():
                result["ssh_public_key"] = pub_key_path.read_text().strip()
                result["ssh_key_path"] = str(pub_key_path.with_suffix(""))
        except Exception:
            pass

        # Store on ticket
        async with httpx.AsyncClient(timeout=10.0, headers=_headers) as client:
            await client.patch(
                f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                json={
                    "fields": {
                        "jumpstarter_flash": result,
                    },
                },
            )

        if result.get("error"):
            logger.warning(
                f"[jumpstarter-images] Resolution failed "
                f"for {ticket_id}: {result['error']}"
            )
        else:
            logger.info(
                f"[jumpstarter-images] Resolved "
                f"{len(result.get('flash_targets', []))} "
                f"partition(s) for {ticket_id}"
            )

    except Exception:
        logger.debug(
            "[jumpstarter-images] Resolution skipped",
            exc_info=True,
        )
