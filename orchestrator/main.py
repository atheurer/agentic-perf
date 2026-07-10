from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

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
from .poller import fetch_tickets_by_status

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
        return provider

    return factory


PLAN_AGENT_STATUS = {
    "teardown": "awaiting_teardown",
    "resource": "awaiting_hardware",
    "provision": "awaiting_provision",
    "benchmark": "executing_benchmark",
    "review": "awaiting_review",
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
        }
    elif agent_type == "teardown":
        return {"teardown_complete": True}
    elif agent_type == "review":
        return {
            "verdict": cf.get("verdict", ""),
            "review_summary": cf.get("review_summary", ""),
        }
    return {}


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
            return

        step = steps[current]
        if step.get("status") != "in_progress":
            return

        expected_status = PLAN_AGENT_STATUS.get(step.get("agent_type", ""))
        if expected_status != completed_status:
            return

        ticket_status = ticket.get("status", "")
        if ticket_status == "awaiting_customer_guidance":
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
        plan["current_step"] = next_idx

        if next_idx < len(steps):
            next_step = steps[next_idx]
            next_status = PLAN_AGENT_STATUS.get(next_step["agent_type"])
            if next_status:
                next_step["status"] = "in_progress"

                client.patch(
                    f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                    json={"fields": {"execution_plan": plan}},
                )

                _apply_step_overrides(
                    store_url,
                    client,
                    ticket_id,
                    next_step,
                    cf,
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
                if event_bus:
                    event_bus.emit(
                        ticket_id,
                        "orchestrator",
                        "transition",
                        {
                            "to": next_status,
                            "comment": comment,
                            "ticket_id": ticket_id,
                        },
                    )
                return

        client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"execution_plan": plan}},
        )
    finally:
        client.close()


