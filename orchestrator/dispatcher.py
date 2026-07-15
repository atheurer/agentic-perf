from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from agents.benchmark.agent import BenchmarkAgent
from agents.evaluate.agent import EvaluateAgent
from agents.gathering_context.agent import GatheringContextAgent
from agents.provisioning.agent import ProvisioningAgent
from agents.resource.agent import ResourceAgent
from agents.retrospective.agent import RetrospectiveAgent
from agents.review.agent import ReviewAgent
from agents.stub import StubAgent
from agents.synthesis.agent import SynthesisAgent
from agents.triage.agent import TriageAgent
from providers.events import EventBus
from providers.llm.base import LLMProvider
from providers.secrets.base import SecretsProvider
from providers.skills.base import SkillProvider
from providers.skills.repo_cache import RepoCache

logger = logging.getLogger(__name__)

STATUS_AGENT_MAP = {
    # Original linear pipeline
    "triage_pending": "triage",
    "awaiting_hardware": "resource_create",
    "awaiting_provision": "provisioning",
    "executing_benchmark": "benchmark",
    "awaiting_review": "review",
    "awaiting_teardown": "resource_teardown",
    "retrospective_pending": "retrospective",
    # Recursive investigation loop
    "gathering_context": "gathering_context",
    "planning_investigation": "planning_investigation",
    "evaluating_convergence": "evaluating_convergence",
    "synthesizing_results": "synthesizing_results",
}


