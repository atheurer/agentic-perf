"""Resolve Foreman site configuration without exposing credential values."""

from __future__ import annotations

import json
from typing import Any

from providers.foreman import ForemanError


async def resolve_foreman_config(
    resolver: Any, secrets: Any, site: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose one complete source and one site; local config replaces org config."""
    raw = await secrets.get_secret("foreman/config.json")
    if raw is not None:
        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            raise ForemanError(
                "configuration", "Invalid local Foreman configuration"
            ) from None
        provenance: dict[str, Any] = {"source": "installation-local"}
    elif resolver is not None and resolver.has_runtime_config("service/foreman"):
        config = resolver.get_runtime_config("service/foreman")
        source = resolver.bootstrap("service/foreman")
        provenance = {
            "source": "organization",
            "revision": source.get("revision"),
            "sources": source.get("runtime_config_sources", []),
        }
    else:
        raise ForemanError("configuration", "Foreman service configuration is missing")
    sites = config.get("sites") if isinstance(config, dict) else None
    if not isinstance(sites, dict) or not sites:
        raise ForemanError("configuration", "Foreman sites configuration is invalid")
    if not site:
        if len(sites) != 1:
            raise ForemanError("configuration", "Foreman site selection is ambiguous")
        site = next(iter(sites))
    selected = sites.get(site)
    if not isinstance(site, str) or not site or not isinstance(selected, dict):
        raise ForemanError("configuration", "Selected Foreman site is not configured")
    endpoint = selected.get("endpoint")
    auth = selected.get("auth")
    if not isinstance(endpoint, str) or not endpoint or not isinstance(auth, dict):
        raise ForemanError(
            "configuration", "Foreman endpoint or authentication is missing"
        )
    strategy = auth.get("strategy")
    allowed = {
        "secret": {"strategy", "username_secret", "password_secret"},
        "quads": {"strategy", "infra_location", "convention_verified"},
    }
    if (
        not isinstance(strategy, str)
        or strategy not in allowed
        or set(auth) != allowed[strategy]
    ):
        raise ForemanError(
            "configuration", "Invalid Foreman authentication configuration"
        )
    if strategy == "secret" and any(
        not isinstance(auth[k], str) or not auth[k].strip()
        for k in ("username_secret", "password_secret")
    ):
        raise ForemanError("configuration", "Foreman secret references are missing")
    if strategy == "quads" and (
        auth["convention_verified"] is not True
        or not isinstance(auth["infra_location"], str)
        or not auth["infra_location"].strip()
    ):
        raise ForemanError(
            "configuration",
            "QUADS Foreman convention must be verified and have an infra location",
        )
    ca_bundle = selected.get("ca_bundle")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle):
        raise ForemanError("configuration", "Invalid Foreman CA bundle")
    provenance["site"] = site
    return dict(selected), provenance


async def foreman_credentials(
    config: dict[str, Any], secrets: Any, metadata: dict[str, Any]
) -> tuple[str, str]:
    """Resolve credentials only in service-side execution."""
    auth = config["auth"]
    if auth["strategy"] == "secret":
        username = await secrets.get_secret(auth["username_secret"])
        password = await secrets.get_secret(auth["password_secret"])
        if (
            not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
        ):
            raise ForemanError("configuration", "Foreman credentials are missing")
        return username, password
    from providers.resource.quads_foreman import derive_quads_credentials

    return derive_quads_credentials(auth, metadata)
