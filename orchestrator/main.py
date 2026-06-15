from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
from pathlib import Path

from .config import OrchestratorConfig
from .dispatcher import Dispatcher, STATUS_AGENT_MAP, TERMINAL_STATUSES
from .poller import fetch_tickets_by_status

from providers.events import EventBus
from providers.llm.mock import MockLLMProvider
from providers.llm.claude import ClaudeLLMProvider
from providers.secrets.local import LocalSecretsProvider
from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
from providers.skills.clusterbuster import ClusterbusterSkillProvider
from providers.skills.crucible import CrucibleSkillProvider
from providers.skills.k8s_netperf import K8sNetperfSkillProvider
from providers.skills.kube_burner import KubeBurnerSkillProvider
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider
from providers.skills.repo_cache import RepoCache
from providers.skills.zathras import ZathrasSkillProvider

logger = logging.getLogger(__name__)


def create_llm_provider(config: OrchestratorConfig):
    if config.llm_provider == "claude":
        return ClaudeLLMProvider(
            api_key=config.anthropic_api_key,
            model=config.llm_model,
            backend=config.llm_backend,
            project_id=config.llm_project_id,
            region=config.llm_region,
        )
    return MockLLMProvider()


async def run_agent_task(dispatcher: Dispatcher, status: str, ticket_id: str):
    try:
        agent = dispatcher.create_agent(status)
        if agent is None:
            return
        await agent.run(ticket_id)
    except Exception:
        logger.exception(f"Agent failed on ticket {ticket_id} (status={status})")
    finally:
        dispatcher.mark_done(ticket_id)
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


async def poll_loop(config: OrchestratorConfig) -> None:
    llm = create_llm_provider(config)

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
    skills = MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )
    secrets = LocalSecretsProvider()
    events = EventBus()
    dispatcher = Dispatcher(
        config.state_store_url, llm, skills, secrets, events,
        repo_cache=repo_cache,
    )

    logger.info(
        f"Orchestrator started (store={config.state_store_url}, "
        f"poll={config.poll_interval}s, llm={config.llm_provider})"
    )

    while True:
        for status in STATUS_AGENT_MAP:
            try:
                tickets = await fetch_tickets_by_status(
                    config.state_store_url, status
                )
            except Exception:
                logger.exception(f"Failed to fetch tickets for status={status}")
                continue

            for ticket in tickets:
                tid = ticket["id"]
                if dispatcher.is_active(tid):
                    continue
                if dispatcher.was_dispatched(tid, status):
                    continue

                if status == "awaiting_hardware" and ticket.get("custom_fields", {}).get("absent_suite"):
                    logger.warning(f"Ticket {tid} has absent_suite=True, pausing for human input")
                    dispatcher.mark_dispatched(tid, status)
                    await _block_absent_suite(config.state_store_url, tid)
                    continue

                dispatcher.mark_active(tid)
                dispatcher.mark_dispatched(tid, status)
                logger.info(f"Dispatching {status} agent for ticket {tid}")
                asyncio.create_task(
                    run_agent_task(dispatcher, status, tid)
                )

        await asyncio.sleep(config.poll_interval)


LOCK_FILE = Path.home() / ".agentic-perf" / "orchestrator.pid"


def _acquire_lock() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        old_pid = LOCK_FILE.read_text().strip()
        try:
            os.kill(int(old_pid), 0)
            print(
                f"ERROR: Orchestrator already running (PID {old_pid}). "
                f"Kill it first or remove {LOCK_FILE}",
                file=sys.stderr,
            )
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def main():
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