class Dispatcher:
    DEFAULT_LEASE_SECONDS = 300

    def __init__(
        self,
        state_store_url: str,
        llm_provider: LLMProvider,
        skill_provider: SkillProvider,
        secrets_provider: SecretsProvider | None = None,
        event_bus: EventBus | None = None,
        repo_cache: RepoCache | None = None,
        llm_factory: Any | None = None,
        instance_name: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.store_url = state_store_url
        self.llm = llm_provider
        self.skills = skill_provider
        self.secrets = secrets_provider
        self.events = event_bus
        self.repo_cache = repo_cache
        self._llm_factory = llm_factory
        self._instance_name = instance_name or "unknown"
        self.lease_seconds = lease_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._agents: dict[str, Any] = {}
        self._renewal_tasks: dict[str, asyncio.Task] = {}
        self._handoff_blocked: set[tuple[str, str]] = set()
        self._introspection_tasks: dict[str, asyncio.Task] = {}
        self._introspection_agents: dict[str, Any] = {}

    def is_active(self, ticket_id: str) -> bool:
        task = self._tasks.get(ticket_id)
        if task is None:
            return False
        if task.done():
            self._tasks.pop(ticket_id, None)
            return False
        return True

    def _auth_headers(self) -> dict[str, str]:
        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def try_claim(self, ticket_id: str, status: str) -> bool:
        """Attempt to claim a ticket via the state store. Returns True on success."""
        try:
            with httpx.Client(timeout=10.0, headers=self._auth_headers()) as client:
                r = client.post(
                    f"{self.store_url}/api/v1/tickets/{ticket_id}/claim",
                    json={
                        "owner": self._instance_name,
                        "duration_seconds": self.lease_seconds,
                    },
                )
                return r.status_code == 200
        except Exception:
            logger.exception(f"Failed to claim ticket {ticket_id}")
            return False

    def release_claim(self, ticket_id: str) -> None:
        """Release our claim on a ticket."""
        try:
            with httpx.Client(timeout=10.0, headers=self._auth_headers()) as client:
                client.request(
                    "DELETE",
                    f"{self.store_url}/api/v1/tickets/{ticket_id}/claim",
                    json={"owner": self._instance_name},
                )
        except Exception:
            logger.exception(f"Failed to release claim on {ticket_id}")

    def renew_claim(self, ticket_id: str) -> bool:
        """Renew our claim on a ticket. Returns True on success."""
        try:
            with httpx.Client(timeout=10.0, headers=self._auth_headers()) as client:
                r = client.post(
                    f"{self.store_url}/api/v1/tickets/{ticket_id}/claim/renew",
                    json={
                        "owner": self._instance_name,
                        "duration_seconds": self.lease_seconds,
                    },
                )
                return r.status_code == 200
        except Exception:
            logger.exception(f"Failed to renew claim on {ticket_id}")
            return False

    async def _renewal_loop(self, ticket_id: str) -> None:
        """Background task that renews the claim at half the lease interval."""
        interval = self.lease_seconds / 2
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.renew_claim(ticket_id):
                    logger.warning(f"Claim renewal failed for {ticket_id}")
                    break
        except asyncio.CancelledError:
            pass

    def start_renewal(self, ticket_id: str) -> None:
        """Start the background claim renewal task for a ticket."""
        task = asyncio.ensure_future(self._renewal_loop(ticket_id))
        self._renewal_tasks[ticket_id] = task

    def stop_renewal(self, ticket_id: str) -> None:
        """Cancel the background claim renewal task for a ticket."""
        task = self._renewal_tasks.pop(ticket_id, None)
        if task is not None and not task.done():
            task.cancel()

    def set_task(self, ticket_id: str, task: asyncio.Task) -> None:
        self._tasks[ticket_id] = task

    def set_agent(self, ticket_id: str, agent: Any) -> None:
        self._agents[ticket_id] = agent

    def clear_agent(self, ticket_id: str) -> None:
        self._agents.pop(ticket_id, None)

    def is_handoff_blocked(self, ticket_id: str, status: str) -> bool:
        return (ticket_id, status) in self._handoff_blocked

    def mark_handoff_blocked(self, ticket_id: str, status: str) -> None:
        self._handoff_blocked.add((ticket_id, status))

    def clear_handoff_blocked(self, ticket_id: str) -> None:
        self._handoff_blocked = {
            (t, s) for t, s in self._handoff_blocked if t != ticket_id
        }

    def stop_agent(self, ticket_id: str, mode: str = "graceful") -> bool:
        if mode == "graceful":
            agent = self._agents.get(ticket_id)
            if agent is not None and hasattr(agent, "request_stop"):
                agent.request_stop()
                logger.info(f"Graceful stop requested for {ticket_id}")
                return True
        elif mode == "hard":
            task = self._tasks.get(ticket_id)
            if task is not None and not task.done():
                task.cancel()
                logger.info(f"Hard stop (task.cancel) for {ticket_id}")
                return True
        return False

    def active_tasks(self) -> dict[str, asyncio.Task]:
        """Return a snapshot of ticket_id → Task for non-done tasks."""
        # Clean up finished tasks while iterating.
        done = [tid for tid, task in self._tasks.items() if task.done()]
        for tid in done:
            self._tasks.pop(tid, None)
        return dict(self._tasks)

    def mark_done(self, ticket_id: str) -> None:
        self._tasks.pop(ticket_id, None)
        self._agents.pop(ticket_id, None)
        self.stop_renewal(ticket_id)
        self.release_claim(ticket_id)
        self.clear_handoff_blocked(ticket_id)

    def start_introspection(
        self,
        ticket_id: str,
    ) -> bool:
        """Start the introspection agent for a ticket.

        Returns True if started, False if already running.
        The introspection agent runs as a companion task
        alongside the real agents — it does not participate
        in the dispatch loop or affect ticket state.
        """
        existing = self._introspection_tasks.get(ticket_id)
        if existing is not None and not existing.done():
            return False

        from agents.introspection.agent import IntrospectionAgent

        agent = IntrospectionAgent(
            state_store_url=self.store_url,
            event_bus=self.events,
        )
        self._introspection_agents[ticket_id] = agent

        task = asyncio.ensure_future(
            self._run_introspection(
                ticket_id,
                agent,
            )
        )
        self._introspection_tasks[ticket_id] = task
        return True

    async def _run_introspection(
        self,
        ticket_id: str,
        agent: Any,
    ) -> None:
        """Run the introspection agent and clean up on exit."""
        try:
            await agent.run(ticket_id)
        except asyncio.CancelledError:
            logger.info(f"Introspection cancelled for {ticket_id}")
        except Exception:
            logger.exception(f"Introspection failed for {ticket_id}")
        finally:
            self._introspection_agents.pop(ticket_id, None)
            self._introspection_tasks.pop(ticket_id, None)
            try:
                await agent.close()
            except Exception:
                pass

    def stop_introspection(self, ticket_id: str) -> None:
        """Stop the introspection agent for a ticket."""
        agent = self._introspection_agents.get(ticket_id)
        if agent is not None:
            agent.request_stop()
        task = self._introspection_tasks.get(ticket_id)
        if task is not None and not task.done():
            task.cancel()

    def is_introspection_active(self, ticket_id: str) -> bool:
        """Check if introspection is running for a ticket."""
        task = self._introspection_tasks.get(ticket_id)
        if task is None:
            return False
        if task.done():
            self._introspection_tasks.pop(ticket_id, None)
            self._introspection_agents.pop(ticket_id, None)
            return False
        return True

    def _get_llm(self, agent_type: str) -> LLMProvider:
        if self._llm_factory:
            return self._llm_factory(agent_type)
        return self.llm

    def create_agent(self, status: str) -> Any:
        agent_type = STATUS_AGENT_MAP.get(status)
        if agent_type is None:
            return None

        llm = self._get_llm(agent_type)

        if agent_type == "triage":
            return TriageAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                event_bus=self.events,
            )
        elif agent_type == "resource_create":
            return ResourceAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                mode="create",
                secrets_provider=self.secrets,
                event_bus=self.events,
                instance_name=self._instance_name,
            )
        elif agent_type == "provisioning":
            return ProvisioningAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                secrets_provider=self.secrets,
                event_bus=self.events,
            )
        elif agent_type == "benchmark":
            return BenchmarkAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                secrets_provider=self.secrets,
                event_bus=self.events,
                repo_cache=self.repo_cache,
            )
        elif agent_type == "review":
            return ReviewAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                skill_provider=self.skills,
                event_bus=self.events,
                repo_cache=self.repo_cache,
            )
        elif agent_type == "resource_teardown":
            return ResourceAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                mode="teardown",
                secrets_provider=self.secrets,
                event_bus=self.events,
                instance_name=self._instance_name,
            )
        elif agent_type == "retrospective":
            return RetrospectiveAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                event_bus=self.events,
            )

        # Gathering context agent (dedup gate)
        if agent_type == "gathering_context":
            return GatheringContextAgent(
                llm_provider=self.llm,
                state_store_url=self.store_url,
                event_bus=self.events,
            )

        # Evaluating convergence agent
        if agent_type == "evaluating_convergence":
            return EvaluateAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                event_bus=self.events,
            )

        # Synthesizing results agent
        if agent_type == "synthesizing_results":
            return SynthesisAgent(
                llm_provider=llm,
                state_store_url=self.store_url,
                event_bus=self.events,
            )

        # Remaining investigation loop agents (stubs until
        # full implementations land in later issues)
        stub_targets = {
            "planning_investigation": "awaiting_hardware",
        }
        if agent_type in stub_targets:
            return StubAgent(
                agent_name=f"{agent_type}-agent",
                target_status=stub_targets[agent_type],
                state_store_url=self.store_url,
            )

        return None
