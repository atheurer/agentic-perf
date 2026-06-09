from __future__ import annotations

import asyncio
import logging
import sys

from .config import OrchestratorConfig
from .dispatcher import Dispatcher, STATUS_AGENT_MAP, TERMINAL_STATUSES
from .poller import fetch_tickets_by_status

from providers.events import EventBus
from providers.llm.mock import MockLLMProvider
from providers.llm.claude import ClaudeLLMProvider
from providers.secrets.local import LocalSecretsProvider
from providers.skills.crucible import CrucibleSkillProvider
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider
from providers.skills.zathras import ZathrasSkillProvider

logger = logging.getLogger(__name__)


def create_llm_provider(config: OrchestratorConfig):
    if config.llm_provider == "claude":
        return ClaudeLLMProvider(
            api_key=config.anthropic_api_key,
            model=config.llm_model,
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


async def poll_loop(config: OrchestratorConfig) -> None:
    llm = create_llm_provider(config)
    harnesses = {"crucible": CrucibleSkillProvider(config.crucible_home)}
    if config.zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(config.zathras_home)
    skills = MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )
    secrets = LocalSecretsProvider()
    events = EventBus()
    dispatcher = Dispatcher(config.state_store_url, llm, skills, secrets, events)

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
                dispatcher.mark_active(tid)
                logger.info(f"Dispatching {status} agent for ticket {tid}")
                asyncio.create_task(
                    run_agent_task(dispatcher, status, tid)
                )

        await asyncio.sleep(config.poll_interval)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = OrchestratorConfig()
    try:
        asyncio.run(poll_loop(config))
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped")


if __name__ == "__main__":
    main()
