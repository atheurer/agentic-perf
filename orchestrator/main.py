from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path

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


def _advance_plan(store_url: str, ticket_id: str, completed_status: str) -> None:
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

                client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                    json={
                        "status": next_status,
                        "comment": (
                            f"Plan advancing to step {next_idx}: "
                            f"{next_step['agent_type']}"
                        ),
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
        await agent.run(ticket_id)
    except Exception:
        logger.exception(f"Agent failed on ticket {ticket_id} (status={status})")
    finally:
        logger.info(f"run_agent_task finally block for {ticket_id}")
        if status in PLAN_AGENT_STATUS.values():
            try:
                _advance_plan(dispatcher.store_url, ticket_id, status)
            except Exception:
                logger.exception(f"_advance_plan failed for {ticket_id}")
        dispatcher.mark_done(ticket_id)
        logger.info(f"mark_done completed for {ticket_id}")
        try:
            await agent.close()
        except Exception:
            pass


async def _block_absent_suite(store_url: str, ticket_id: str) -> None:
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


HANDOFF_RETRY_STATUS = {
    "awaiting_provision": "awaiting_hardware",
    "executing_benchmark": "awaiting_provision",
    "awaiting_review": "executing_benchmark",
    "evaluating_convergence": "executing_benchmark",
}


async def _block_handoff_failed(
    store_url: str, ticket_id: str, reason: str, current_status: str = ""
) -> None:
    import httpx

    retry_status = HANDOFF_RETRY_STATUS.get(current_status)

    async with httpx.AsyncClient(timeout=10.0) as client:
        if retry_status:
            await client.post(
                f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                json={
                    "status": retry_status,
                    "comment": (
                        f"Rewinding to {retry_status} so the agent"
                        f" can retry after user guidance"
                    ),
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
        await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            json={
                "status": "awaiting_customer_guidance",
                "comment": f"Handoff validation failed: {reason}",
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

    while True:
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
                    await _block_absent_suite(config.state_store_url, tid)
                    continue

                ok, reason = check_handoff(status, ticket)
                if not ok:
                    if not dispatcher.is_handoff_blocked(tid, status):
                        logger.warning(
                            f"Handoff blocked for {tid} at {status}: {reason}"
                        )
                        dispatcher.mark_handoff_blocked(tid, status)
                        await _block_handoff_failed(
                            config.state_store_url, tid, reason, status
                        )
                    continue

                dispatcher.mark_dispatched(tid, status)
                logger.info(f"Dispatching {status} agent for ticket {tid}")
                task = asyncio.create_task(run_agent_task(dispatcher, status, tid))
                dispatcher.set_task(tid, task)

        await asyncio.sleep(config.poll_interval)


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
