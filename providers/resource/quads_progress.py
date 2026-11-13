"""Ticket-visible, best-effort snapshots of a QUADS allocation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.server_utils import tool_progress
from providers.redaction import get_shared_redactor

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 600
_DETAIL_TIMEOUT_SECONDS = 8


def _text(value: Any, ticket_id: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        return "not reported"
    if len(value) > 2048:
        return "[QUADS text omitted: too long]"
    text = get_shared_redactor().redact_string(ticket_id, " ".join(value.split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flag(value: Any) -> str:
    return str(value).lower() if type(value) is bool else "unknown"


class QuadsProgressReporter:
    """Report observed QUADS state without interpreting stale move records."""

    def __init__(
        self,
        client: Any,
        ticket_id: str,
        assignment_id: int,
        cloud: str,
        hostnames: list[str],
    ) -> None:
        self.client = client
        self.ticket_id = ticket_id
        self.assignment_id = assignment_id
        self.cloud = cloud
        self.hostnames = hostnames
        self._last_snapshot: str | None = None
        self._last_report_elapsed = 0

        # QUADS errors are free text. Register the parsed credential values,
        # in addition to the secrets provider's raw JSON, before displaying them.
        self._redaction_ready = False
        try:
            redactor = get_shared_redactor()
            for name in ("password", "default_root_password"):
                value = getattr(client, name, None)
                if isinstance(value, str) and value:
                    redactor.register(ticket_id, f"quads/{name}", value)
            self._redaction_ready = True
        except Exception:
            logger.debug("Could not initialize QUADS progress redaction")

    async def _post(self, message: str) -> None:
        try:
            if not self._redaction_ready:
                return
            message = get_shared_redactor().redact_string(self.ticket_id, message)
            await tool_progress(message, "reserve_resources", ticket_id=self.ticket_id)
        except Exception:
            logger.debug("Could not post QUADS progress")

    async def assignment_created(self) -> None:
        await self._post(
            f"QUADS assignment {self.assignment_id} ({self.cloud}) created; "
            f"scheduling {len(self.hostnames)} host(s)."
        )

    async def __call__(
        self, assignment: dict[str, Any], elapsed: int, phase: str
    ) -> None:
        async def moves() -> list[dict[str, Any]] | None:
            try:
                return await self.client.get_move_progress(self.cloud)
            except Exception:
                logger.debug("Could not read QUADS move progress")
                return None

        semaphore = asyncio.Semaphore(8)

        async def host_status(hostname: str) -> dict[str, Any] | None:
            try:
                async with semaphore:
                    return await self.client.get_host_progress(hostname)
            except Exception:
                logger.debug("Could not read QUADS host progress")
                return None

        tasks = [
            asyncio.create_task(moves()),
            *(asyncio.create_task(host_status(host)) for host in self.hostnames),
        ]
        try:
            await asyncio.wait(tasks, timeout=_DETAIL_TIMEOUT_SECONDS)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        results = [None if task.cancelled() else task.result() for task in tasks]
        move_records = results[0]
        host_records = results[1:]
        by_host = (
            {
                item.get("host"): item
                for item in move_records
                if isinstance(item.get("host"), str) and item["host"] in self.hostnames
            }
            if move_records is not None
            else {}
        )

        lines = [
            f"QUADS assignment {self.assignment_id} ({self.cloud}): "
            f"provisioned={_flag(assignment.get('provisioned'))}, "
            f"validated={_flag(assignment.get('validated'))}."
        ]
        if move_records is None:
            lines.append("QUADS move status is unavailable.")
        for hostname, host in zip(self.hostnames, host_records, strict=True):
            move = by_host.get(hostname)
            fields = []
            if move_records is not None:
                if move is None:
                    fields.append("no move record reported")
                else:
                    fields.append(f"move={_text(move.get('status'), self.ticket_id)}")
                    if move.get("message"):
                        fields.append(
                            f"message={_text(move['message'], self.ticket_id)}"
                        )
                    if move.get("error_message"):
                        fields.append(
                            "last reported error="
                            f"{_text(move['error_message'], self.ticket_id)}"
                        )
                    if move.get("started_at"):
                        fields.append(
                            f"record start={_text(move['started_at'], self.ticket_id)}"
                        )
                    if move.get("completed_at"):
                        fields.append(
                            f"record end={_text(move['completed_at'], self.ticket_id)}"
                        )
            if host is None:
                fields.append("host status unavailable")
            else:
                fields.extend(
                    [
                        f"host validated={_flag(host.get('validated'))}",
                        f"build={_flag(host.get('build'))}",
                        f"switch configured={_flag(host.get('switch_config_applied'))}",
                    ]
                )
            lines.append(f"- {hostname}: {'; '.join(fields)}")

        snapshot = json.dumps(lines, ensure_ascii=False)
        if (
            phase == "waiting"
            and snapshot == self._last_snapshot
            and elapsed - self._last_report_elapsed < _HEARTBEAT_SECONDS
        ):
            return

        if phase == "validated":
            lines.append(f"QUADS reports the assignment validated after {elapsed}s.")
        elif phase == "timed_out":
            lines.append(
                f"Stopped waiting after {elapsed}s without validation. "
                "The assignment may still be active; reconcile it before retrying."
            )
        else:
            lines.append(
                f"Waiting for QUADS validation ({elapsed}s elapsed). "
                "Move records are last reported state and may be stale."
            )
        await self._post("\n".join(lines))
        self._last_snapshot = snapshot
        self._last_report_elapsed = elapsed
