"""Evaluate agent: convergence assessment for investigation loops.

Reads benchmark results, ledger history, and convergence criteria
to decide whether the investigation should loop back (refine
parameters, re-provision hardware) or advance to synthesis.

Deterministic convergence checks run first (max_iterations,
statistical thresholds, info gain stall). If none fire, the LLM
reasons about the four convergence gates: Isolation, Entropy Stall,
Manual Interruption, Expected Regression.

Only runs for investigation-mode tickets. Ad-hoc benchmark tickets
go to the review agent instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .prompts import EVALUATE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class EvaluateAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(
            agent_name="evaluate-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
            # Termination driven by convergence gates and
            # budget guardrails, not iteration count.
            max_iterations=0,
        )
        # Set by _build_messages, read by _handle_completion
        self._deterministic_outcome: str = ""

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return EVALUATE_SYSTEM_PROMPT

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        hypothesis = cf.get("hypothesis", "")
        ledger = cf.get("investigation_ledger", [])
        plan = cf.get("execution_plan", {})
        criteria = cf.get("convergence_criteria", {})
        anomaly = cf.get("anomaly_context", {})

        content = (
            f"## Investigation Ticket\n\n"
            f"**Ticket ID:** {ticket.get('id', '')}\n"
            f"**Summary:** {ticket.get('summary', '')}\n\n"
            f"When using infra tools (set_ssh_context, check_host, "
            f"execute_command), use the ticket ID above — not a "
            f"run ID.\n\n"
        )

        if anomaly:
            content += (
                f"**Anomaly Context:**\n"
                f"- Subsystem: {anomaly.get('subsystem', '')}\n"
                f"- Metric: {anomaly.get('metric', '')}\n"
                f"- Direction: {anomaly.get('direction', '')}\n"
                f"- Platform: {anomaly.get('platform', '')}\n"
                f"- Magnitude: {anomaly.get('magnitude', '')}\n\n"
            )

        content += f"**Working Hypothesis:** {hypothesis}\n\n"

        # Deterministic convergence check — run before
        # the LLM. If a hard gate fires, the outcome is
        # included as context but also enforced in code
        # in _handle_completion via _deterministic_outcome.
        self._deterministic_outcome = self._check_deterministic(cf)
        if self._deterministic_outcome:
            content += (
                f"**⚠️ Deterministic convergence check: "
                f"{self._deterministic_outcome}**\n"
                f"This gate has been enforced by the system. "
                f"Your response should reflect this outcome.\n\n"
            )

        if criteria:
            content += (
                f"**Convergence Criteria:**\n"
                f"```json\n{json.dumps(criteria, indent=2)}\n"
                f"```\n\n"
            )

        # Execution plan steps with results
        steps = plan.get("steps", [])
        if steps:
            content += "**Execution Plan Steps:**\n"
            for s in steps:
                status = s.get("status", "?")
                results = s.get("results", {})
                params = s.get("params", {})
                content += (
                    f"- Step {s.get('id', '?')}: {s.get('agent_type', '?')} [{status}]"
                )
                if params:
                    content += f" params={json.dumps(params)}"
                if results:
                    content += f" results={json.dumps(results)}"
                content += "\n"
            content += "\n"

        # Investigation ledger
        if ledger:
            content += "**Investigation Ledger:**\n"
            for entry in ledger:
                content += (
                    f"- Iteration {entry.get('iteration', '?')}: "
                    f'hypothesis="{entry.get("hypothesis", "")}"'
                    f' → conclusion="'
                    f'{entry.get("conclusion", "")}"'
                    f" (info_gain={entry.get('info_gain', 0.0)})\n"
                )
            content += "\n"
        else:
            content += (
                "**No prior ledger entries.** This is the first "
                "evaluation after the initial benchmark run.\n\n"
            )

        # Change context (for Expected Regression gate)
        change_ctx = cf.get("change_context")
        if change_ctx:
            content += (
                f"**Change Context:**\n"
                f"```json\n{json.dumps(change_ctx, indent=2)}\n"
                f"```\n\n"
            )

        # Latest benchmark results
        run_id = cf.get("run_id", "")
        bench_status = cf.get("benchmark_status", "")
        output_dir = cf.get("output_dir", "")
        if run_id:
            content += (
                f"**Latest Benchmark Result:**\n"
                f"- Run ID: {run_id}\n"
                f"- Status: {bench_status}\n"
            )
            if output_dir:
                content += (
                    f"- Output Dir: {output_dir}\n"
                    f"  (use this path with list_benchmark_artifacts "
                    f"and read_benchmark_artifact)\n"
                )
            content += "\n"

        content += (
            "Evaluate the convergence gates and submit your "
            "decision. If infra tools are available, you may "
            "query the host for detailed benchmark results "
            "before deciding.\n"
        )

        return [{"role": "user", "content": content}]

    def _check_deterministic(
        self,
        custom_fields: dict[str, Any],
    ) -> str:
        """Run deterministic convergence checks.

        Returns a human-readable outcome string if a gate fired,
        or empty string if no deterministic gate matched.
        """
        try:
            from providers.convergence import (
                ConvergenceOutcome,
                criteria_from_custom_fields,
                evaluate_deterministic,
                results_from_custom_fields,
            )

            criteria = criteria_from_custom_fields(custom_fields)
            results = results_from_custom_fields(custom_fields)
            outcome = evaluate_deterministic(criteria, results)

            if outcome == ConvergenceOutcome.CONVERGED:
                return "CONVERGED — deterministic threshold met"
            if outcome == ConvergenceOutcome.MAX_ITERATIONS:
                return f"MAX_ITERATIONS — reached {criteria.max_iterations} iterations"
        except Exception:
            logger.debug(
                "Deterministic convergence check failed",
                exc_info=True,
            )

        # Budget exhaustion: if the previous agent was
        # paused due to budget, treat as a resource
        # exhaustion convergence signal.
        comments = custom_fields.get("comments", [])
        if any(
            "budget exhausted" in str(c).lower()
            for c in (
                custom_fields.get("benchmark_status", ""),
                *(c.get("body", "") for c in comments if isinstance(c, dict)),
            )
        ):
            return (
                "BUDGET_EXHAUSTED — the benchmark agent "
                "ran out of LLM budget. Assess any "
                "partial results that were submitted "
                "before the budget was exhausted."
            )

        return ""

    async def run(self, ticket_id: str) -> None:
        # NOTE on tool count: This agent connects to 3 MCP
        # servers (evaluate, investigation-records, infra),
        # exposing up to ~14 tools to the LLM. The RHIVOS
        # architecture warns about tool proliferation
        # degrading LLM performance. The investigation
        # records tools should eventually be absorbed into
        # the Domain MCP per the architecture's "Tool Count
        # Discipline" guidance. For now, graceful degradation
        # (try/except on connect) keeps the agent functional
        # with fewer servers when not all are available.
        eval_server = str(Path(__file__).with_name("server.py"))
        ir_server = str(Path(__file__).parent.parent / "investigation" / "server.py")
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(eval_server, name="evaluate")

        # Investigation records — may not be configured
        try:
            await mcp.connect(
                ir_server,
                name="investigation-records",
            )
        except Exception:
            logger.info(
                "[evaluate-agent] Investigation records MCP not available — skipping"
            )

        # Infra — may not be available for result queries
        try:
            await mcp.connect(infra_server, name="infra")
        except Exception:
            logger.info(
                "[evaluate-agent] Infra MCP not available "
                "— working with ticket data only"
            )

        self._mcp = mcp
        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        result = self._get_submit_result(response)
        if result is None:
            result = self._parse_json_response(response.text)

        decision = result.get("decision", "stalled")
        gate = result.get("convergence_gate", "")
        confidence = result.get("confidence", 0.0)
        hypothesis = result.get("updated_hypothesis", "")
        info_gain = result.get("info_gain", 0.0)
        params_rationale = result.get("params_rationale", "")
        next_params = result.get("next_params", "")
        root_cause = result.get("root_cause_summary", "")
        notes = result.get("notes", "")

        # Enforce deterministic convergence — code overrides
        # the LLM if a hard gate fired. The LLM's analysis
        # is still captured in the ledger but the transition
        # decision is code-enforced.
        det = getattr(self, "_deterministic_outcome", "")
        if det and decision in ("loop_plan", "loop_provision"):
            logger.info(
                f"[{self.agent_name}] Overriding LLM decision "
                f"'{decision}' with deterministic outcome: {det}"
            )
            if "MAX_ITERATIONS" in det:
                decision = "stalled"
                gate = "max_iterations"
                notes = (
                    f"Deterministic override: {det}. "
                    f"LLM wanted to {result.get('decision')}: "
                    f"{notes}"
                )
            else:
                decision = "converged"
                gate = "deterministic_threshold"
                notes = (
                    f"Deterministic override: {det}. "
                    f"LLM wanted to {result.get('decision')}: "
                    f"{notes}"
                )

        # Determine which plan steps this evaluation covers
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        plan = cf.get("execution_plan", {})
        # The evaluation covers the most recently completed step
        completed_steps = [
            s.get("id", i)
            for i, s in enumerate(plan.get("steps", []))
            if s.get("status") == "completed"
        ]
        # Reference the last completed step(s) since the
        # previous ledger entry
        ledger = cf.get("investigation_ledger", [])
        prev_max_step = -1
        if ledger:
            prev_steps = ledger[-1].get("plan_steps", [])
            if prev_steps:
                prev_max_step = max(prev_steps)
        eval_steps = [s for s in completed_steps if s > prev_max_step]

        # Append ledger entry
        await self._append_ledger_entry(
            ticket_id,
            iteration=len(ledger) + 1,
            plan_steps=eval_steps,
            hypothesis=hypothesis or cf.get("hypothesis", ""),
            params_rationale=params_rationale,
            conclusion=root_cause if root_cause else notes,
            info_gain=info_gain,
        )

        # Populate iteration_results for deterministic
        # convergence checks on subsequent iterations.
        # Maps ledger entries to the IterationResult format
        # that evaluate_deterministic() consumes.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        updated_ledger = cf.get("investigation_ledger", [])
        iteration_results = [
            {
                "iteration": entry.get("iteration", i),
                "info_gain": entry.get("info_gain", 0.0),
                "summary": entry.get("conclusion", ""),
            }
            for i, entry in enumerate(updated_ledger)
        ]

        # Persist evaluation result and iteration_results
        eval_fields: dict[str, Any] = {
            "iteration_results": iteration_results,
            "evaluation_result": {
                "decision": decision,
                "convergence_gate": gate,
                "confidence": confidence,
                "info_gain": info_gain,
                "root_cause_summary": root_cause,
                "notes": notes,
            },
        }
        await self._update_fields(ticket_id, eval_fields)

        if decision in ("converged", "stalled"):
            summary = (
                f"**Convergence: {decision.upper()}**\n\n"
                f"- **Gate:** {gate or 'LLM assessment'}\n"
                f"- **Confidence:** {confidence}\n"
            )
            if root_cause:
                summary += f"- **Root Cause:** {root_cause}\n"
            if notes:
                summary += f"- **Notes:** {notes}\n"

            await self._add_comment(ticket_id, summary)
            await self._transition_ticket(
                ticket_id,
                "synthesizing_results",
                comment=(
                    f"Convergence: {decision} "
                    f"(gate={gate or 'llm'}, "
                    f"confidence={confidence})"
                ),
            )

        elif decision == "loop_plan":
            summary = (
                f"**Loop Back: Refine Parameters**\n\n"
                f"- **Hypothesis:** {hypothesis}\n"
                f"- **Rationale:** {params_rationale}\n"
                f"- **Info Gain:** {info_gain}\n"
            )
            if next_params:
                summary += f"- **Next Params:** {next_params}\n"

            await self._add_comment(ticket_id, summary)

            # Append a new plan step for the next iteration.
            # NOTE: This writes directly to execution_plan
            # rather than going through _advance_plan() in the
            # orchestrator. This is safe because:
            # - _advance_plan() only fires after an agent
            #   completes for plan-relevant statuses
            # - The evaluate agent is transitioning AWAY from
            #   evaluating_convergence, so _advance_plan()
            #   won't run for this status
            # - The new step is appended as "pending" — it
            #   won't be advanced until the ticket re-enters
            #   executing_benchmark
            # If _advance_plan() gains the ability to modify
            # pending steps, this coordination must be revisited.
            if next_params:
                try:
                    params = json.loads(next_params)
                except (json.JSONDecodeError, TypeError):
                    params = {"raw": next_params}
            else:
                params = {}

            # Read fresh ticket for plan modification
            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            plan = cf.get("execution_plan", {})
            steps = plan.get("steps", [])
            new_step = {
                "id": len(steps),
                "agent_type": "benchmark",
                "status": "pending",
                "params": params,
                "results": {},
            }
            steps.append(new_step)
            plan["steps"] = steps
            await self._update_fields(
                ticket_id,
                {"execution_plan": plan},
            )

            await self._transition_ticket(
                ticket_id,
                "planning_investigation",
                comment=(
                    f"Loop back: refining parameters (hypothesis: {hypothesis[:60]})"
                ),
            )

        elif decision == "loop_provision":
            summary = (
                f"**Loop Back: Re-provision Hardware**\n\n"
                f"- **Reason:** {params_rationale or notes}\n"
                f"- **Info Gain:** {info_gain}\n"
            )
            await self._add_comment(ticket_id, summary)
            await self._transition_ticket(
                ticket_id,
                "awaiting_provision",
                comment="Loop back: re-provisioning hardware",
            )

        else:
            # Unknown decision — pause for human guidance
            await self._add_comment(
                ticket_id,
                f"**Evaluation produced unexpected decision: "
                f"{decision}**\n\nPausing for human guidance.",
            )
            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment=(f"Unexpected evaluation decision: {decision}"),
            )
