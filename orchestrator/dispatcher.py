from __future__ import annotations

import logging
from typing import Any

from agents.benchmark.agent import BenchmarkAgent
from agents.provisioning.agent import ProvisioningAgent
from agents.resource.agent import ResourceAgent
from agents.review.agent import ReviewAgent
from agents.triage.agent import TriageAgent
from providers.events import EventBus
from providers.llm.base import LLMProvider
from providers.secrets.base import SecretsProvider
from providers.skills.base import SkillProvider
from providers.skills.repo_cache import RepoCache

logger = logging.getLogger(__name__)

STATUS_AGENT_MAP = {
    "triage_pending": "triage",
    "awaiting_hardware": "resource_create",
    "awaiting_provision": "provisioning",
    "executing_benchmark": "benchmark",
    "awaiting_review": "review",
    "awaiting_teardown": "resource_teardown",
}

TERMINAL_STATUSES = {"closed", "awaiting_customer_guidance"}


class Dispatcher:
    def __init__(
        self,
        state_store_url: str,
        llm_provider: LLMProvider,
        skill_provider: SkillProvider,
        secrets_provider: SecretsProvider | None = None,
        event_bus: EventBus | None = None,
        repo_cache: RepoCache | None = None,
    ) -> None:
        self.store_url = state_store_url
        self.llm = llm_provider
        self.skills = skill_provider
        self.secrets = secrets_provider
        self.events = event_bus
        self.repo_cache = repo_cache
        self._active: set[str] = set()
        self._dispatched: set[tuple[str, str]] = set()

    def is_active(self, ticket_id: str) -> bool:
        return ticket_id in self._active

    def was_dispatched(self, ticket_id: str, status: str) -> bool:
        return (ticket_id, status) in self._dispatched

    def mark_active(self, ticket_id: str) -> None:
        self._active.add(ticket_id)

    def mark_dispatched(self, ticket_id: str, status: str) -> None:
        self._dispatched.add((ticket_id, status))

    def mark_done(self, ticket_id: str) -> None:
        self._active.discard(ticket_id)
        self._dispatched = {
            (t, s) for t, s in self._dispatched if t != ticket_id
        }

    def create_agent(self, status: str) -> Any:
        agent_type = STATUS_AGENT_MAP.get(status)
        if agent_type is None:
            return None

        if agent_type == "triage":
            return TriageAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                event_bus=self.events,
            )
        elif agent_type == "resource_create":
            return ResourceAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                mode="create",
                secrets_provider=self.secrets,
                event_bus=self.events,
            )
        elif agent_type == "provisioning":
            return ProvisioningAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                secrets_provider=self.secrets,
                event_bus=self.events,
            )
        elif agent_type == "benchmark":
            return BenchmarkAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                secrets_provider=self.secrets,
                event_bus=self.events,
                repo_cache=self.repo_cache,
            )
        elif agent_type == "review":
            return ReviewAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                event_bus=self.events,
                repo_cache=self.repo_cache,
            )
        elif agent_type == "resource_teardown":
            return ResourceAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                mode="teardown",
                secrets_provider=self.secrets,
                event_bus=self.events,
            )

        return None
