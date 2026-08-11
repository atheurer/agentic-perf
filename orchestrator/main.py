from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import signal
import sys
import time
from typing import Any

from agents.base import HITLDriftError
from paths import LOCK_FILE
from providers.events import EventBus
from providers.llm.factory import create_llm_provider
from providers.secrets.local import LocalSecretsProvider
from providers.skills.arcaflow_plugins import ArcaflowPluginSkillProvider
from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
from providers.skills.clusterbuster import ClusterbusterSkillProvider
from providers.skills.crucible import CrucibleSkillProvider
from providers.skills.forge import ForgeSkillProvider
from providers.skills.ioscale import IoscaleSkillProvider
from providers.skills.k8s_netperf import K8sNetperfSkillProvider
from providers.skills.kube_burner import KubeBurnerSkillProvider
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider
from providers.skills.repo_cache import RepoCache
from providers.skills.vstorm import VstormSkillProvider
from providers.skills.zathras import ZathrasSkillProvider

from .config import OrchestratorConfig
from .dispatcher import STATUS_AGENT_MAP, Dispatcher
from .handoff import check_handoff
from .poller import fetch_all_tickets

logger = logging.getLogger(__name__)


def _make_llm_provider(config: OrchestratorConfig, provider: str = "", model: str = ""):
    return create_llm_provider(
        provider=provider or config.llm_provider,
        model=model or config.llm_model,
        api_key=config.anthropic_api_key,
        backend=config.llm_backend,
        project_id=config.llm_project_id,
        region=config.llm_region,
        base_url=config._openai_base_url,
        gemini_api_key=config._gemini_api_key,
    )


def _make_llm_factory(config: OrchestratorConfig):
    def factory(agent_type: str):
        agent_cfg = config.get_agent_llm_config(agent_type)
        provider = _make_llm_provider(
            config,
            provider=agent_cfg.get("provider", ""),
            model=agent_cfg.get("model", ""),
        )
        provider.default_timeout = config.llm_timeout
        effort = agent_cfg.get("reasoning_effort") or config.llm_reasoning_effort
        if effort:
            provider.reasoning_effort = effort
        max_tokens = agent_cfg.get("max_tokens")
        provider.max_tokens = int(max_tokens) if max_tokens else config.llm_max_tokens
        return provider

    return factory


PLAN_AGENT_STATUS = {
    "teardown": "awaiting_teardown",
    "resource": "awaiting_hardware",
    "provision": "awaiting_provision",
    "benchmark": "executing_benchmark",
    "review": "awaiting_review",
    "analyze": "analyzing",
}


def _capture_step_results(agent_type: str, cf: dict) -> dict:
    """Snapshot agent-type-specific fields from custom_fields.

    Called when a plan step completes so per-iteration state
    (IPs, run_ids, provisioning info) survives teardown.
    """
    if agent_type == "benchmark":
        return {
            "run_id": cf.get("run_id", ""),
            "benchmark_status": cf.get("benchmark_status", ""),
            "benchmark_duration": cf.get("benchmark_duration"),
            "run_file_used": cf.get("run_file_used", {}),
        }
    elif agent_type == "resource":
        return {
            "assigned_hardware_ips": cf.get("assigned_hardware_ips", {}),
            "ssh_hardware_ips": cf.get("ssh_hardware_ips", {}),
            "ssh_user": cf.get("ssh_user", ""),
            "ssh_key_path": cf.get("ssh_key_path", ""),
            "resource_provider": cf.get("resource_provider", ""),
            "resource_reservation_id": cf.get(
                "resource_reservation_id",
                "",
            ),
            "resource_provider_metadata": cf.get(
                "resource_provider_metadata",
                {},
            ),
        }
    elif agent_type == "provision":
        return {
            "provisioning_complete": cf.get("provisioning_complete", False),
            "hosts_provisioned": cf.get("hosts_provisioned", []),
            "harness_name": cf.get("harness_name", ""),
            "harness_version": cf.get("harness_version", ""),
            "configuration_applied": cf.get("configuration_applied", {}),
        }
    elif agent_type == "teardown":
        return {"teardown_complete": True}
    elif agent_type == "review":
        return {
            "verdict": cf.get("verdict", ""),
            "review_summary": cf.get("review_summary", ""),
        }
    return {}


# parsed_specs keys that imply host-level NIC/kernel tuning is required.
# If any of these are present, the provisioning agent must have applied
# and verified the tuning (see agents/provisioning/prompts.py) — the
# benchmark agent has no tools to do this itself.
_HOST_TUNING_SPEC_KEYS = (
    "irq_pinning_cpu",
    "combined_queues",
    "congestion_control",
    "qdisc",
)


def _missing_host_tuning(cf: dict) -> str:
    """Return a comma-separated list of requested tuning fields if the
    ticket's parsed_specs requires host tuning but configuration_applied
    is empty. Empty string if tuning wasn't requested or was recorded.
    """
    parsed_specs = cf.get("parsed_specs") or {}
    requested = [k for k in _HOST_TUNING_SPEC_KEYS if k in parsed_specs]
    if requested and not cf.get("configuration_applied"):
        return ", ".join(requested)
    return ""


