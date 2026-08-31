from __future__ import annotations

"""CAIB (Cloud Automotive Image Builder) provider.

Builds custom AutoSD images using the ``caib`` CLI tool.
Generates AIB manifests from build specs and runs the build
in the cloud via the CAIB build service.
"""


import asyncio
import copy
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from .base import BuildResult, BuildSpec, ImageBuildProvider

logger = logging.getLogger(__name__)

# Default AIB manifest template.
#
# RPMs here are additive to the AIB target's base package set.
# The EBBR base is minimal — these are packages that our
# benchmarks and remote management universally require.
_BASE_MANIFEST: dict[str, Any] = {
    "name": "agentic-perf-custom",
    "content": {
        "rpms": [
            # Remote access
            "openssh-server",
            "openssh-clients",
            "sshpass",
            # Network configuration — without NetworkManager,
            # Linux interfaces are not brought up even if
            # firmware assigns an IP via DHCP
            "NetworkManager",
            "dhcpcd",
            # Time sync (accurate measurements)
            "chrony",
            # Network diagnostics
            "iproute",
            "iproute-tc",
            # Container runtime (boot-time TTFC, workloads)
            "podman",
        ],
        "systemd": {
            "enabled_services": [
                "sshd.service",
                # osbuild does not run systemd presets,
                # so services must be explicitly enabled
                "NetworkManager.service",
                "chronyd.service",
            ],
        },
    },
    # Explicit dynamic network — AIB schema defaults to
    # dynamic but may not apply the default when the field
    # is absent. Without this, NetworkManager is not
    # configured and interfaces stay down.
    "network": {
        "dynamic": {},
    },
    "image": {
        "image_size": "8 GiB",
        "sealed": False,
    },
    "auth": {
        "root_password": (
            "$6$xoLqEUz0cGGJRx01$H3H/bFm0myJPULNMtbSsOFd/"
            "2BnHqHkMD92Sfxd.EKM9hXTWSmELG8cf205l6dktomuTcg"
            "KGGtGDgtvHVXSWU."
        ),
        "sshd_config": {
            "PermitRootLogin": True,
            "PasswordAuthentication": True,
        },
    },
}

# Map board selectors to CAIB target names
_BOARD_TO_TARGET: dict[str, str] = {
    "nxp-s32g-vnp-rdb3": "ebbr",
    "renesas-rcar-s4": "ebbr",
    "qc8775": "ride4_sa8775p_sx",
}

# Default build mode. Both package (build-dev) and bootc (build)
# work on ALL targets. Default to package mode because it
# produces a mutable rootfs that supports post-deploy
# modifications (dnf install, config changes).
_DEFAULT_BUILD_MODE = "build-dev"


def resolve_target(board_selector: str) -> str:
    """Resolve a board selector to a CAIB target name."""
    if "=" in board_selector:
        board_type = board_selector.split("=", 1)[1]
        board_type = board_type.split(",")[0]
        return _BOARD_TO_TARGET.get(board_type, "ebbr")
    return "ebbr"


def resolve_build_mode(board_selector: str) -> str:
    """Resolve a board selector to a CAIB build mode.

    Both package (build-dev) and bootc (build) modes work on
    all targets. Default to package mode for mutability.
    """
    return _DEFAULT_BUILD_MODE


def generate_manifest(
    customizations: dict[str, Any],
    name: str = "agentic-perf-custom",
) -> dict[str, Any]:
    """Generate an AIB manifest from customizations."""
    manifest = copy.deepcopy(_BASE_MANIFEST)
    manifest["name"] = name

    extra_rpms = customizations.get("rpms", [])
    if extra_rpms:
        manifest["content"]["rpms"].extend(extra_rpms)

    repos = customizations.get("repos", [])
    if repos:
        manifest["content"]["repos"] = repos

    enabled = customizations.get("enabled_services", [])
    if enabled:
        manifest["content"]["systemd"]["enabled_services"].extend(enabled)

    disabled = customizations.get("disabled_services", [])
    if disabled:
        manifest["content"]["systemd"]["disabled_services"] = disabled

    # NOTE: masked_services is NOT supported by the AIB
    # manifest schema. Use disabled_services instead.
    masked = customizations.get("masked_services", [])
    if masked:
        logger.warning(
            "[caib] masked_services is not supported by AIB. "
            "Converting to disabled_services."
        )
        disabled = manifest["content"]["systemd"].get("disabled_services", [])
        disabled.extend(masked)
        manifest["content"]["systemd"]["disabled_services"] = disabled

    kernel = customizations.get("kernel")
    if kernel:
        if isinstance(kernel, str):
            # Triage may infer a kernel package name as a
            # string, but AIB expects an object with fields
            # like {"package": ..., "version": ...}.
            # Convert common string patterns.
            manifest["kernel"] = {"package": kernel}
        elif isinstance(kernel, dict):
            manifest["kernel"] = kernel
        else:
            logger.warning(
                "[caib] Ignoring invalid kernel field (expected dict, got %s)",
                type(kernel).__name__,
            )

    return manifest


