"""Gathering Context agent: dedup gate against Investigation Records.

Checks whether an incoming anomaly matches an open Investigation
Record. If matched, the agent appends a build_history entry and
skips the full investigation. If no match, proceeds to planning.

Only runs for investigation-mode tickets (those routed to
gathering_context by triage). Ad-hoc tickets never enter this
status and are unaffected.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.investigation.models import is_inconclusive_outcome
from providers.llm.base import LLMProvider, LLMResponse

from .prompts import (
    EXTERNAL_PERF_DATA_GUIDANCE,
    EXTERNAL_PERF_TOOL_NAMES,
    GATHERING_CONTEXT_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


class GatheringContextAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(
            agent_name="gathering-context-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
        )

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        prompt = GATHERING_CONTEXT_SYSTEM_PROMPT
        tool_names = {t.name for t in self.tools} if self.tools else set()
        if tool_names & EXTERNAL_PERF_TOOL_NAMES:
            prompt += EXTERNAL_PERF_DATA_GUIDANCE
        return prompt

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        anomaly = cf.get("anomaly_context", {})
        hypothesis = cf.get("hypothesis", "")

        content = (
            f"## Investigation Ticket\n\n**Summary:** {ticket.get('summary', '')}\n\n"
        )

        if anomaly:
            content += "**Anomaly Context:**\n"
            # Present all anomaly fields — webhook tickets
            # may have different fields than manual ones.
            for key, val in anomaly.items():
                if val is not None and val != "":
                    content += f"- {key}: {val}\n"
            content += "\n"
        else:
            content += (
                "**No anomaly context found.** This ticket has no "
                "structured anomaly data to match against Investigation "
                "Records. Submit a NO_MATCH result to proceed.\n\n"
            )

        if hypothesis:
            content += f"**Hypothesis:** {hypothesis}\n\n"

        content += (
            "Check open Investigation Records for this subsystem "
            "and evaluate whether this anomaly has already been "
            "investigated.\n"
        )

        return [{"role": "user", "content": content}]

    async def _deterministic_dedup(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
    ) -> bool:
        """Check for dedup match using canonical dedup fields.

        The ticket's ``investigation_dedup`` (set at ingestion by webhook
        enrichment, ticket creation, or any upstream process)
        contains normalized ``metric`` and ``platform`` values.
        These are compared against open investigation records
        using exact matching.

        This is source-agnostic — any ticket creator that sets
        ``investigation_dedup.metric`` and ``investigation_dedup.platform`` gets
        deterministic dedup. Source-specific field extraction
        belongs in the webhook translator or enrichment layer,
        not here.

        Returns True if a match was found and the ticket was
        transitioned.
        """
        cf = ticket.get("custom_fields", {})
        # Fall back to the pre-rename ``dedup_key`` field for tickets
        # enriched before the rename.
        dedup_key = cf.get("investigation_dedup") or cf.get("dedup_key") or {}
        if not isinstance(dedup_key, dict):
            return False

        dedup_metric = dedup_key.get("metric", "")
        dedup_platform = dedup_key.get("platform", "")

        if not dedup_metric or not dedup_platform:
            return False

        from providers.investigation.registry import (
            create_record_provider,
        )

        try:
            provider = create_record_provider()
        except Exception:
            logger.debug(
                "[gathering-context] Investigation records "
                "provider not available for deterministic "
                "dedup"
            )
            return False

        try:
            # Fetch all matching open records without a
            # limit. The query orders by created_at, but
            # we select by min(updated_at). A record
            # created long ago but recently updated would
            # be missed by a small limit. Open records
            # are a small set (typically < 100) so
            # fetching all is safe.
            records = await provider.query(
                state="open",
                metric=dedup_metric,
                platform=dedup_platform,
            )
        except Exception:
            logger.warning(
                "[gathering-context] Failed to query "
                "investigation records for deterministic "
                "dedup — falling back to LLM"
            )
            return False

        if not records:
            return False

        # Temporal confidence decay (#702): older records
        # get reduced confidence or advisory-only treatment.
        # Use the most recently updated matching record.
        from datetime import datetime, timezone

        _FULL_CONFIDENCE_DAYS = 30
        _ADVISORY_ONLY_DAYS = 90

        now = datetime.now(timezone.utc)

        def _record_age(r: Any) -> int:
            ts = getattr(r, "updated_at", None) or getattr(r, "created_at", None)
            if ts is None:
                return 0  # No timestamp — treat as fresh
            return (now - ts).days

        # Pick the newest (lowest age) matching record
        matched = min(records, key=_record_age)
        matched_id = matched.investigation_id
        age_days = _record_age(matched)

        if age_days >= _ADVISORY_ONLY_DAYS:
            # Record is too old to block investigation.
            # Surface as context but proceed with fresh
            # investigation.
            logger.info(
                "[gathering-context] Deterministic dedup "
                "match %s is %d days old (>%d) — "
                "advisory only, proceeding to investigation",
                matched_id,
                age_days,
                _ADVISORY_ONLY_DAYS,
            )
            await self._update_fields(
                ticket_id,
                {
                    "dedup_advisory": {
                        "matched_investigation_id": matched_id,
                        "age_days": age_days,
                        "rationale": (
                            f"Record {matched_id} matches on "
                            f"metric/platform but is {age_days} "
                            f"days old. Proceeding with fresh "
                            f"investigation — root cause may "
                            f"no longer apply."
                        ),
                    },
                },
            )
            return False

        # Inconclusive records should not block new investigations.
        # The prior investigation failed to determine the cause, so a
        # new alert warrants a fresh attempt with potentially new data.
        # The record stays open and accumulates build history.
        metrics = getattr(matched, "operational_metrics", None)
        outcome = (getattr(metrics, "convergence_outcome", "") or "").strip().lower()
        if is_inconclusive_outcome(outcome):
            logger.info(
                "[gathering-context] Deterministic dedup "
                "match %s has outcome '%s' — proceeding "
                "with fresh investigation",
                matched_id,
                outcome or "none",
            )
            # Append build history so the record tracks this alert even
            # though we're not blocking. The advisory records whether
            # that append succeeded so _handle_completion only cancels
            # entries that actually exist.
            run_meta = cf.get("run_metadata", {})
            anomaly_ctx = cf.get("anomaly_context", {})
            build_id = run_meta.get(
                "build",
                str(anomaly_ctx.get("run_id", "unknown")),
            )
            from providers.investigation.models import BuildHistoryEntry

            history_appended = False
            try:
                await provider.append_build_history(
                    matched_id,
                    BuildHistoryEntry(
                        build_id=str(build_id),
                        ticket_id=ticket_id,
                        action="REINVESTIGATING",
                        comment=(
                            f"Prior outcome was '{outcome or 'none'}'; "
                            f"proceeding with fresh investigation from "
                            f"ticket {ticket_id}"
                        ),
                    ),
                )
                history_appended = True
            except Exception:
                logger.warning(
                    "[gathering-context] Failed to append build history "
                    "for inconclusive match",
                    exc_info=True,
                )
            await self._update_fields(
                ticket_id,
                {
                    "dedup_advisory": {
                        "matched_investigation_id": matched_id,
                        "prior_outcome": outcome or "none",
                        "age_days": age_days,
                        "history_appended": history_appended,
                        "rationale": (
                            f"Record {matched_id} matches on "
                            f"metric/platform but its outcome "
                            f"was '{outcome or 'none'}'. "
                            f"Proceeding with fresh "
                            f"investigation."
                        ),
                    },
                },
            )
            return False

        match_confidence = 1.0
        age_note = ""
        if age_days >= _FULL_CONFIDENCE_DAYS:
            match_confidence = 0.7
            age_note = (
                f" Record is {age_days} days old — "
                f"re-investigation recommended if "
                f"platform/software context has changed."
            )

        logger.info(
            "[gathering-context] Deterministic dedup "
            "match: %s (metric=%s, platform=%s, "
            "age=%dd, confidence=%.1f)",
            matched_id,
            dedup_metric,
            dedup_platform,
            age_days,
            match_confidence,
        )

        # Record that the anomaly persists in this build
        run_meta = cf.get("run_metadata", {})
        anomaly = cf.get("anomaly_context", {})
        build_id = run_meta.get(
            "build",
            str(anomaly.get("run_id", "unknown")),
        )
        from providers.investigation.models import (
            BuildHistoryEntry,
        )

        try:
            entry = BuildHistoryEntry(
                build_id=str(build_id),
                ticket_id=ticket_id,
                action="SKIP_MATCHED",
                comment=(f"Deterministic dedup from ticket {ticket_id}"),
            )
            await provider.append_build_history(
                matched_id,
                entry,
            )
            logger.info(
                f"[gathering-context] Appended build "
                f"history to {matched_id}: {build_id}"
            )
        except Exception as e:
            logger.warning(
                f"[gathering-context] Failed to append "
                f"build history to {matched_id}: {e}"
            )

        record_url = matched.record_url

        dedup_fields: dict[str, Any] = {
            "decision": "MATCH_FOUND",
            "matched_investigation_id": matched_id,
            "match_confidence": match_confidence,
            "match_rationale": (
                "Deterministic match on "
                f"metric='{dedup_metric}' "
                f"platform='{dedup_platform}'.{age_note}"
            ),
            "match_method": "deterministic",
            "record_age_days": age_days,
        }
        if record_url:
            dedup_fields["record_url"] = record_url

        await self._update_fields(
            ticket_id,
            {"dedup_result": dedup_fields},
        )

        record_label = f"[{matched_id}]({record_url})" if record_url else matched_id
        summary = (
            "**Dedup Match Found** "
            "(deterministic)\n\n"
            f"- **Matched Record:** {record_label}\n"
            f"- **Metric:** {dedup_metric}\n"
            f"- **Platform:** {dedup_platform}\n"
            f"- **Record Age:** {age_days} days\n"
            f"- **Confidence:** {match_confidence}\n\n"
            "Skipping investigation — this anomaly "
            "matches an open Investigation Record."
            f"{age_note}"
        )
        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "retrospective_pending",
            comment=(
                f"Deterministic dedup match: {matched_id}. Skipping investigation."
            ),
        )
        return True

    async def run(self, ticket_id: str) -> None:
        # Try deterministic dedup first — no LLM needed
        ticket = await self._get_ticket(ticket_id)
        if await self._deterministic_dedup(
            ticket_id,
            ticket,
        ):
            return

        gc_server = str(Path(__file__).with_name("server.py"))
        ir_server = str(Path(__file__).parent.parent / "investigation" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect_ticket_server(
            gc_server,
            name="gathering-context",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )
        await mcp.connect_ticket_server(
            ir_server,
            name="investigation-records",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )

        # Connect any configured external MCP servers
        # (e.g., domain knowledge, historical data).
        from agents.mcp_client import connect_external_servers

        connected_ext, ext_tools = await connect_external_servers(
            mcp, "gathering_context"
        )

        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        if ext_tools:
            from agents.mcp_client import filter_external_tools

            mcp_tools = filter_external_tools(
                mcp_tools, mcp._tool_routing, connected_ext, ext_tools
            )
        self._install_mcp_tools(mcp_tools)

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    async def _cancel_reinvestigation(
        self,
        investigation_id: str,
        ticket_id: str,
        comment: str,
    ) -> None:
        """Append a REINVESTIGATION_CANCELLED entry to a record.

        Used when deterministic dedup speculatively appended a
        REINVESTIGATING entry but the ticket did not ultimately
        reinvestigate that record.
        """
        from providers.investigation.models import BuildHistoryEntry
        from providers.investigation.registry import create_record_provider

        try:
            provider = create_record_provider()
            await provider.append_build_history(
                investigation_id,
                BuildHistoryEntry(
                    build_id="",
                    ticket_id=ticket_id,
                    action="REINVESTIGATION_CANCELLED",
                    comment=comment,
                ),
            )
        except Exception:
            logger.warning(
                "[gathering-context] Failed to cancel reinvestigation entry for %s",
                investigation_id,
                exc_info=True,
            )

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        result = self._get_submit_result(response)
        if result is None:
            result = self._parse_json_response(response.text)

        decision = result.get("decision", "NO_MATCH")
        matched_id = result.get("matched_investigation_id", "")
        confidence = result.get("match_confidence", 0.0)
        rationale = result.get("match_rationale", "")
        notes = result.get("notes", "")

        # The deterministic dedup step may have written a
        # dedup_advisory naming a record it matched on but did not
        # block on (inconclusive or too old). The advisory is only
        # authoritative for the record it names.
        advisory: Any = None
        try:
            ticket = await self._get_ticket(ticket_id)
            advisory = (ticket.get("custom_fields") or {}).get("dedup_advisory")
        except Exception:
            logger.warning(
                "[gathering-context] Could not load ticket %s for dedup advisory",
                ticket_id,
                exc_info=True,
            )
        advisory_id = (
            advisory.get("matched_investigation_id")
            if isinstance(advisory, dict)
            else None
        )
        advisory_history_appended = bool(
            isinstance(advisory, dict) and advisory.get("history_appended")
        )

        # Persist the decision on the ticket
        fields: dict[str, Any] = {
            "dedup_result": {
                "decision": decision,
                "matched_investigation_id": matched_id,
                "match_confidence": confidence,
                "match_rationale": rationale,
                "notes": notes,
            },
        }

        await self._update_fields(ticket_id, fields)

        # Resolve the matched record from the provider rather than
        # trusting LLM output — this gives us the canonical URL and
        # the persisted convergence outcome the LLM is unaware of.
        record = None
        record_outcome = ""
        if decision == "MATCH_FOUND" and matched_id:
            try:
                from providers.investigation.registry import (
                    create_record_provider,
                )

                provider = create_record_provider()
                record = await provider.get(matched_id)
                if record:
                    metrics = getattr(record, "operational_metrics", None)
                    record_outcome = (
                        (getattr(metrics, "convergence_outcome", "") or "")
                        .strip()
                        .lower()
                    )
                    fields["dedup_result"]["record_url"] = record.record_url
                    await self._update_fields(ticket_id, fields)
            except Exception:
                logger.warning(
                    "[gathering-context] Could not resolve record %s",
                    matched_id,
                    exc_info=True,
                )

        # Reinvestigate when the deterministic advisory named this
        # record, or the matched record itself is inconclusive. If the
        # advisory named a different record, its REINVESTIGATING entry
        # is orphaned and must be cancelled regardless of the outcome.
        reinvestigate = bool(
            decision == "MATCH_FOUND"
            and matched_id
            and (
                advisory_id == matched_id
                or (record is not None and is_inconclusive_outcome(record_outcome))
            )
        )
        if (
            decision == "MATCH_FOUND"
            and matched_id
            and advisory_id
            and advisory_id != matched_id
            and advisory_history_appended
        ):
            await self._cancel_reinvestigation(
                advisory_id,
                ticket_id,
                comment=f"LLM matched different record {matched_id}",
            )

        if decision == "MATCH_FOUND" and matched_id:
            record_url = record.record_url if record is not None else ""
            record_label = f"[{matched_id}]({record_url})" if record_url else matched_id

            if reinvestigate:
                if advisory_id == matched_id and isinstance(advisory, dict):
                    prior_outcome = advisory.get("prior_outcome", "none")
                else:
                    prior_outcome = record_outcome or "none"
                summary = (
                    f"**Reinvestigating**\n\n"
                    f"- **Matched Record:** {record_label}\n"
                    f"- **Prior Outcome:** {prior_outcome}\n"
                    f"- **Confidence:** {confidence}\n"
                    f"- **Rationale:** {rationale}\n\n"
                    f"Reinvestigating — prior outcome was "
                    f"{prior_outcome}. Linked to record {record_label}."
                )
                await self._add_comment(ticket_id, summary)
                await self._transition_ticket(
                    ticket_id,
                    "analyzing",
                    comment=(
                        f"Reinvestigating {matched_id} — "
                        f"prior outcome was '{prior_outcome}'."
                    ),
                )
            else:
                summary = (
                    f"**Dedup Match Found**\n\n"
                    f"- **Matched Record:** {record_label}\n"
                    f"- **Confidence:** {confidence}\n"
                    f"- **Rationale:** {rationale}\n\n"
                    f"Skipping full investigation — this anomaly "
                    f"matches an open Investigation Record."
                )
                await self._add_comment(ticket_id, summary)
                # Skip investigation, go to retrospective
                # for transcript analysis before closing
                await self._transition_ticket(
                    ticket_id,
                    "retrospective_pending",
                    comment=(f"Dedup match: {matched_id}. Skipping investigation."),
                )
        else:
            # LLM decided NO_MATCH. If deterministic dedup had
            # appended a REINVESTIGATING entry, clean it up.
            if advisory_id and advisory_history_appended:
                await self._cancel_reinvestigation(
                    advisory_id,
                    ticket_id,
                    comment="LLM dedup determined this is a different anomaly",
                )

            summary = (
                "**No Dedup Match**\n\n"
                "No open Investigation Records match this anomaly. "
                "Proceeding to data analysis."
            )
            if notes:
                summary += f"\n\n**Notes:** {notes}"
            await self._add_comment(ticket_id, summary)
            await self._transition_ticket(
                ticket_id,
                "analyzing",
                comment="No dedup match, analyzing existing data",
            )
