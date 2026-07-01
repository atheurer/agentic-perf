from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

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
        return _make_llm_provider(
            config,
            provider=agent_cfg.get("provider", ""),
            model=agent_cfg.get("model", ""),
        )

    return factory


PLAN_AGENT_STATUS = {
    "benchmark": "executing_benchmark",
    "review": "awaiting_review",
}


def _advance_plan(
    store_url: str,
    ticket_id: str,
    completed_status: str,
    event_bus: EventBus | None = None,
) -> None:
    """Advance the execution plan after an agent completes a step.

    Only advances if the completed agent matches the current step's
    agent_type — prevents non-plan agents (resource, provisioning)
    from prematurely completing plan steps.
    """
    import httpx

    client = httpx.Client(timeout=10.0)
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
        step["results"] = {
            "run_id": cf.get("run_id", ""),
            "benchmark_status": cf.get("benchmark_status", ""),
        }

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

                client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/comments",
                    json={
                        "author": "orchestrator",
                        "body": (
                            f"**Plan step {current} complete** — "
                            f"advancing to step {next_idx} "
                            f"({next_step['agent_type']})"
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


async def run_agent_task(dispatcher: Dispatcher, status: str, ticket_id: str):
    try:
        agent = dispatcher.create_agent(status)
        if agent is None:
            return

        # Investigation tickets get unlimited iterations for
        # all agents — convergence gates and budget guardrails
        # handle termination, not arbitrary iteration caps.
        # Without this, agents like the benchmark agent exhaust
        # their default max_iterations re-reading skills and
        # host state on each investigation loop-back.
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{dispatcher.store_url}/api/v1/tickets/{ticket_id}"
                )
                if r.status_code == 200:
                    cf = r.json().get("custom_fields", {})
                    if cf.get("investigation_ledger") or cf.get("anomaly_context"):
                        agent.max_iterations = 0
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

        await agent.run(ticket_id)
    except Exception:
        logger.exception(f"Agent failed on ticket {ticket_id} (status={status})")
    finally:
        logger.info(f"run_agent_task finally block for {ticket_id}")
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
        dispatcher.mark_done(ticket_id)
        logger.info(f"mark_done completed for {ticket_id}")
        try:
            await agent.close()
        except Exception:
            pass


async def _block_absent_suite(
    store_url: str,
    ticket_id: str,
    event_bus: EventBus | None = None,
) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
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

        # Extract image parameters from directives
        base_url = directives.get(
            "image_server",
            "https://autosd.sig.centos.org/",
        )
        image_version = directives.get("image_version", "AutoSD-10")
        release = directives.get("release", "nightly")
        image_name = directives.get("image_name", "ps")
        image_type = directives.get("image_type", "regular")

        # Board target from selector
        selector = directives.get("board_selector") or metadata.get("selector", "")
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

    async with httpx.AsyncClient(timeout=10.0) as client:
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


async def poll_loop(config: OrchestratorConfig) -> None:
    llm = _make_llm_provider(config)
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
                if dispatcher.was_dispatched(tid, status):
                    logger.info(f"Skipping {tid} at {status}: was_dispatched")
                    continue

                if status == "awaiting_hardware" and ticket.get(
                    "custom_fields", {}
                ).get("absent_suite"):
                    logger.warning(
                        f"Ticket {tid} has absent_suite=True, pausing for human input"
                    )
                    dispatcher.mark_dispatched(tid, status)
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
                    if cf.get("anomaly_context"):
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

                dispatcher.mark_dispatched(tid, status)
                logger.info(f"Dispatching {status} agent for ticket {tid}")
                task = asyncio.create_task(run_agent_task(dispatcher, status, tid))
                dispatcher.set_task(tid, task)

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

        # Already cleaned up?
        if cf.get("jumpstarter_lease_cleaned_up"):
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


LOCK_FILE = Path.home() / ".agentic-perf" / "orchestrator.pid"

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
    config = OrchestratorConfig()
    try:
        asyncio.run(poll_loop(config))
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped")


if __name__ == "__main__":
    main()
