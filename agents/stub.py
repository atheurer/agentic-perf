from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class StubAgent:
    """Stub agent that skips the LLM loop and just advances the state machine."""

    def __init__(
        self,
        agent_name: str,
        target_status: str,
        state_store_url: str,
        custom_fields: dict[str, Any] | None = None,
        delay: float = 1.0,
    ) -> None:
        self.agent_name = agent_name
        self.target_status = target_status
        self.store_url = state_store_url.rstrip("/")
        self.custom_fields = custom_fields or {}
        self.delay = delay

    def _auth_headers(self) -> dict[str, str]:
        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def run(self, ticket_id: str) -> None:
        logger.info(f"[{self.agent_name}] Processing ticket {ticket_id}")

        async with httpx.AsyncClient(
            timeout=30.0, headers=self._auth_headers()
        ) as client:
            await asyncio.sleep(self.delay)

            if self.custom_fields:
                await client.patch(
                    f"{self.store_url}/api/v1/tickets/{ticket_id}/fields",
                    json={"fields": self.custom_fields},
                )

            await client.post(
                f"{self.store_url}/api/v1/tickets/{ticket_id}/comments",
                json={
                    "author": self.agent_name,
                    "body": f"Agent **{self.agent_name}** completed processing.",
                },
            )

            await client.post(
                f"{self.store_url}/api/v1/tickets/{ticket_id}/transition",
                json={
                    "status": self.target_status,
                    "comment": f"{self.agent_name} advancing state",
                },
            )

        logger.info(f"[{self.agent_name}] Done with {ticket_id}")

    async def close(self) -> None:
        pass
