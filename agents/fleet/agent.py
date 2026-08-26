"""Fleet coordinator agent — deterministic fleet iteration lifecycle.

One job: after each benchmark, platform failure, or resource
exhaustion during a fleet investigation, record the per-host
result and route to the next step.

This agent is fully deterministic — no LLM calls. It reads
ticket state and makes code-enforced routing decisions:

- After benchmark/platform: record result, route to
  ``awaiting_hardware`` for the next board
- After resource exhaustion (no untested boards available):
  set ``fleet_exhausted`` and route to
  ``evaluating_convergence``
- Error → ``awaiting_customer_guidance``

Board selection is NOT the coordinator's job — the resource
agent handles it via ``exclude_hosts`` filtering through the
provider-agnostic ``check_available_resources`` interface.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.events import EventBus
from providers.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class FleetCoordinatorAgent:
    """Deterministic fleet iteration coordinator.

    Not an AgentBase subclass — no LLM loop needed.
    Runs purely on ticket state.
    """

    agent_name = "fleet-coordinator"

    def __init__(
        self,
        *,
        llm_provider: LLMProvider | None = None,
        state_store_url: str = "http://localhost:8090",
        event_bus: EventBus | None = None,
    ) -> None:
        self.store_url = state_store_url
        self._events = event_bus
        # LLM provider accepted but unused — keeps dispatcher
        # interface consistent.
        import httpx

        from state_store.auth import read_token_from_file

        token = read_token_from_file()
        self._client = httpx.AsyncClient(
            base_url=state_store_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def run(self, ticket_id: str) -> None:
        """Coordinate one fleet iteration step."""
        try:
            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            from providers.fleet import get_fleet_progress

            progress = get_fleet_progress(cf)
            self._emit(
                ticket_id,
                "agent_started",
                {
                    "tested": progress["tested"],
                    "completed": progress["completed"],
                    "partial": progress["partial"],
                },
            )
            await self._coordinate(ticket_id)
        except Exception as e:
            logger.error(
                f"[fleet-coordinator] {ticket_id}: {e}",
                exc_info=True,
            )
            self._emit(
                ticket_id,
                "agent_error",
                {"reason": str(e)},
            )
            await self._transition(
                ticket_id,
                "awaiting_customer_guidance",
                f"Fleet coordinator error: {e}",
            )
        finally:
            self._emit(ticket_id, "agent_finished", {})
            await self._client.aclose()

    async def _coordinate(self, ticket_id: str) -> None:
        from providers.fleet import (
            get_fleet_progress,
            get_tested_host_ids,
            record_host_result,
        )

        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})

        # Determine what happened in the previous step by
        # checking ticket state.
        board = cf.get("platform_board", "unknown")
        lease_id = cf.get("resource_reservation_id", "")
        ip = cf.get("platform_ip", "")
        platform_ready = cf.get("platform_ready", False)
        benchmark_status = cf.get("benchmark_status")

        # No board assigned — resource agent couldn't
        # find one and routed here directly.
        if board == "unknown" or not board:
            fleet = dict(cf.get("fleet_investigation", {}))
            fleet["fleet_exhausted"] = {"hard": True}
            await self._update_fields(
                ticket_id,
                {"fleet_investigation": fleet},
            )
            progress = get_fleet_progress({"fleet_investigation": fleet})
            await self._add_comment(
                ticket_id,
                f"**Fleet complete:** "
                f"{progress['tested']} hosts tested. "
                f"No boards available.",
            )
            await self._transition(
                ticket_id,
                "evaluating_convergence",
                "Fleet exhaustion: no board assigned",
            )
            return

        # Duplicate board detection: if the provider assigned
        # a board we already tested, treat as soft exhaustion.
        tested_ids = get_tested_host_ids(cf)
        if board in tested_ids:
            fleet = dict(cf.get("fleet_investigation", {}))
            fleet["fleet_exhausted"] = {
                "soft": True,
                "unavailable_hosts": ["(duplicate assignment)"],
            }
            await self._update_fields(
                ticket_id,
                {"fleet_investigation": fleet},
            )
            progress = get_fleet_progress({"fleet_investigation": fleet})
            await self._add_comment(
                ticket_id,
                f"**Fleet exhausted (soft):** board "
                f"{board} was assigned again — no new "
                f"boards available in the pool. "
                f"{progress['tested']} hosts tested.",
            )
            await self._transition(
                ticket_id,
                "evaluating_convergence",
                f"Fleet soft exhaustion: duplicate {board}",
            )
            return

        if not platform_ready:
            # Platform provisioning failed — record partial.
            diag = self._get_latest_diagnostic(ticket)
            await record_host_result(
                self._update_fields,
                ticket_id,
                cf,
                host_id=board,
                lease_id=lease_id,
                ip=ip,
                status="partial",
                failure_reason=diag[:500] if diag else "provisioning failed",
            )
            await self._add_comment(
                ticket_id,
                f"Fleet: recorded {board} as partial (provisioning failure).",
            )
        elif benchmark_status == "failed":
            # Benchmark failed — record partial with any data.
            notes = cf.get("benchmark_notes", "benchmark failed")
            await record_host_result(
                self._update_fields,
                ticket_id,
                cf,
                host_id=board,
                lease_id=lease_id,
                ip=ip,
                status="partial",
                metrics=cf.get("benchmark_kpis"),
                failure_reason=str(notes)[:500],
            )
            await self._add_comment(
                ticket_id,
                f"Fleet: recorded {board} as partial (benchmark failure).",
            )
        else:
            # Benchmark succeeded — record completed.
            await record_host_result(
                self._update_fields,
                ticket_id,
                cf,
                host_id=board,
                lease_id=lease_id,
                ip=ip,
                status="completed",
                metrics=cf.get("benchmark_kpis"),
            )
            await self._add_comment(
                ticket_id,
                f"Fleet: recorded {board} as completed.",
            )

        # Route to the next board. All resource-exhaustion
        # cases are handled above (no board assigned,
        # duplicate detection). If we reach here, a host
        # was just recorded — acquire the next one.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        progress = get_fleet_progress(cf)

        await self._add_comment(
            ticket_id,
            f"**Fleet iteration {progress['tested']}** complete. Acquiring next board.",
        )
        # Emit epoch marker so agents in the next
        # iteration don't count previous iterations
        # against their per-agent budget.
        self._emit(
            ticket_id,
            "fleet_iteration_epoch",
            {"iteration": progress["tested"]},
        )
        await self._transition(
            ticket_id,
            "awaiting_hardware",
            f"Fleet: {progress['tested']} tested, acquiring next board",
        )

    def _get_latest_diagnostic(self, ticket: dict[str, Any]) -> str:
        """Extract the most recent failure diagnostic."""
        for comment in reversed(ticket.get("comments", [])):
            body = comment.get("body", "")
            if "Failed" in body or "Diagnostics" in body:
                return body[:500]
        return ""

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