class CAIBProvider(ImageBuildProvider):
    """Build images using the CAIB CLI."""

    provider_name = "caib"

    def resolve_target(self, board_selector: str) -> str:
        return resolve_target(board_selector)

    def resolve_build_mode(self, board_selector: str) -> str:
        return resolve_build_mode(board_selector)

    async def build(self, spec: BuildSpec) -> BuildResult:
        """Build an image via caib CLI."""
        target = spec.target or "ebbr"
        build_mode = spec.extra_options.get("build_mode", "build-dev")

        # Generate and write manifest
        manifest = generate_manifest(
            spec.customizations,
            name=spec.name or "agentic-perf-custom",
        )

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".aib.yml",
            delete=False,
        ) as f:
            import yaml

            yaml.dump(manifest, f, default_flow_style=False)
            manifest_path = f.name

        try:
            build_name = spec.name or "agentic-perf-custom"

            cmd = ["caib", "image"]
            if build_mode == "build-dev":
                # build-dev supports --mode package or image (ostree)
                dev_mode = spec.extra_options.get("image_mode", "package")
                cmd += [
                    "build-dev",
                    manifest_path,
                    "--mode",
                    dev_mode,
                ]
            else:
                # bootc container build with disk image
                cmd += ["build", manifest_path, "--disk"]

            cmd += [
                "--format",
                "simg",
                "--target",
                target,
                "--wait",
                "--name",
                build_name,
                "--output-format",
                "json",
                "--timeout",
                str(spec.timeout_minutes),
            ]

            # Build record TTL (CAIB-side cleanup)
            ttl = spec.extra_options.get("ttl", "168h")
            if ttl:
                cmd += ["--ttl", ttl]

            # Push to external registry so Jumpstarter
            # exporters can pull the image. Configured via
            # config.json image_build.push_registry.
            push_registry = spec.extra_options.get("push_registry", "")
            if push_registry:
                cmd += [
                    "--push",
                    f"{push_registry}:{build_name}",
                ]
                # Registry auth for pushing (e.g., Quay robot account)
                from paths import SECRETS_DIR as _sd

                registry_auth = _sd / "caib" / "registry-auth.json"
                if registry_auth.exists():
                    cmd += [
                        "--registry-auth-file",
                        str(registry_auth),
                    ]

            aib_image = spec.extra_options.get("aib_image")
            if aib_image:
                cmd += ["--aib-image", aib_image]

            # Read CAIB service token from secrets
            token = ""
            from paths import SECRETS_DIR

            token_path = SECRETS_DIR / "caib" / "token"
            if token_path.exists():
                token = token_path.read_text().strip()
            if token:
                # NOTE: --token on argv is visible in
                # /proc/pid/cmdline. CAIB CLI does not support
                # env-var or file-based token input (upstream
                # limitation, same category as boot-time-analysis
                # --password= exposure).
                cmd += ["--token", token]

            # Server URL (derived from jumpstarter config
            # or explicit)
            server_url = spec.extra_options.get("server_url", "")
            if server_url:
                cmd += ["--server", server_url]

            logger.info(
                "[caib] Building: %s", " ".join(a if a != token else "***" for a in cmd)
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=spec.timeout_minutes * 60 + 120,
            )

            stdout_str = stdout.decode(errors="replace")
            stderr_str = stderr.decode(errors="replace")

            if proc.returncode != 0:
                return BuildResult(
                    success=False,
                    build_name=build_name,
                    error=(f"caib exited {proc.returncode}: {stderr_str[:1000]}"),
                )

            # Parse build output
            details = {}
            try:
                details = json.loads(stdout_str)
            except json.JSONDecodeError:
                pass

            # Extract image URL from build result.
            # CAIB JSON output fields:
            #   diskImage: OCI registry URL for the disk image
            #   jumpstarter.flashCmd: "j storage flash oci://<url>"
            image_url = details.get("diskImage", "")
            if not image_url:
                flash_cmd = details.get("jumpstarter", {}).get("flashCmd", "")
                if "oci://" in flash_cmd:
                    image_url = flash_cmd.split("oci://", 1)[1]

            # Set Quay tag expiration if push_registry is quay.io
            if push_registry and "quay.io" in push_registry:
                await self._set_quay_tag_expiration(
                    push_registry,
                    build_name,
                    days=spec.extra_options.get("tag_expiration_days", 14),
                )

            return BuildResult(
                success=True,
                build_name=build_name,
                image_url=image_url,
                details=details,
            )

        finally:
            Path(manifest_path).unlink(missing_ok=True)

    async def _set_quay_tag_expiration(
        self,
        registry: str,
        tag: str,
        days: int = 14,
    ) -> None:
        """Set tag expiration on Quay via API.

        Token resolution order:
        1. Robot account token from ``secrets/caib/registry-auth.json``
           (repo-scoped, already used for push).
        2. Dedicated OAuth token from ``secrets/quay/api-token``
           (user-scoped, needs 'Administer Repositories' scope).

        The Quay REST API requires Bearer auth — basic auth
        triggers a 403 CSRF rejection.
        """
        import time

        try:
            from paths import SECRETS_DIR

            token = self._resolve_quay_token(SECRETS_DIR)
            if not token:
                logger.debug(
                    "[caib] No Quay API token found, skipping tag expiration",
                )
                return

            # Parse registry path: quay.io/org/repo
            parts = registry.replace("quay.io/", "").split("/")
            if len(parts) < 2:
                return
            namespace = parts[0]
            repo = "/".join(parts[1:])

            expiration = int(time.time()) + (days * 86400)

            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.put(
                    f"https://quay.io/api/v1/repository/{namespace}/{repo}/tag/{tag}",
                    json={"expiration": expiration},
                    headers={
                        "Authorization": f"Bearer {token}",
                    },
                )
                if r.status_code == 200:
                    logger.info(
                        "[caib] Set Quay tag %s expiration to %d days",
                        tag,
                        days,
                    )
                    # Also expire sha256- digest tags
                    await self._expire_digest_tags(
                        client,
                        namespace,
                        repo,
                        tag,
                        token,
                        expiration,
                    )
                else:
                    logger.warning(
                        "[caib] Failed to set Quay tag expiration: %s %s",
                        r.status_code,
                        r.text[:200],
                    )
        except Exception:
            logger.debug(
                "Failed to set Quay tag expiration",
                exc_info=True,
            )

    @staticmethod
    async def _expire_digest_tags(
        client: Any,
        namespace: str,
        repo: str,
        tag: str,
        token: str,
        expiration: int,
    ) -> None:
        """Expire sha256- digest tags for the same manifest."""
        try:
            # Get the manifest digest for our tag
            r = await client.get(
                f"https://quay.io/api/v1/repository/{namespace}/{repo}/tag/",
                params={"specificTag": tag},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return
            tags = r.json().get("tags", [])
            if not tags:
                return
            digest = tags[0].get("manifest_digest", "")
            if not digest:
                return

            # Find sha256- tags with matching digest
            digest_tag = digest.replace(":", "-")
            r = await client.put(
                f"https://quay.io/api/v1/repository/"
                f"{namespace}/{repo}/tag/{digest_tag}",
                json={"expiration": expiration},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                logger.info(
                    "[caib] Set digest tag %s expiration",
                    digest_tag,
                )
        except Exception:
            logger.debug(
                "Failed to expire digest tags",
                exc_info=True,
            )

    @staticmethod
    def _resolve_quay_token(secrets_dir: Path) -> str:
        """Find a Quay API token from available secrets."""
        import base64

        # 1. Robot account token from registry-auth.json
        auth_path = secrets_dir / "caib" / "registry-auth.json"
        if auth_path.exists():
            try:
                auth_data = json.loads(auth_path.read_text())
                encoded = auth_data.get("auths", {}).get("quay.io", {}).get("auth", "")
                if encoded:
                    decoded = base64.b64decode(encoded).decode()
                    # Robot auth is "user+robot:token" — token
                    # is the password portion
                    _, token = decoded.split(":", 1)
                    if token:
                        return token
            except Exception:
                pass

        # 2. Dedicated OAuth token
        token_path = secrets_dir / "quay" / "api-token"
        if token_path.exists():
            token = token_path.read_text().strip()
            if token:
                return token

        return ""