def _apply_step_overrides(
    store_url: str,
    client: object,
    ticket_id: str,
    next_step: dict,
    cf: dict,
) -> None:
    """Write step-level param overrides to ticket custom_fields.

    Resource steps can carry per-step required_hosts, directives,
    and scoped_context. Provision steps can carry per-step directive
    merges. Resource steps also clear stale provisioning state so the
    provisioning agent re-runs, and replace scoped_context for the
    agent's section so stale multi-iteration text doesn't mislead.
    """
    agent_type = next_step.get("agent_type", "")
    step_params = next_step.get("params", {})
    override_fields: dict = {}

    if agent_type == "teardown":
        if step_params.get("preserve_roles"):
            override_fields["teardown_preserve_roles"] = step_params["preserve_roles"]

    if agent_type == "resource":
        if step_params.get("required_hosts"):
            override_fields["required_hosts"] = step_params["required_hosts"]
        override_fields["provisioning_complete"] = False
        override_fields["hosts_provisioned"] = []

    if agent_type in ("resource", "provision"):
        if step_params.get("directives"):
            existing = dict(cf.get("directives", {}))
            existing.update(step_params["directives"])
            override_fields["directives"] = existing

    # When a benchmark step follows an inconclusive analysis,
    # merge the analysis agent's suggested params into the step.
    if agent_type == "benchmark":
        analysis = cf.get("analysis_result", {})
        if not analysis.get("conclusive") and analysis.get("benchmark_needed"):
            suggested = analysis["benchmark_needed"].get("suggested_params", {})
            if suggested:
                existing_params = dict(step_params)
                # Suggested params fill gaps but don't override
                # explicit plan params set by triage.
                for k, v in suggested.items():
                    if k not in existing_params:
                        existing_params[k] = v
                next_step["params"] = existing_params

    # Apply per-step scoped_context if provided, or clear the
    # agent's section so it falls back to structured data
    # (required_hosts) instead of stale ticket-level text.
    scoped = dict(cf.get("scoped_context", {}))
    if step_params.get("scoped_context"):
        scoped.update(step_params["scoped_context"])
        override_fields["scoped_context"] = scoped
    elif agent_type in ("resource", "provision", "benchmark", "review"):
        agent_key = {
            "resource": "resource",
            "provision": "provisioning",
            "benchmark": "benchmark",
            "review": "review",
        }.get(agent_type)
        if agent_key and agent_key in scoped:
            del scoped[agent_key]
            override_fields["scoped_context"] = scoped

    if override_fields:
        client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={"fields": override_fields},
        )


def _advance_plan(
    store_url: str,
    ticket_id: str,
    completed_status: str,
    event_bus: EventBus | None = None,
) -> None:
    """Advance the execution plan after an agent completes a step.

    Snapshots step results, applies per-step param overrides for the
    next step, and transitions the ticket to the next step's status.
    Only advances if the completed agent matches the current step's
    agent_type.
    """
    import httpx

    client = httpx.Client(timeout=10.0, headers=_auth_headers())
    try:
        r = client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
        if r.status_code != 200:
            return
        ticket = r.json()
        cf = ticket.get("custom_fields", {})
        plan = cf.get("execution_plan")
        if not plan:
            return

        steps = plan.get("steps", [])
        current = plan.get("current_step", 0)

        if current >= len(steps):
            logger.debug(
                f"[advance-plan] {ticket_id}: step index {current} past end of plan"
            )
            return

        step = steps[current]
        if step.get("status") != "in_progress":
            logger.debug(
                f"[advance-plan] {ticket_id}: step {current} "
                f"status is {step.get('status')!r}, not in_progress"
            )
            return

        expected_status = PLAN_AGENT_STATUS.get(step.get("agent_type", ""))
        if expected_status != completed_status:
            logger.debug(
                f"[advance-plan] {ticket_id}: completed "
                f"{completed_status} but step expects "
                f"{expected_status}"
            )
            return

        ticket_status = ticket.get("status", "")
        if ticket_status == "awaiting_customer_guidance":
            logger.debug(
                f"[advance-plan] {ticket_id}: ticket is at guidance, deferring"
            )
            return
        if cf.get("abort_requested"):
            return

        if step.get("agent_type") == "provision":
            missing = _missing_host_tuning(cf)
            if missing:
                logger.warning(
                    f"[advance-plan] {ticket_id}: parsed_specs requests host "
                    f"tuning ({missing}) but configuration_applied is empty "
                    f"— blocking advance to benchmark"
                )
                client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                    json={
                        "status": "awaiting_customer_guidance",
                        "comment": (
                            "Provisioning reported complete, but the ticket "
                            f"requests host tuning ({missing}) and no "
                            "configuration_applied was recorded. The "
                            "provisioning agent may have deferred this work "
                            "incorrectly — the benchmark agent has no tools "
                            "to apply NIC/IRQ tuning. Reply to have "
                            "provisioning re-run tuning, or override if this "
                            "was intentional."
                        ),
                    },
                )
                return

        step["status"] = "completed"
        step["results"] = _capture_step_results(
            step.get("agent_type", ""),
            cf,
        )

        run_ids = plan.get("run_ids", [])
        if cf.get("run_id") and cf["run_id"] not in run_ids:
            run_ids.append(cf["run_id"])
        plan["run_ids"] = run_ids

        next_idx = current + 1

        # Conclusive analysis: skip hardware/benchmark steps
        # and jump directly to review.
        if step.get("agent_type") == "analyze" and cf.get("analysis_result", {}).get(
            "conclusive"
        ):
            for skip_idx in range(next_idx, len(steps)):
                skip_step = steps[skip_idx]
                if skip_step["agent_type"] == "review":
                    next_idx = skip_idx
                    break
                skip_step["status"] = "skipped"

        plan["current_step"] = next_idx

        if next_idx < len(steps):
            next_step = steps[next_idx]
            next_status = PLAN_AGENT_STATUS.get(next_step["agent_type"])
            # Insert preparing_platform before provision
            # so the platform agent can run system
            # provisioning (flash, kickstart) before
            # harness installation.
            if (
                next_status == "awaiting_provision"
                and completed_status == "awaiting_hardware"
            ):
                next_status = "preparing_platform"
            if next_status:
                next_step["status"] = "in_progress"

                # Apply step overrides BEFORE saving the plan
                # so that mutations (e.g. analysis-informed
                # benchmark params) are persisted.
                _apply_step_overrides(
                    store_url,
                    client,
                    ticket_id,
                    next_step,
                    cf,
                )

                client.patch(
                    f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                    json={
                        "fields": {
                            "execution_plan": plan,
                            "review_submitted": None,
                        },
                    },
                )

                label = next_step.get("params", {}).get(
                    "label",
                    next_step["agent_type"],
                )
                client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/comments",
                    json={
                        "author": "orchestrator",
                        "body": (
                            f"**Plan step {current} complete** — "
                            f"advancing to step {next_idx} "
                            f"({next_step['agent_type']}: {label})"
                        ),
                    },
                )

                comment = (
                    f"Plan advancing to step {next_idx}: {next_step['agent_type']}"
                )
                client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                    json={"status": next_status, "comment": comment},
                )
                return

        client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {
                    "execution_plan": plan,
                    "review_submitted": None,
                },
            },
        )
    finally:
        client.close()


