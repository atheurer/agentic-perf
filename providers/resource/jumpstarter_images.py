"""Jumpstarter image URL resolution.

Resolves OS image URLs from the AutoSD/RHIVOS build server's
test_images_info.json manifest. This is a deterministic lookup —
no LLM reasoning needed, no hardware access needed.

The manifest is keyed by board target label (e.g.,
ride4_sa8775p_sx_r3). Each entry has image_name, image_type,
and partition paths relative to the release base URL.

Usage:
    urls = await resolve_image_urls(
        base_url="https://autosd.sig.centos.org/",
        image_version="AutoSD-10",
        release="nightly",
        board_target="ride4_sa8775p_sx_r3",
        image_name="ps",
        image_type="regular",
    )
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def resolve_image_urls(
    base_url: str = "https://autosd.sig.centos.org/",
    image_version: str = "AutoSD-10",
    release: str = "nightly",
    board_target: str = "",
    image_name: str = "ps",
    image_type: str = "regular",
) -> dict[str, Any]:
    """Resolve image URLs from the build server manifest.

    Args:
        base_url: Root URL of the image server.
        image_version: Image stream (e.g., AutoSD-10, RHIVOS-2).
        release: Build release (e.g., nightly, latest-RHIVOS-2).
        board_target: Board target label from Jumpstarter
            (e.g., ride4_sa8775p_sx_r3).
        image_name: Build variant (ps, qa, developer-vm, etc.).
        image_type: Image format (regular or ostree).

    Returns:
        Dict with resolved URLs and flash command info:
        - flash_targets: list of {partition, url} dicts
        - flash_command: the j storage flash command string
        - board_target: resolved target label
        - manifest_url: URL of the manifest used
        - available_variants: list of available image_name/type
          combos for this board (for fallback selection)
    """
    base_url = base_url.rstrip("/")
    manifest_url = f"{base_url}/{image_version}/{release}/info/test_images_info.json"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(manifest_url)
        if r.status_code != 200:
            return {
                "error": (
                    f"Failed to fetch manifest: {r.status_code} from {manifest_url}"
                ),
                "manifest_url": manifest_url,
            }
        manifest = r.json()

    # Find board entries
    board_entries = manifest.get(board_target, [])
    if not board_entries:
        # List available boards for error message
        board_keys = [
            k
            for k in manifest
            if isinstance(manifest[k], list)
            and len(manifest[k]) > 0
            and isinstance(manifest[k][0], dict)
            and "image_name" in manifest[k][0]
        ]
        return {
            "error": (f"No images found for board '{board_target}'"),
            "available_boards": board_keys,
            "manifest_url": manifest_url,
        }

    # List all available variants for this board
    available = [
        {
            "image_name": e.get("image_name"),
            "image_type": e.get("image_type"),
        }
        for e in board_entries
    ]

    # Find matching entry
    match = None
    for entry in board_entries:
        if (
            entry.get("image_name") == image_name
            and entry.get("image_type") == image_type
        ):
            match = entry
            break

    if not match:
        return {
            "error": (
                f"No image found for "
                f"name='{image_name}', type='{image_type}' "
                f"on board '{board_target}'"
            ),
            "available_variants": available,
            "manifest_url": manifest_url,
        }

    # Build full URLs
    release_base = f"{base_url}/{image_version}/{release}/"
    flash_targets = []

    if "root_image_path" in match:
        # Multi-partition board
        root_url = release_base + match["root_image_path"]
        aboot_url = release_base + match["aboot_image_path"]
        flash_targets.append({"partition": "system_a", "url": root_url})
        flash_targets.append({"partition": "boot_a", "url": aboot_url})
        flash_targets.append({"partition": "boot_b", "url": aboot_url})
        if "qm_var_path" in match:
            qm_url = release_base + match["qm_var_path"]
            flash_targets.append({"partition": "system_b", "url": qm_url})

        # Build flash command
        target_args = " ".join(f"-t {t['partition']}:{t['url']}" for t in flash_targets)
        flash_command = f"j storage flash {target_args}"
    elif "path" in match:
        # Single-image board
        image_url = release_base + match["path"]
        flash_targets.append({"partition": "default", "url": image_url})
        flash_command = f"j storage flash {image_url}"
    else:
        return {
            "error": "Image entry has no path fields",
            "entry": match,
            "manifest_url": manifest_url,
        }

    logger.info(
        f"[jumpstarter-images] Resolved {len(flash_targets)} "
        f"partition(s) for {board_target}/{image_name}/"
        f"{image_type}"
    )

    return {
        "flash_targets": flash_targets,
        "flash_command": flash_command,
        "board_target": board_target,
        "image_name": image_name,
        "image_type": image_type,
        "manifest_url": manifest_url,
        "available_variants": available,
    }