async def run_agent_task(
    dispatcher: Dispatcher,
    status: str,
    ticket_id: str,
    config: OrchestratorConfig | None = None,
    agent_task_timeout: float = 0,
):
    agent = None
    try:
        agent = dispatcher.create_agent(status)
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
                        agent.llm = override_llm
                        logger.info(
                            f"LLM override for {ticket_id}:"
                            f" provider={llm_override.get('provider', '')}"
                            f" model={llm_override.get('model', '')}"
                        )
                    # Jumpstarter provisioning needs many tool
                    # calls (flash, boot, IP discovery, SSH key
                    # injection). Budget guardrails are the real
                    # safety net, not iteration caps.
                    elif (
                        status == "awaiting_provision"
                        and cf.get("resource_provider") == "jumpstarter"
                    ):
                        agent.max_iterations = 0
        except Exception:
            pass  # proceed with default iterations

        # Jumpstarter: resolve image URLs before provisioning.
        # This is a deterministic HTTP lookup — no LLM needed.
        if status == "awaiting_provision":
            await _resolve_jumpstarter_images(dispatcher.store_url, ticket_id)

        if agent_task_timeout > 0:
            try:
                await asyncio.wait_for(
                    agent.run(ticket_id),
                    timeout=agent_task_timeout,
                )
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
    except Exception:
        logger.exception(f"Agent failed on ticket {ticket_id} (status={status})")
    finally:
        logger.info(f"run_agent_task finally block for {ticket_id}")

        # Release Jumpstarter lease after benchmark completes.
        # The lease is no longer needed — serial capture and
        # SSH are done. Releasing here (not in evaluate or
        # orchestrator poll) ensures exactly one lease is
        # active at a time during fleet investigations.
        if status == "executing_benchmark":
            try:
                await _cleanup_jumpstarter_lease(
                    dispatcher.store_url,
                    ticket_id,
                    event_bus=dispatcher.events,
                )
            except Exception:
                logger.debug(
                    f"Post-benchmark lease cleanup failed for {ticket_id}",
                    exc_info=True,
                )

        if status in PLAN_AGENT_STATUS.values():
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
    if event_bus:
        event_bus.emit(
            ticket_id,
            "orchestrator",
            "transition",
            {
                "to": "awaiting_customer_guidance",
                "comment": comment,
                "ticket_id": ticket_id,
            },
        )


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
    """
    import httpx
    from datetime import datetime, timezone

    now = time.time()
    for tid, task in dispatcher.active_tasks().items():
        last_event_time = event_bus.last_event_time(tid)
        if last_event_time is None:
            continue

        # Check the ticket's updated_at as a secondary signal —
        # tool_progress comments update this even though they
        # don't go through the in-process EventBus.
        last_activity = last_event_time
        try:
            async with httpx.AsyncClient(
                timeout=5.0, headers=_auth_headers()
            ) as client:
                r = await client.get(f"{store_url}/api/v1/tickets/{tid}")
                if r.status_code == 200:
                    updated_at = r.json().get("updated_at", "")
                    if updated_at:
                        ticket_time = datetime.fromisoformat(updated_at)
                        ticket_ts = ticket_time.timestamp()
                        if ticket_ts > last_activity:
                            last_activity = ticket_ts
        except Exception:
            pass

        idle_seconds = now - last_activity
        if idle_seconds > stale_timeout:
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
        if event_bus:
            event_bus.emit(
                ticket_id,
                "orchestrator",
                "transition",
                {
                    "to": "awaiting_customer_guidance",
                    "comment": "Absent benchmark suite — no harness can run this",
                    "ticket_id": ticket_id,
                },
            )


async def _resolve_jumpstarter_images(
    store_url: str,
    ticket_id: str,
) -> None:
    """Resolve Jumpstarter image URLs before provisioning.

    Fetches the build server manifest and resolves the flash
    command for the board. Stores the result in
    custom_fields.jumpstarter_flash so the provisioning agent
    can flash without needing to resolve URLs itself.

    This is deterministic code — no LLM reasoning needed.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            if r.status_code != 200:
                return
            cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return

        # Already resolved?
        if cf.get("jumpstarter_flash"):
            return

        directives = cf.get("directives", {})
        metadata = cf.get("resource_provider_metadata", {})

        # Resolve image source. No hardcoded OS defaults
        # — if the user didn't specify and there's no
        # config, the provisioning agent must ask.
        from orchestrator.config import _load_config_file

        img_cfg = _load_config_file().get("jumpstarter_images", {})

        base_url = directives.get(
            "image_server",
            img_cfg.get(
                "server",
                "https://autosd.sig.centos.org/",
            ),
        )
        image_version = directives.get(
            "image_version",
            img_cfg.get("image_version", ""),
        )
        release = directives.get("release", "nightly")
        image_name = directives.get("image_name", "ps")
        image_type = directives.get("image_type", "regular")

        if not image_version:
            logger.info(
                f"[jumpstarter-images] No image_version "
                f"for {ticket_id} — provisioning agent "
                f"will need to ask the user"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.patch(
                    f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                    json={
                        "fields": {
                            "jumpstarter_flash": {
                                "error": (
                                    "No OS image version"
                                    " specified. Set "
                                    "image_version in "
                                    "ticket directives "
                                    "(e.g., AutoSD-10, "
                                    "RHIVOS-2) or "
                                    "configure "
                                    "jumpstarter_images."
                                    "image_version in "
                                    "config.json."
                                ),
                            },
                        },
                    },
                )
            return

        # Board target from selector. The resource agent
        # may store it as 'selector' or 'jumpstarter_selector'.
        selector = (
            directives.get("board_selector")
            or metadata.get("selector", "")
            or metadata.get("jumpstarter_selector", "")
        )
        board_target = selector.split("=", 1)[-1] if "=" in selector else selector

        from providers.resource.jumpstarter_images import (
            resolve_image_urls,
        )

        result = await resolve_image_urls(
            base_url=base_url,
            image_version=image_version,
            release=release,
            board_target=board_target,
            image_name=image_name,
            image_type=image_type,
        )

        # If exact match failed, try fallbacks
        if result.get("error") and result.get("available_variants"):
            variants = result["available_variants"]
            # Try same image_name with other type
            for v in variants:
                if v["image_name"] == image_name:
                    result = await resolve_image_urls(
                        base_url=base_url,
                        image_version=image_version,
                        release=release,
                        board_target=board_target,
                        image_name=v["image_name"],
                        image_type=v["image_type"],
                    )
                    if not result.get("error"):
                        break

        # Flash duration estimate by board type.
        _FLASH_DURATION_MINS: dict[str, int] = {
            "ride4_sa8775p_sx_r3": 8,
            "ride4_sa8775p_sx": 8,
            "ride4_sa8775p_sx_legacy": 8,
            "ride4_sa8650p_sx_r3": 8,
            "rcar_s4": 20,
            "s32g_vnp_rdb3": 20,
            "j784s4evm": 20,
        }
        result["expected_duration_mins"] = _FLASH_DURATION_MINS.get(board_target, 15)

        # Include the orchestrator's SSH public key so
        # the provisioning agent can inject it into the
        # board without needing a local file-read tool.
        try:
            from pathlib import Path

            pub_key_path = Path.home() / ".ssh" / "id_rsa.pub"
            if pub_key_path.exists():
                result["ssh_public_key"] = pub_key_path.read_text().strip()
                result["ssh_key_path"] = str(pub_key_path.with_suffix(""))
        except Exception:
            pass

        # Store on ticket (after adding SSH key)
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                json={
                    "fields": {
                        "jumpstarter_flash": result,
                    },
                },
            )

        if result.get("error"):
            logger.warning(
                f"[jumpstarter-images] Resolution failed "
                f"for {ticket_id}: {result['error']}"
            )
        else:
            logger.info(
                f"[jumpstarter-images] Resolved "
                f"{len(result.get('flash_targets', []))} "
                f"partition(s) for {ticket_id}"
            )

    except Exception:
        logger.debug(
            "[jumpstarter-images] Resolution skipped",
            exc_info=True,
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

    async with httpx.AsyncClient(timeout=10.0) as client:
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
            if event_bus:
                event_bus.emit(
                    ticket_id,
                    "orchestrator",
                    "transition",
                    {
                        "to": retry_status,
                        "comment": rewind_comment,
                        "ticket_id": ticket_id,
                    },
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
        if event_bus:
            event_bus.emit(
                ticket_id,
                "orchestrator",
                "transition",
                {
                    "to": "awaiting_customer_guidance",
                    "comment": block_comment,
                    "ticket_id": ticket_id,
                },
            )


async def _process_stop_requests(
    dispatcher: Dispatcher,
    store_url: str,
) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
            r = await client.get(f"{store_url}/api/v1/tickets")
            if r.status_code != 200:
                return
            for ticket in r.json():
                stop_req = ticket.get("custom_fields", {}).get(
                    "stop_requested",
                )
                if not stop_req:
                    continue
                tid = ticket["id"]
                mode = stop_req.get("mode", "graceful")
                if dispatcher.is_active(tid):
                    dispatcher.stop_agent(tid, mode)
                    logger.info(f"Processed stop request for {tid} (mode={mode})")
                await client.patch(
                    f"{store_url}/api/v1/tickets/{tid}/fields",
                    json={"fields": {"stop_requested": None}},
                )
    except Exception:
        logger.exception("Failed to process stop requests")


async def poll_loop(config: OrchestratorConfig) -> None:
    llm = _make_llm_provider(config)
    llm.default_timeout = config.llm_timeout
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
    secrets = LocalSecretsProvider()
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

    dispatcher = Dispatcher(
        config.state_store_url,
        llm,
        skills,
        secrets,
        events,
        repo_cache=repo_cache,
        llm_factory=llm_factory,
        instance_name=config.instance_name,
    )

    logger.info(
        f"Orchestrator started (store={config.state_store_url}, "
        f"poll={config.poll_interval}s, llm={config.llm_provider})"
    )

    # System-wide budget check (per orchestrator session)
    system_budget = None
    if config.budget_session_cost_usd > 0:
        from providers.budget import SystemBudget

        system_budget = SystemBudget(
            session_cost_usd=config.budget_session_cost_usd,
        )
        logger.info(f"System session budget: ${config.budget_session_cost_usd:.2f}")

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

        for status in STATUS_AGENT_MAP:
            try:
                tickets = await fetch_tickets_by_status(config.state_store_url, status)
            except Exception:
                logger.exception(f"Failed to fetch tickets for status={status}")
                continue

            for ticket in tickets:
                tid = ticket["id"]
                if dispatcher.is_active(tid):
                    logger.info(f"Skipping {tid} at {status}: is_active")
                    continue

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
                if status == "awaiting_hardware":
                    cf = ticket.get("custom_fields", {})
                    fleet = cf.get("fleet_investigation", {})
                    if cf.get("anomaly_context") and not fleet.get("enabled"):
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
                        dispatcher.mark_dispatched(tid, status)
                        continue

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
                logger.info(f"Dispatching {status} agent for ticket {tid}")
                task = asyncio.create_task(
                    run_agent_task(
                        dispatcher,
                        status,
                        tid,
                        config=config,
                        agent_task_timeout=config.agent_task_timeout,
                    )
                )
                dispatcher.set_task(tid, task)

        await _process_stop_requests(dispatcher, config.state_store_url)

        # Stale-task watchdog: cancel tasks with no events
        # for longer than the configured threshold.
        if config.stale_task_timeout > 0 and events is not None:
            await _check_stale_tasks(
                dispatcher,
                events,
                config.stale_task_timeout,
                store_url=config.state_store_url,
            )

        # Check async_wait tickets for completion or timeout
        await _check_async_wait_tickets(
            config.state_store_url,
            event_bus=events,
        )

        await asyncio.sleep(config.poll_interval)


async def _check_async_wait_tickets(
    store_url: str,
    event_bus: EventBus | None = None,
) -> None:
    """Monitor async_wait tickets for timeout.

    Checks each async_wait ticket's elapsed time against its
    timeout limit (2x expected_duration_mins).  If exceeded,
    transitions to awaiting_customer_guidance.

    Completion detection is handled by the CloudEvents signal
    endpoint (POST /api/v1/tickets/{id}/signal).  External
    systems (benchmark harnesses, Jumpstarter, Horreum, CI
    pipelines) send CloudEvents to resume tickets.  The
    orchestrator does not poll infrastructure directly —
    that would violate the trust boundary between the
    control plane and agent execution.
    """
    import httpx

    try:
        tickets = await fetch_tickets_by_status(
            store_url,
            "async_wait",
        )
    except Exception:
        return

    if not tickets:
        return

    from datetime import datetime, timezone

    async with httpx.AsyncClient(timeout=30.0) as client:
        for ticket in tickets:
            tid = ticket["id"]
            cf = ticket.get("custom_fields", {})
            ctx = cf.get("async_context", {})
            if not ctx:
                continue

            started_at = ctx.get("started_at", "")
            expected_mins = ctx.get(
                "expected_duration_mins",
                60,
            )
            timeout_mins = expected_mins * 2
            if not started_at:
                continue

            try:
                start = datetime.fromisoformat(started_at)
                elapsed = datetime.now(timezone.utc) - start
                elapsed_mins = elapsed.total_seconds() / 60
            except (ValueError, TypeError):
                continue

            if elapsed_mins <= timeout_mins:
                continue

            logger.warning(
                f"[async_wait] Ticket {tid} timed "
                f"out after {elapsed_mins:.0f}m "
                f"(limit {timeout_mins:.0f}m)"
            )
            ctx["timed_out"] = True
            ctx["elapsed_mins"] = round(
                elapsed_mins,
                1,
            )
            await client.patch(
                f"{store_url}/api/v1/tickets/{tid}/fields",
                json={
                    "fields": {
                        "async_context": ctx,
                    },
                },
            )
            await client.post(
                f"{store_url}/api/v1/tickets/{tid}/comments",
                json={
                    "author": "orchestrator",
                    "body": (
                        f"**Async timeout:** Operation "
                        f"exceeded {timeout_mins:.0f}m "
                        f"limit ({elapsed_mins:.0f}m "
                        f"elapsed). Pausing for "
                        f"guidance."
                    ),
                },
            )
            await client.post(
                f"{store_url}/api/v1/tickets/{tid}/transition",
                json={
                    "status": ("awaiting_customer_guidance"),
                    "comment": (f"Async operation timed out after {elapsed_mins:.0f}m"),
                },
            )
            if event_bus:
                event_bus.emit(
                    tid,
                    "orchestrator",
                    "async_timeout",
                    {
                        "wait_type": ctx.get(
                            "wait_type",
                        ),
                        "elapsed_mins": round(
                            elapsed_mins,
                            1,
                        ),
                    },
                )

            # Clean up Jumpstarter lease if ticket
            # is being parked due to timeout.
            await _cleanup_jumpstarter_lease(store_url, tid, client, event_bus)


async def _cleanup_jumpstarter_lease(
    store_url: str,
    ticket_id: str,
    client: Any = None,
    event_bus: EventBus | None = None,
) -> None:
    """Terminate a Jumpstarter lease for a parked ticket.

    Called when a ticket with an active Jumpstarter lease is
    entering a state where no agent will run (e.g.,
    awaiting_customer_guidance after timeout). Without this,
    the lease would sit open until it expires on the
    controller.

    This is a safety net — normal lease cleanup happens
    through the resource teardown agent. This catches the
    cases where teardown is bypassed.
    """
    import httpx

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)

    try:
        r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
        if r.status_code != 200:
            return
        cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return

        lease_id = cf.get("resource_reservation_id") or cf.get(
            "resource_provider_metadata", {}
        ).get("lease_id")
        if not lease_id:
            return

        # Already cleaned up? Skip for fleet — each
        # iteration has a different lease ID.
        fleet = cf.get("fleet_investigation", {})
        if cf.get("jumpstarter_lease_cleaned_up") and not fleet.get("enabled"):
            return

        logger.warning(
            f"[jumpstarter-cleanup] Releasing lease "
            f"{lease_id} for parked ticket {ticket_id}"
        )

        try:
            from providers.resource.jumpstarter import (
                JumpstarterResourceProvider,
            )

            # Attempt to load from secrets
            try:
                from providers.secrets.file import (
                    FileSecretsProvider,
                )

                secrets = FileSecretsProvider()
                provider = await JumpstarterResourceProvider.from_secrets(secrets)
            except Exception:
                # Fall back to default construction
                from pathlib import Path

                cli_config = (
                    Path.home() / ".config" / "jumpstarter" / "clients" / "perf-ci.yaml"
                )
                provider = JumpstarterResourceProvider(
                    client_name="perf-ci",
                    config_path=(cli_config if cli_config.exists() else None),
                )

            result = await provider.terminate(lease_id)
            await provider.close()

            # Mark as cleaned up on the ticket
            await client.patch(
                f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                json={
                    "fields": {
                        "jumpstarter_lease_cleaned_up": True,
                    },
                },
            )

            await client.post(
                f"{store_url}/api/v1/tickets/{ticket_id}/comments",
                json={
                    "author": "orchestrator",
                    "body": (
                        f"**Jumpstarter lease cleanup:** "
                        f"Lease {lease_id} terminated "
                        f"(ticket parked without "
                        f"reaching teardown). "
                        f"Status: {result.get('status')}"
                    ),
                },
            )

            if event_bus:
                event_bus.emit(
                    ticket_id,
                    "orchestrator",
                    "jumpstarter_lease_cleanup",
                    {
                        "lease_id": lease_id,
                        "result": result.get("status"),
                    },
                )

            logger.info(
                f"[jumpstarter-cleanup] Lease {lease_id} terminated for {ticket_id}"
            )

        except ImportError:
            logger.warning(
                "[jumpstarter-cleanup] jumpstarter package "
                "not available — lease not cleaned up"
            )
        except Exception:
            logger.exception(
                f"[jumpstarter-cleanup] Failed to terminate "
                f"lease {lease_id} for {ticket_id}"
            )

    finally:
        if owns_client:
            await client.aclose()


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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _acquire_lock()
    _setup_api_token()
    config = OrchestratorConfig()
    try:
        asyncio.run(poll_loop(config))
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped")


if __name__ == "__main__":
    main()