async def run_agent_task(
    dispatcher: Dispatcher,
    status: str,
    ticket_id: str,
    config: OrchestratorConfig | None = None,
    agent_task_timeout: float = 0,
    ticket_data: dict | None = None,
):
    agent = None
    success = False

    try:
        agent = dispatcher.create_agent(status, ticket_data=ticket_data)
        if agent is None:
            return

        dispatcher.set_agent(ticket_id, agent)

        # Investigation tickets get unlimited iterations for
        # all agents — convergence gates and budget guardrails
        # handle termination, not arbitrary iteration caps.
        # Without this, agents like the benchmark agent exhaust
        # their default max_iterations re-reading skills and
        # host state on each investigation loop-back.
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=10.0, headers=_auth_headers()
            ) as client:
                r = await client.get(
                    f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}"
                )
                if r.status_code == 200:
                    cf = r.json().get("custom_fields", {})
                    if cf.get("investigation_ledger") or cf.get("anomaly_context"):
                        agent.max_iterations = 0
                    max_iter_override = cf.get("max_iterations_override")
                    if max_iter_override is not None:
                        agent.max_iterations = int(max_iter_override)
                        logger.info(
                            f"Max iterations override for {ticket_id}:"
                            f" {max_iter_override}"
                        )
                    llm_override = cf.get("llm_override")
                    if llm_override and config:
                        override_llm = _make_llm_provider(
                            config,
                            provider=llm_override.get("provider", ""),
                            model=llm_override.get("model", ""),
                        )
                        override_effort = llm_override.get("reasoning_effort")
                        if override_effort:
                            override_llm.reasoning_effort = override_effort
                        override_max_tokens = llm_override.get("max_tokens")
                        if override_max_tokens:
                            override_llm.max_tokens = int(override_max_tokens)
                        agent.llm = override_llm
                        logger.info(
                            f"LLM override for {ticket_id}:"
                            f" provider={llm_override.get('provider', '')}"
                            f" model={llm_override.get('model', '')}"
                        )
                    # Jumpstarter provisioning: flash + boot +
                    # key injection should complete in ~15 tool
                    # calls with structured context. Default
                    # cap of 30 catches runaway loops.
                    elif (
                        status == "awaiting_provision"
                        and cf.get("resource_provider") == "jumpstarter"
                    ):
                        from orchestrator.config import _load_config_file

                        _jmp_cfg = _load_config_file().get("jumpstarter_images", {})
                        agent.max_iterations = _jmp_cfg.get(
                            "provisioning_max_iterations", 30
                        )

        except Exception:
            pass  # proceed with default iterations

        # Jumpstarter: resolve image URLs before platform
        # setup. This is a deterministic HTTP lookup — no
        # LLM needed. Runs for both preparing_platform
        # (new path) and awaiting_provision (legacy/direct).
        if status in ("preparing_platform", "awaiting_provision"):
            from orchestrator.config import _load_config_file

            await _resolve_jumpstarter_images(
                dispatcher.store_url,
                ticket_id,
                auth_headers=_auth_headers(),
                image_config=_load_config_file().get("jumpstarter_images", {}),
            )

        if agent_task_timeout > 0:
            try:
                await asyncio.wait_for(
                    agent.run(ticket_id),
                    timeout=agent_task_timeout,
                )
                success = True
            except asyncio.TimeoutError:
                logger.error(
                    f"Agent task timed out for {ticket_id} after {agent_task_timeout}s"
                )
                if dispatcher.events:
                    dispatcher.events.emit(
                        ticket_id,
                        "orchestrator",
                        "agent_error",
                        {
                            "reason": "agent_task_timeout",
                            "timeout_seconds": agent_task_timeout,
                        },
                    )
                await _transition_to_guidance(
                    dispatcher.store_url,
                    ticket_id,
                    f"Agent task timed out after {agent_task_timeout}s",
                    event_bus=dispatcher.events,
                )
        else:
            await agent.run(ticket_id)
            success = True

        if config:
            try:
                import httpx

                async with httpx.AsyncClient(
                    timeout=10.0, headers=_auth_headers()
                ) as client:
                    await client.patch(
                        f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}/fields",
                        json={
                            "fields": {
                                "llm_override": None,
                                "max_iterations_override": None,
                            },
                        },
                    )
            except Exception:
                pass
    except asyncio.CancelledError:
        logger.warning(f"Agent hard-stopped on ticket {ticket_id} (status={status})")
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=10.0, headers=_auth_headers()
            ) as client:
                # Check if ticket was already force-closed before
                # trying to transition — avoids reopening a closed
                # ticket.
                r = await client.get(
                    f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}",
                )
                if r.status_code == 200:
                    current = r.json().get("status", "")
                    if current == "closed":
                        logger.info(
                            f"Ticket {ticket_id} already closed,"
                            " skipping post-cancel transition"
                        )
                    else:
                        await client.patch(
                            f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}/fields",
                            json={"fields": {"interrupted": True}},
                        )
                        await client.post(
                            f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}/transition",
                            json={
                                "status": "awaiting_customer_guidance",
                                "comment": "Agent hard-stopped by user request",
                            },
                        )
        except Exception:
            logger.exception(f"Failed to transition hard-stopped ticket {ticket_id}")
        if dispatcher.events:
            dispatcher.events.emit(
                ticket_id,
                f"{status}-agent",
                "agent_stopped",
                {"mode": "hard"},
            )
    except HITLDriftError:
        logger.info(
            f"Agent cleanly unwound after ticket drift on "
            f"{ticket_id} (status={status}) — no transition needed"
        )
    except Exception as e:
        logger.exception(f"Agent failed on ticket {ticket_id} (status={status})")
        err_msg = str(e).lower()
        if (
            "resource_exhausted" in err_msg
            or "rate limit" in err_msg
            or "429" in err_msg
        ):
            reason = "Agent encountered sustained API rate limits (RESOURCE_EXHAUSTED). Pausing ticket for guidance."
        else:
            reason = f"Agent failed with an unhandled exception: {e}"
        try:
            await _transition_to_guidance(
                dispatcher.store_url,
                ticket_id,
                reason,
                event_bus=dispatcher.events,
            )
        except Exception:
            logger.exception(
                f"Failed to transition failed ticket {ticket_id} to guidance"
            )
    finally:
        logger.info(f"run_agent_task finally block for {ticket_id}")

        if success and status in PLAN_AGENT_STATUS.values():
            try:
                _advance_plan(
                    dispatcher.store_url,
                    ticket_id,
                    status,
                    event_bus=dispatcher.events,
                )
            except Exception:
                logger.exception(f"_advance_plan failed for {ticket_id}")
        dispatcher.clear_agent(ticket_id)
        dispatcher.mark_done(ticket_id)
        logger.info(f"mark_done completed for {ticket_id}")
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                pass


