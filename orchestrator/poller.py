from __future__ import annotations

import os
from typing import Any

import httpx


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def fetch_all_tickets(store_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        r = await client.get(
            f"{store_url}/api/v1/tickets",
            params={"exclude_status": "closed"},
        )
        r.raise_for_status()
        return r.json()