async def _transition_to_guidance(
    store_url: str,
    ticket_id: str,
    comment: str,
    event_bus: EventBus | None = None,
) -> None:
    """Transition a ticket to awaiting_customer_guidance.

    Used by orchestrator-level error handlers (stale watchdog,
    task timeout) that operate outside an agent context.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
            await client.post(
                f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                json={
                    "status": "awaiting_customer_guidance",
                    "comment": comment,
                },
            )
    except Exception:
        logger.exception(
            f"Failed to transition {ticket_id} to awaiting_customer_guidance"
        )
        return


async def _check_stale_tasks(
    dispatcher: Dispatcher,
    event_bus: EventBus,
    stale_timeout: float,
    store_url: str,
) -> None:
    """Cancel agent tasks with no events for too long.

    Detects agents stuck on unresponsive LLM calls, hung SSH
    connections, or infinite loops that don't emit events.

    Checks two staleness signals and uses the most recent:
    1. EventBus last-event timestamp (in-process agent events)
    2. Ticket updated_at from the state store (covers progress
       comments posted by MCP subprocess tools like run_with_progress)

    Always skips tickets in awaiting_customer_guidance — the
    agent is waiting for user input which can take arbitrarily
    long.

    Cleans up leaked active_tasks entries for closed tickets
    (e.g. after force_close while an agent was running).
    """
    from datetime import datetime

    import httpx

    now = time.time()
    async with httpx.AsyncClient(timeout=5.0, headers=_auth_headers()) as client:
        for tid, task in list(dispatcher.active_tasks().items()):
            last_event_time = event_bus.last_event_time(tid)
            if last_event_time is None:
                continue

            last_activity = last_event_time
            ticket_status = None
            try:
                r = await client.get(f"{store_url}/api/v1/tickets/{tid}")
                if r.status_code == 200:
                    ticket_data = r.json()
                    ticket_status = ticket_data.get("status", "")
                    if ticket_status == "closed":
                        logger.info(
                            f"Cleaning up active_tasks entry for closed ticket {tid}"
                        )
                        dispatcher.mark_done(tid)
                        task.cancel()
                        continue
                    updated_at = ticket_data.get("updated_at", "")
                    if updated_at:
                        ticket_time = datetime.fromisoformat(updated_at)
                        ticket_ts = ticket_time.timestamp()
                        if ticket_ts > last_activity:
                            last_activity = ticket_ts
            except Exception:
                pass

            idle_seconds = now - last_activity
            if idle_seconds > stale_timeout:
                if ticket_status == "awaiting_customer_guidance":
                    logger.debug(
                        f"Skipping stale check for {tid}: ticket is awaiting user input"
                    )
                    continue

                logger.warning(
                    f"Stale task detected for {tid}:"
                    f" no events for {idle_seconds:.0f}s"
                    f" (threshold: {stale_timeout:.0f}s)"
                    f" — cancelling task"
                )
                event_bus.emit(
                    tid,
                    "orchestrator",
                    "agent_error",
                    {
                        "reason": "stale_task_cancelled",
                        "idle_seconds": round(idle_seconds),
                        "threshold_seconds": round(stale_timeout),
                    },
                )
                await _transition_to_guidance(
                    store_url,
                    tid,
                    f"Agent task cancelled: no activity for"
                    f" {round(idle_seconds)}s (threshold:"
                    f" {round(stale_timeout)}s)",
                    event_bus=event_bus,
                )
                task.cancel()


async def _block_absent_suite(
    store_url: str,
    ticket_id: str,
    event_bus: EventBus | None = None,
) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        suite = ""
        try:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            suite = r.json().get("custom_fields", {}).get("benchmark_suite", "unknown")
        except Exception:
            pass
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/comments",
            json={
                "author": "orchestrator",
                "body": (
                    f"**Blocked:** No automation harness supports the "
                    f"'{suite}' benchmark. The ticket cannot proceed to "
                    f"hardware allocation.\n\n"
                    f"Please specify a supported benchmark or harness, "
                    f"or configure the harness that provides this benchmark."
                ),
            },
        )
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            json={
                "status": "awaiting_customer_guidance",
                "comment": "Absent benchmark suite — no harness can run this",
            },
        )


# Jumpstarter lifecycle functions extracted to
# providers/resource/jumpstarter_lifecycle.py
from providers.resource.jumpstarter_lifecycle import (
    release_lease_for_ticket as _release_jumpstarter_lease,
)
from providers.resource.jumpstarter_lifecycle import (
    resolve_images as _resolve_jumpstarter_images,
)
from providers.resource.jumpstarter_lifecycle import (
    sweep_orphaned_leases as _sweep_orphaned_leases,
)


async def _redirect_to_investigation(
    store_url: str,
    ticket_id: str,
    event_bus: EventBus | None = None,
) -> None:
    """Redirect a ticket from awaiting_hardware to gathering_context.

    Code-enforced invariant: tickets with anomaly_context belong
    on the investigation path. If triage routed to the ad-hoc
    path, the orchestrator corrects it here.
    """
    import httpx

    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/comments",
            json={
                "author": "orchestrator",
                "body": (
                    "**Investigation redirect:** Ticket has "
                    "anomaly_context but was routed to the "
                    "ad-hoc path. Redirecting to the "
                    "investigation path (gathering_context) "
                    "for proper convergence tracking."
                ),
            },
        )
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            json={
                "status": "gathering_context",
                "comment": (
                    "Code-enforced redirect: anomaly_context → investigation path"
                ),
            },
        )
        if event_bus:
            event_bus.emit(
                ticket_id,
                "orchestrator",
                "investigation_redirect",
                {
                    "from": "awaiting_hardware",
                    "to": "gathering_context",
                    "reason": "anomaly_context present",
                },
            )
    logger.info(f"[investigation-redirect] {ticket_id} redirected to gathering_context")


HANDOFF_RETRY_STATUS = {
    "awaiting_provision": "awaiting_hardware",
    "executing_benchmark": "awaiting_provision",
    "awaiting_review": "executing_benchmark",
    "evaluating_convergence": "executing_benchmark",
}


async def _block_handoff_failed(
    store_url: str,
    ticket_id: str,
    reason: str,
    current_status: str = "",
    event_bus: EventBus | None = None,
) -> None:
    import httpx

    retry_status = HANDOFF_RETRY_STATUS.get(current_status)

    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        if retry_status:
            rewind_comment = (
                f"Rewinding to {retry_status} so the agent"
                f" can retry after user guidance"
            )
            await client.post(
                f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                json={"status": retry_status, "comment": rewind_comment},
            )
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/comments",
            json={
                "author": "orchestrator",
                "body": (
                    f"**Handoff blocked:** {reason}\n\n"
                    f"The previous agent's results do not meet the "
                    f"preconditions for the next stage. The ticket is "
                    f"paused for guidance."
                ),
            },
        )
        block_comment = f"Handoff validation failed: {reason}"
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            json={
                "status": "awaiting_customer_guidance",
                "comment": block_comment,
            },
        )


async def _process_stop_requests(
    dispatcher: Dispatcher,
    store_url: str,
    dispatched_tickets: list[dict[str, Any]] | None = None,
) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
            if dispatched_tickets is not None:
                tickets = dispatched_tickets
            else:
                r = await client.get(f"{store_url}/api/v1/tickets")
                if r.status_code != 200:
                    return
                tickets = r.json()
            for ticket in tickets:
                stop_req = ticket.get("custom_fields", {}).get(
                    "stop_requested",
                )
                if not stop_req:
                    continue
                tid = ticket["id"]
                mode = stop_req.get("mode", "graceful")
                if mode == "hard":
                    # Hard stop: cancel the agent task (if any) AND
                    # force-close the ticket. Both steps are needed
                    # because force_close only changes ticket status
                    # — it doesn't notify the dispatcher to kill the
                    # running asyncio task.
                    if dispatcher.is_active(tid):
                        dispatcher.stop_agent(tid, "hard")
                        dispatcher.mark_done(tid)
                        logger.info(f"Cancelled agent task for {tid}")
                    resp = await client.post(
                        f"{store_url}/api/v1/tickets/{tid}/force-close",
                    )
                    if resp.status_code == 200:
                        logger.info(f"Force-closed ticket {tid}")
                    elif resp.status_code == 404:
                        logger.warning(f"Ticket {tid} not found during force-close")
                    else:
                        resp.raise_for_status()
                elif dispatcher.is_active(tid):
                    dispatcher.stop_agent(tid, "graceful")
                    logger.info(f"Graceful stop requested for {tid}")
                else:
                    resp = await client.post(
                        f"{store_url}/api/v1/tickets/{tid}/transition",
                        json={
                            "status": "awaiting_customer_guidance",
                            "comment": "Graceful stop requested — ticket paused",
                        },
                    )
                    if resp.status_code in (400, 409):
                        resp = await client.post(
                            f"{store_url}/api/v1/tickets/{tid}/force-close",
                        )
                        resp.raise_for_status()
                        logger.info(
                            f"Force-closed non-active ticket {tid}"
                            " (transition not allowed)"
                        )
                    else:
                        resp.raise_for_status()
                        logger.info(f"Paused non-active ticket {tid}")
                await client.patch(
                    f"{store_url}/api/v1/tickets/{tid}/fields",
                    json={"fields": {"stop_requested": None}},
                )
    except Exception:
        logger.exception("Failed to process stop requests")


def _maybe_start_introspection(
    dispatcher: Dispatcher,
    config: OrchestratorConfig,
    ticket: dict[str, Any],
    ticket_id: str,
) -> None:
    """Start introspection for a ticket if enabled and not already running.

    Introspection is enabled when either:
    1. Global config: introspection.enabled = true (or env var), OR
    2. Per-ticket: custom_fields.introspection_enabled = true

    Per-ticket custom_fields.introspection_enabled = false explicitly
    disables introspection even when globally enabled.
    """
    if dispatcher.is_introspection_active(ticket_id):
        return

    cf = ticket.get("custom_fields", {})
    per_ticket = cf.get("introspection_enabled")

    # Per-ticket override takes precedence.
    if per_ticket is False:
        return
    if per_ticket is not True and not config.introspection_enabled:
        return

    started = dispatcher.start_introspection(ticket_id)
    if started:
        logger.info(f"Introspection started for {ticket_id}")


async def poll_loop(config: OrchestratorConfig) -> None:
    llm = _make_llm_provider(config)
    llm.default_timeout = config.llm_timeout
    if config.llm_reasoning_effort:
        llm.reasoning_effort = config.llm_reasoning_effort
    llm.max_tokens = config.llm_max_tokens
    llm_factory = _make_llm_factory(config)

    repo_cache = RepoCache()
    for name, url in config.harness_repos.items():
        try:
            repo_cache.ensure_repo(name, url)
        except Exception:
            logger.warning(f"Failed to cache repo {name} from {url}", exc_info=True)

    harnesses = {"crucible": CrucibleSkillProvider(config.crucible_home)}
    if config.zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(config.zathras_home)
    else:
        private = PrivateSkillProvider()
        zathras_tests = private._load_config("zathras").get("tests")
        if zathras_tests:
            logger.info("No zathras_home set — using private-skills benchmark catalog")
            harnesses["zathras"] = ZathrasSkillProvider(fallback_tests=zathras_tests)
    harnesses["kube-burner"] = KubeBurnerSkillProvider()
    harnesses["k8s-netperf"] = K8sNetperfSkillProvider()
    harnesses["benchmark-runner"] = BenchmarkRunnerSkillProvider()
    harnesses["clusterbuster"] = ClusterbusterSkillProvider()
    harnesses["vstorm"] = VstormSkillProvider()
    harnesses["ioscale"] = IoscaleSkillProvider()
    harnesses["forge"] = ForgeSkillProvider()
    harnesses["arcaflow-plugins"] = ArcaflowPluginSkillProvider()
    skills = MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )
    local_secrets = LocalSecretsProvider()
    vault_config = config.raw.get("secrets")

    # Build shared secrets provider — local + vault (when configured).
    # Vault providers are constructed once here and reused in all
    # per-ticket cascades via vault_config passthrough.
    bw_shared = (
        (vault_config or {})
        .get("bitwarden", {})
        .get(
            "shared_project_id",
        )
    )
    if bw_shared and (vault_config or {}).get("bitwarden", {}).get(
        "organization_id",
    ):
        try:
            from providers.secrets.cascade import (
                CascadingSecretsProvider,
                _create_vault_layer,
            )

            vault_layer = _create_vault_layer(
                vault_config["bitwarden"],
                bw_shared,
            )
            if vault_layer is not None:
                secrets = CascadingSecretsProvider(
                    [
                        ("shared", local_secrets),
                        ("vault:shared", vault_layer),
                    ]
                )
                logger.info("Vault-aware shared secrets provider enabled")
            else:
                secrets = local_secrets
        except ImportError:
            logger.info(
                "bitwarden-sdk not installed; using local secrets only",
            )
            secrets = local_secrets
    else:
        secrets = local_secrets

    events = EventBus()

    # Initialize OpenTelemetry LLM instrumentation.
    # Spans from the Anthropic/OpenAI SDKs are captured
    # and fed into the EventBus for per-ticket token
    # accumulation.
    try:
        from providers.telemetry import setup_telemetry

        telemetry_config = config.raw.get("telemetry", {})
        setup_telemetry(
            event_bus=events,
            otlp_endpoint=telemetry_config.get("otlp_endpoint"),
            enabled=telemetry_config.get("enabled", True),
        )
    except ImportError:
        logger.info("OpenTelemetry not installed — LLM token tracking disabled")

    multi_user = config.raw.get("auth", {}).get("multi_user", False)
    user_store = None
    secrets_root = None
    if multi_user:
        from state_store.identity import UserStore

        user_store = UserStore()
        from paths import SECRETS_DIR

        secrets_root = SECRETS_DIR

    dispatcher = Dispatcher(
        config.state_store_url,
        llm,
        skills,
        secrets,
        events,
        repo_cache=repo_cache,
        llm_factory=llm_factory,
        instance_name=config.instance_name,
        user_store=user_store,
        secrets_root=secrets_root,
        vault_config=vault_config,
    )

    logger.info(
        f"Orchestrator started (store={config.state_store_url}, "
        f"poll={config.poll_interval}s, llm={config.llm_provider}, "
        f"max_agents={config.max_concurrent_agents})"
    )

    # System-wide budget check (per orchestrator session)
    system_budget = None
    if config.budget_session_cost_usd > 0:
        from providers.budget import SystemBudget

        system_budget = SystemBudget(
            session_cost_usd=config.budget_session_cost_usd,
        )
        logger.info(f"System session budget: ${config.budget_session_cost_usd:.2f}")

    status_names = list(STATUS_AGENT_MAP)
    status_offset = 0
    was_at_capacity = False

    while True:
        # Check system-wide budget before dispatching
        if system_budget is not None and events is not None:
            from providers.budget import (
                BudgetAction,
                check_system_budget,
            )
            from providers.cost import estimate_cumulative_cost

            global_usage = events.get_global_usage()
            global_cost = estimate_cumulative_cost(global_usage)
            sys_status = check_system_budget(
                system_budget,
                global_usage,
                global_cost,
            )
            if sys_status.action == BudgetAction.PAUSE:
                logger.warning(
                    f"System budget exceeded: {sys_status.reason}"
                    f" — skipping dispatch cycle"
                )
                await asyncio.sleep(config.poll_interval)
                continue

        try:
            all_fetched = await fetch_all_tickets(config.state_store_url)
        except Exception:
            logger.exception("Failed to fetch tickets")
            await asyncio.sleep(config.poll_interval)
            continue

        tickets_by_status: dict[str, list[dict[str, Any]]] = {}
        for t in all_fetched:
            tickets_by_status.setdefault(t.get("status", ""), []).append(t)

        at_capacity = False
        rotated = status_names[status_offset:] + status_names[:status_offset]
        status_offset = (status_offset + 1) % len(status_names)

        for status in rotated:
            if at_capacity:
                break

            tickets = tickets_by_status.get(status, [])
            for ticket in tickets:
                active_count = len(dispatcher.active_tasks())
                if active_count >= config.max_concurrent_agents:
                    if not was_at_capacity:
                        logger.info(
                            f"At capacity ({active_count}/"
                            f"{config.max_concurrent_agents})"
                            f" — deferring remaining tickets"
                        )
                    at_capacity = True
                    break

                tid = ticket["id"]
                if dispatcher.is_active(tid):
                    logger.info(f"Skipping {tid} at {status}: is_active")
                    continue

                cf = ticket.get("custom_fields", {})
                if status == "awaiting_review" and cf.get("review_submitted"):
                    logger.info(f"Skipping {tid}: review already submitted")
                    continue

                # Deterministic enrichment for webhook tickets.
                # Resolve directives from run metadata before
                # any agent sees the ticket. Best-effort —
                # agents handle gaps if enrichment fails.
                if status == "triage_pending":
                    cf = ticket.get("custom_fields", {})
                    if cf.get("trigger_source"):
                        try:
                            from providers.webhook_enrichment import (
                                enrich_webhook_ticket,
                            )

                            await enrich_webhook_ticket(
                                config.state_store_url,
                                tid,
                                ticket,
                            )
                        except Exception:
                            logger.warning(
                                f"Webhook enrichment failed for {tid}",
                                exc_info=True,
                            )

                if status == "awaiting_hardware" and ticket.get(
                    "custom_fields", {}
                ).get("absent_suite"):
                    logger.warning(
                        f"Ticket {tid} has absent_suite=True, pausing for human input"
                    )
                    await _block_absent_suite(
                        config.state_store_url, tid, event_bus=dispatcher.events
                    )
                    continue

                # Code-enforce investigation routing.
                # If triage routed to awaiting_hardware but
                # the ticket has anomaly_context, redirect
                # to gathering_context (investigation path).
                # LLM decides intent; code enforces invariants.
                # Skip if gathering_context already ran ---
                # prevents loop when planning_investigation
                # stub transitions back to awaiting_hardware.
                if status == "awaiting_hardware":
                    cf = ticket.get("custom_fields", {})
                    if cf.get("anomaly_context") and not cf.get("dedup_result"):
                        logger.info(
                            f"Redirecting {tid} to "
                            f"gathering_context "
                            f"(anomaly_context present)"
                        )
                        try:
                            await _redirect_to_investigation(
                                config.state_store_url,
                                tid,
                                event_bus=dispatcher.events,
                            )
                        except Exception:
                            logger.exception(f"Failed to redirect {tid}")
                        dispatcher.mark_done(tid)
                        continue

                # Jumpstarter: release any existing lease
                # before acquiring a new board. This
                # handles the case where a user sends a
                # ticket back to awaiting_hardware after
                # a provisioning failure.
                if status == "awaiting_hardware":
                    await _release_jumpstarter_lease(
                        ticket,
                    )

                ok, reason = check_handoff(status, ticket)
                if not ok:
                    if not dispatcher.is_handoff_blocked(tid, status):
                        logger.warning(
                            f"Handoff blocked for {tid} at {status}: {reason}"
                        )
                        dispatcher.mark_handoff_blocked(tid, status)
                        await _block_handoff_failed(
                            config.state_store_url,
                            tid,
                            reason,
                            status,
                            event_bus=dispatcher.events,
                        )
                    continue

                if not dispatcher.try_claim(tid, status):
                    logger.info(f"Skipping {tid} at {status}: claim held")
                    continue
                dispatcher.start_renewal(tid)

                # Start introspection BEFORE the pipeline agent
                # so no events are missed in a startup race.
                _maybe_start_introspection(
                    dispatcher,
                    config,
                    ticket,
                    tid,
                )

                logger.info(f"Dispatching {status} agent for ticket {tid}")
                task = asyncio.create_task(
                    run_agent_task(
                        dispatcher,
                        status,
                        tid,
                        config=config,
                        agent_task_timeout=config.agent_task_timeout,
                        ticket_data=ticket,
                    )
                )
                dispatcher.set_task(tid, task)

        await _process_stop_requests(
            dispatcher,
            config.state_store_url,
            dispatched_tickets=all_fetched,
        )

        # Jumpstarter: release orphaned leases whose
        # tickets are closed or no longer active.
        await _sweep_orphaned_leases(
            config.state_store_url,
            auth_headers=_auth_headers(),
        )

        # Stale-task watchdog: cancel tasks with no events
        # for longer than the configured threshold.
        if config.stale_task_timeout > 0 and events is not None:
            await _check_stale_tasks(
                dispatcher,
                events,
                config.stale_task_timeout,
                store_url=config.state_store_url,
            )

        if was_at_capacity and not at_capacity:
            logger.info("Below capacity — resuming normal dispatch")
        was_at_capacity = at_capacity

        await asyncio.sleep(config.poll_interval)


_lock_fd: int | None = None


def _acquire_lock() -> None:
    global _lock_fd
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        try:
            old_pid = LOCK_FILE.read_text().strip()
        except OSError:
            old_pid = "unknown"
        print(
            f"ERROR: Orchestrator already running (PID {old_pid}). "
            f"Kill it first or remove {LOCK_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd
    atexit.register(_release_lock)


def _release_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_api_token() -> None:
    """Read the state store API token and set it in the environment.

    All httpx clients and child processes (agent MCP servers)
    inherit the env var so they can authenticate automatically.
    """
    from state_store.auth import read_token_from_file

    token = read_token_from_file()
    if token:
        os.environ["AGENTIC_PERF_API_TOKEN"] = token


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def main():
    # Ignore SIGPIPE so broken stderr (e.g., parent shell exited)
    # doesn't kill the orchestrator. Python's logging handles the
    # resulting BrokenPipeError internally.
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    import faulthandler

    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _acquire_lock()
    _setup_api_token()
    config = OrchestratorConfig()
    try:
        asyncio.run(poll_loop(config))
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped")


if __name__ == "__main__":
    main()
