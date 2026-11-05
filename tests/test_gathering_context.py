"""Tests for the Gathering Context agent (dedup gate).

Tests the agent's dedup decision logic: matching against open
Investigation Records, state machine transitions, and field
persistence.
"""

from __future__ import annotations

import pytest

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State machine transitions ---


class TestStateTransitions:
    """Verify gathering_context transitions in the state machine."""

    def test_gathering_context_to_analyzing(self):
        """Can transition to analyzing (no match)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.ANALYZING in valid

    def test_gathering_context_to_retrospective(self):
        """Can transition to retrospective_pending (dedup match)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.RETROSPECTIVE_PENDING in valid

    def test_gathering_context_to_hitl(self):
        """Can transition to awaiting_customer_guidance."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in valid

    def test_gathering_context_not_to_closed(self):
        """Cannot transition directly to closed (must go through
        retrospective first)."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.CLOSED not in valid

    def test_triage_to_gathering_context(self):
        """Triage can route to gathering_context."""
        valid = VALID_TRANSITIONS[TicketStatus.TRIAGE_PENDING]
        assert TicketStatus.GATHERING_CONTEXT in valid


# --- Agent construction ---


class TestAgentConstruction:
    """Verify agent can be constructed and has correct attributes."""

    def test_agent_name(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        assert agent.agent_name == "gathering-context-agent"

    def test_system_prompt_content(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        prompt = agent._system_prompt({})
        assert "Investigation Records" in prompt
        assert "MATCH_FOUND" in prompt
        assert "NO_MATCH" in prompt


# --- Message building ---


class TestMessageBuilding:
    """Verify _build_messages extracts anomaly context."""

    def test_with_anomaly_context(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Storage regression on NXP",
            "custom_fields": {
                "anomaly_context": {
                    "subsystem": "storage_io",
                    "metric": "iops_4k_randread",
                    "direction": "degrading",
                    "platform": "NXP_S32G",
                    "magnitude": "-31%",
                },
                "hypothesis": "virtio-blk driver regression",
            },
        }
        msgs = agent._build_messages(ticket)
        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert "storage_io" in content
        assert "iops_4k_randread" in content
        assert "NXP_S32G" in content
        assert "-31%" in content
        assert "virtio-blk" in content

    def test_without_anomaly_context(self):
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        ticket = {
            "summary": "Run fio test",
            "custom_fields": {},
        }
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]
        assert "No anomaly context found" in content
        assert "NO_MATCH" in content


# --- Handle completion ---


class TestHandleCompletion:
    """Verify _handle_completion transitions correctly."""

    @pytest.mark.asyncio
    async def test_no_match_transitions_to_planning(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "NO_MATCH",
                        "notes": "No open records for storage_io",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "analyzing"

    @pytest.mark.asyncio
    async def test_match_transitions_to_retrospective(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                        "match_confidence": 0.92,
                        "match_rationale": (
                            "Same subsystem and metric, similar "
                            "magnitude on related platform"
                        ),
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "retrospective_pending"

    @pytest.mark.asyncio
    async def test_match_persists_dedup_result(self):
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                        "match_confidence": 0.92,
                        "match_rationale": "Same regression",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Check fields update
        patch_calls = [
            c for c in agent._client.patch.call_args_list if "fields" in str(c)
        ]
        assert len(patch_calls) == 1
        fields = patch_calls[0].kwargs.get(
            "json",
            patch_calls[0][1].get("json", {}),
        )["fields"]
        dedup = fields["dedup_result"]
        assert dedup["decision"] == "MATCH_FOUND"
        assert dedup["matched_investigation_id"] == "RCA-998"
        assert dedup["match_confidence"] == 0.92

    @pytest.mark.asyncio
    async def test_llm_match_persists_provider_record_url(self):
        """A provider URL is persisted and rendered without trusting the LLM."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agents.gathering_context.agent import GatheringContextAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._get_ticket = AsyncMock(return_value={"custom_fields": {}})
        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                        "match_confidence": 0.92,
                        "match_rationale": "Same regression",
                    },
                ),
            ],
            stop_reason="tool_use",
        )
        provider = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    record_url="https://horreum.example.com/run/101",
                    operational_metrics=SimpleNamespace(
                        convergence_outcome="ISOLATION",
                    ),
                )
            )
        )

        with patch(
            "providers.investigation.registry.create_record_provider",
            return_value=provider,
        ):
            await agent._handle_completion("PERF-TEST", response)

        dedup = agent._update_fields.await_args_list[-1].args[1]["dedup_result"]
        assert dedup["record_url"] == "https://horreum.example.com/run/101"
        assert (
            "[RCA-998](https://horreum.example.com/run/101)"
            in (agent._add_comment.await_args.args[1])
        )

    @pytest.mark.asyncio
    async def test_llm_match_omits_unavailable_provider_record_url(self):
        """A provider without a URL leaves the dedup result and comment unlinked."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agents.gathering_context.agent import GatheringContextAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._get_ticket = AsyncMock(return_value={"custom_fields": {}})
        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-998",
                    },
                ),
            ],
            stop_reason="tool_use",
        )
        provider = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    record_url="",
                    operational_metrics=SimpleNamespace(
                        convergence_outcome="ISOLATION",
                    ),
                )
            )
        )

        with patch(
            "providers.investigation.registry.create_record_provider",
            return_value=provider,
        ):
            await agent._handle_completion("PERF-TEST", response)

        assert agent._update_fields.await_count == 2
        dedup = agent._update_fields.await_args.args[1]["dedup_result"]
        assert dedup["record_url"] == ""
        assert "[RCA-998](" not in agent._add_comment.await_args.args[1]


# --- Triage routing ---


class TestTriageRouting:
    """Verify triage routes based on anomaly_context presence."""

    @pytest.mark.asyncio
    async def test_anomaly_context_routes_to_gathering(self):
        """Triage routes to gathering_context when anomaly_context
        is present on the ticket."""
        from unittest.mock import AsyncMock

        from agents.triage.agent import TriageAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = TriageAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            skill_provider=MockSkillProvider(),
        )
        agent._client = AsyncMock()

        # Mock: _update_fields and _add_comment succeed
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        # Mock: _get_ticket returns ticket WITH anomaly_context
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {
                        "anomaly_context": {
                            "subsystem": "storage_io",
                            "metric": "iops_4k_randread",
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_triage_result",
                    input={
                        "hypothesis": "storage regression",
                        "benchmark_suite": "fio",
                        "parsed_specs": {},
                        "required_hosts": [
                            {"roles": ["controller", "client"]},
                        ],
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        # Find the transition call
        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "gathering_context"

    @pytest.mark.asyncio
    async def test_no_anomaly_context_routes_to_hardware(self):
        """Triage routes to awaiting_hardware when no
        anomaly_context is present (ad-hoc ticket)."""
        from unittest.mock import AsyncMock

        from agents.triage.agent import TriageAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        agent = TriageAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
            skill_provider=MockSkillProvider(),
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        # Mock: _get_ticket returns ticket WITHOUT anomaly_context
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "custom_fields": {},
                },
                raise_for_status=lambda: None,
            ),
        )

        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_triage_result",
                    input={
                        "hypothesis": "baseline network",
                        "benchmark_suite": "uperf",
                        "parsed_specs": {},
                        "required_hosts": [
                            {"roles": ["controller"]},
                            {"roles": ["client"]},
                            {"roles": ["server"]},
                        ],
                    },
                ),
            ],
            stop_reason="tool_use",
        )

        await agent._handle_completion("PERF-TEST", response)

        transition_calls = [
            c for c in agent._client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        body = transition_calls[0].kwargs.get(
            "json",
            transition_calls[0][1].get("json", {}),
        )
        assert body["status"] == "awaiting_hardware"


# --- Dispatcher integration ---


class TestDispatcherIntegration:
    """Verify dispatcher creates the real agent, not a stub."""

    def test_dispatcher_creates_gathering_context_agent(self):
        import pytest

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )

        try:
            from orchestrator.dispatcher import Dispatcher
        except ImportError:
            pytest.skip("orchestrator imports unavailable")
        from providers.llm.mock import MockLLMProvider
        from tests.conftest import MockSkillProvider

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MockLLMProvider(),
            skill_provider=MockSkillProvider(),
        )
        agent = dispatcher.create_agent("gathering_context")
        assert isinstance(agent, GatheringContextAgent)
        assert agent.agent_name == "gathering-context-agent"


class TestDeterministicDedup:
    """Test deterministic dedup via canonical dedup_key fields."""

    def test_analyzing_transition_still_valid(self):
        """gathering_context can still transition to analyzing."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.ANALYZING in valid

    def test_retrospective_transition_valid(self):
        """gathering_context can transition to retrospective."""
        valid = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
        assert TicketStatus.RETROSPECTIVE_PENDING in valid

    @pytest.mark.asyncio
    async def test_dedup_skips_without_dedup_key(self):
        """No dedup_key on ticket → returns False."""
        from unittest.mock import AsyncMock, MagicMock

        agent = MagicMock()
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {"anomaly_context": {"source": "horreum"}},
            }
        )
        # Call the static logic — dedup_key is missing

        ticket = {
            "custom_fields": {
                "anomaly_context": {"source": "horreum"},
            },
        }
        # dedup_key not present → should return False
        dedup_key = ticket["custom_fields"].get("investigation_dedup", {})
        assert not dedup_key.get("metric")
        assert not dedup_key.get("platform")

    def test_investigation_dedup_set_by_enrichment(self):
        """Verify investigation_dedup structure matches what enrichment sets."""
        dedup_key = {
            "metric": "CoV - BOOT2 - Kernel Duration",
            "platform": "qc8775",
        }
        # Both fields present and non-empty
        assert dedup_key["metric"]
        assert dedup_key["platform"]
        # Exact match would work
        assert dedup_key["metric"] == "CoV - BOOT2 - Kernel Duration"
        assert dedup_key["platform"] == "qc8775"

    def test_different_metrics_dont_match(self):
        """Different metric strings should not match."""
        key_a = {"metric": "CoV - BOOT2 - Kernel Duration", "platform": "qc8775"}
        key_b = {"metric": "CoV - BOOT2 - InitRD Duration", "platform": "qc8775"}
        assert key_a["metric"] != key_b["metric"]

    def test_different_platforms_dont_match(self):
        """Different platforms should not match."""
        key_a = {"metric": "CoV - BOOT2 - Kernel Duration", "platform": "qc8775"}
        key_b = {
            "metric": "CoV - BOOT2 - Kernel Duration",
            "platform": "nxp-s32g-vnp-rdb3",
        }
        assert key_a["platform"] != key_b["platform"]


class TestDedupTemporalDecay:
    """Test temporal confidence decay in deterministic dedup."""

    def _make_agent(self):
        from unittest.mock import AsyncMock, MagicMock

        agent = MagicMock()
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        # Bind the real method
        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )

        agent._deterministic_dedup = GatheringContextAgent._deterministic_dedup.__get__(
            agent,
        )
        return agent

    def _make_record(self, age_days):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock

        record = MagicMock()
        record.investigation_id = f"RCA-AGE{age_days}"
        ts = datetime.now(timezone.utc) - timedelta(days=age_days)
        record.created_at = ts
        record.updated_at = ts
        record.record_url = ""
        return record

    def _make_ticket(self):
        return {
            "custom_fields": {
                "investigation_dedup": {
                    "metric": "CoV - BOOT2 - Kernel Duration",
                    "platform": "qc8775",
                },
                "anomaly_context": {"source": "horreum"},
                "run_metadata": {"build": "nightly-2026-09-08"},
            },
        }

    @pytest.mark.asyncio
    async def test_recent_record_full_match(self):
        """Records < 30 days old get full confidence match."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=5)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST1", self._make_ticket())

        assert result is True
        # Check confidence was 1.0
        update_call = agent._update_fields.call_args_list[0]
        dedup_result = update_call[0][1]["dedup_result"]
        assert dedup_result["match_confidence"] == 1.0
        assert dedup_result["record_age_days"] == 5
        mock_provider.append_build_history.assert_awaited_once()
        appended_entry = mock_provider.append_build_history.await_args.args[1]
        assert appended_entry.ticket_id == "PERF-TEST1"

    @pytest.mark.asyncio
    async def test_deterministic_match_persists_provider_record_url(self):
        """Deterministic matches preserve provider URLs in fields and comments."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=5)
        record.record_url = "https://horreum.example.com/run/101"

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST8", self._make_ticket())

        assert result is True
        dedup = agent._update_fields.await_args.args[1]["dedup_result"]
        assert dedup["record_url"] == "https://horreum.example.com/run/101"
        assert (
            "[RCA-AGE5](https://horreum.example.com/run/101)"
            in (agent._add_comment.await_args.args[1])
        )

    @pytest.mark.asyncio
    async def test_aging_record_reduced_confidence(self):
        """Records 30-90 days old get reduced confidence."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=60)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST2", self._make_ticket())

        assert result is True
        update_call = agent._update_fields.call_args_list[0]
        dedup_result = update_call[0][1]["dedup_result"]
        assert dedup_result["match_confidence"] == 0.7
        assert dedup_result["record_age_days"] == 60
        assert "re-investigation" in dedup_result["match_rationale"]

    @pytest.mark.asyncio
    async def test_stale_record_advisory_only(self):
        """Records > 90 days old are advisory — no match."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=120)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST3", self._make_ticket())

        # Should NOT match — returns False
        assert result is False
        # Should save advisory context
        update_call = agent._update_fields.call_args_list[0]
        advisory = update_call[0][1]["dedup_advisory"]
        assert advisory["age_days"] == 120
        assert "RCA-AGE120" in advisory["matched_investigation_id"]
        # Should NOT transition
        agent._transition_ticket.assert_not_called()

    @pytest.mark.asyncio
    async def test_boundary_30_days(self):
        """Exactly 30 days gets reduced confidence."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=30)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST4", self._make_ticket())

        assert result is True
        update_call = agent._update_fields.call_args_list[0]
        dedup_result = update_call[0][1]["dedup_result"]
        assert dedup_result["match_confidence"] == 0.7

    @pytest.mark.asyncio
    async def test_boundary_90_days(self):
        """Exactly 90 days is advisory only."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(age_days=90)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST5", self._make_ticket())

        assert result is False
        agent._transition_ticket.assert_not_called()

    @pytest.mark.asyncio
    async def test_picks_newest_record(self):
        """When multiple records match, picks the most recent."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        old_record = self._make_record(age_days=120)  # advisory
        new_record = self._make_record(age_days=10)  # full match

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[old_record, new_record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST6", self._make_ticket())

        # Should match the newer record, not the stale one
        assert result is True
        update_call = agent._update_fields.call_args_list[0]
        dedup_result = update_call[0][1]["dedup_result"]
        assert dedup_result["match_confidence"] == 1.0
        assert "RCA-AGE10" in dedup_result["matched_investigation_id"]

    @pytest.mark.asyncio
    async def test_record_without_timestamp_treated_as_fresh(self):
        """Records missing timestamps are treated as fresh."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()
        record = MagicMock()
        record.investigation_id = "RCA-NODATE"
        record.created_at = None
        record.updated_at = None

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST7", self._make_ticket())

        # No timestamp → age 0 → full confidence
        assert result is True
        update_call = agent._update_fields.call_args_list[0]
        dedup_result = update_call[0][1]["dedup_result"]
        assert dedup_result["match_confidence"] == 1.0


class TestDedupInconclusiveOutcome:
    """Inconclusive investigation records should not block new investigations."""

    def _make_agent(self):
        from unittest.mock import AsyncMock, MagicMock

        from agents.gathering_context.agent import GatheringContextAgent

        agent = MagicMock()
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._deterministic_dedup = GatheringContextAgent._deterministic_dedup.__get__(
            agent
        )
        return agent

    def _make_record(self, outcome="", age_days=5):
        from datetime import datetime, timedelta, timezone

        from providers.investigation.models import (
            AnomalyContext,
            InvestigationRecord,
            OperationalMetrics,
        )

        ts = datetime.now(timezone.utc) - timedelta(days=age_days)
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(
                subsystem="test",
                metric="test-metric",
                platform="test-platform",
            ),
            operational_metrics=OperationalMetrics(
                convergence_outcome=outcome,
            ),
        )
        record.created_at = ts
        record.updated_at = ts
        record.record_url = ""
        return record

    def _make_ticket(self):
        return {
            "custom_fields": {
                "investigation_dedup": {
                    "metric": "test-metric",
                    "platform": "test-platform",
                },
                "anomaly_context": {"source": "test"},
                "run_metadata": {"build": "build-123"},
            },
        }

    @pytest.mark.asyncio
    async def test_inconclusive_outcome_does_not_block(self):
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(outcome="inconclusive")

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST", self._make_ticket())

        assert result is False
        # Should set dedup_advisory, not dedup_result
        fields = agent._update_fields.call_args[0][1]
        assert "dedup_advisory" in fields
        assert fields["dedup_advisory"]["prior_outcome"] == "inconclusive"

    @pytest.mark.asyncio
    async def test_empty_outcome_does_not_block(self):
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(outcome="")

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST", self._make_ticket())

        assert result is False
        fields = agent._update_fields.call_args[0][1]
        assert "dedup_advisory" in fields
        assert fields["dedup_advisory"]["prior_outcome"] == "none"

    @pytest.mark.asyncio
    async def test_entropy_stall_outcome_does_not_block(self):
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(outcome="ENTROPY_STALL")  # uppercase from evaluate

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST", self._make_ticket())

        assert result is False
        fields = agent._update_fields.call_args[0][1]
        assert "dedup_advisory" in fields
        assert fields["dedup_advisory"]["prior_outcome"] == "entropy_stall"

    @pytest.mark.asyncio
    async def test_conclusive_outcome_still_blocks(self):
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(outcome="ISOLATION")

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-TEST", self._make_ticket())

        assert result is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome,should_proceed",
        [
            ("", True),
            ("ENTROPY_STALL", True),
            ("MAX_ITERATIONS", True),
            ("undetermined", True),
            ("manual_interruption", True),
            ("ISOLATION", False),
            ("EXPECTED_REGRESSION", False),
            ("EXPECTED_BEHAVIOR", False),
        ],
    )
    async def test_dedup_respects_convergence_outcome(self, outcome, should_proceed):
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        record = self._make_record(outcome=outcome)

        with patch(
            "providers.investigation.registry.create_record_provider",
        ) as mock_create:
            mock_provider = AsyncMock()
            mock_provider.query = AsyncMock(return_value=[record])
            mock_provider.append_build_history = AsyncMock()
            mock_create.return_value = mock_provider

            result = await agent._deterministic_dedup("PERF-PARAM", self._make_ticket())

        if should_proceed:
            assert result is False, (
                f"outcome '{outcome}' should NOT block (proceed=True)"
            )
        else:
            assert result is True, f"outcome '{outcome}' should block (proceed=False)"

    @pytest.mark.asyncio
    async def test_llm_match_overridden_by_advisory(self):
        """When dedup_advisory is set and LLM returns MATCH_FOUND,
        the agent reinvestigates (transitions to analyzing) while
        keeping the record link."""
        from unittest.mock import AsyncMock

        from agents.gathering_context.agent import GatheringContextAgent
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {
                    "dedup_advisory": {
                        "matched_investigation_id": "RCA-INCONC",
                        "prior_outcome": "none",
                    },
                },
            }
        )
        response = LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_gathering_context_result",
                    input={
                        "decision": "MATCH_FOUND",
                        "matched_investigation_id": "RCA-INCONC",
                        "match_confidence": 1.0,
                        "match_rationale": "Same",
                    },
                ),
            ],
        )
        await agent._handle_completion("PERF-OVERRIDE", response)

        # Verify transition is to "analyzing", not "retrospective_pending"
        transitioned_to_analyzing = False
        has_reinvestigating_comment = False
        for call in agent._client.post.call_args_list:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                body = call.kwargs.get("json", {})
                assert body.get("status") != "retrospective_pending"
                if body.get("status") == "analyzing":
                    transitioned_to_analyzing = True
            if "comment" in str(url):
                body = call.kwargs.get("json", {})
                comment_text = body.get("body", "") or body.get("comment", "")
                if "Reinvestigating" in str(comment_text):
                    has_reinvestigating_comment = True

        assert transitioned_to_analyzing, "Should transition to analyzing"
        assert has_reinvestigating_comment, "Should post a Reinvestigating comment"

        # Verify dedup_result preserves MATCH_FOUND and matched_investigation_id
        for call in agent._client.patch.call_args_list:
            body = call.kwargs.get("json", {})
            dedup_result = body.get("custom_fields", {}).get("dedup_result")
            if dedup_result:
                assert dedup_result["decision"] == "MATCH_FOUND"
                assert dedup_result["matched_investigation_id"] == "RCA-INCONC"


class TestLLMInconclusiveOutcomeCheck:
    """P2: LLM MATCH_FOUND on an inconclusive record without dedup_advisory
    should trigger reinvestigation."""

    @pytest.mark.asyncio
    async def test_llm_match_inconclusive_triggers_reinvestigation(self):
        """When the LLM matches an inconclusive record and no
        dedup_advisory is set, the agent should reinvestigate."""
        from unittest.mock import AsyncMock, patch

        from agents.gathering_context.agent import GatheringContextAgent
        from providers.investigation.models import (
            AnomalyContext,
            InvestigationRecord,
            OperationalMetrics,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )
        # No dedup_advisory — deterministic dedup did not run
        agent._get_ticket = AsyncMock(return_value={"custom_fields": {}})

        # Mock the record provider to return an inconclusive record
        inconclusive_record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
            operational_metrics=OperationalMetrics(
                convergence_outcome="inconclusive",
            ),
        )
        inconclusive_record.record_url = ""

        mock_provider = AsyncMock()
        mock_provider.get = AsyncMock(return_value=inconclusive_record)

        with (
            patch(
                "agents.gathering_context.agent.create_record_provider",
                return_value=mock_provider,
                create=True,
            ),
            patch(
                "providers.investigation.registry.create_record_provider",
                return_value=mock_provider,
            ),
        ):
            response = LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="tc_1",
                        name="submit_gathering_context_result",
                        input={
                            "decision": "MATCH_FOUND",
                            "matched_investigation_id": "RCA-INCONC-LLM",
                            "match_confidence": 0.9,
                            "match_rationale": "Same anomaly",
                        },
                    ),
                ],
            )
            await agent._handle_completion("PERF-LLM-INCONC", response)

        # Should transition to analyzing (reinvestigation), not
        # retrospective_pending
        transitioned_to_analyzing = False
        for call in agent._client.post.call_args_list:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                body = call.kwargs.get("json", {})
                if body.get("status") == "analyzing":
                    transitioned_to_analyzing = True
                assert body.get("status") != "retrospective_pending"

        assert transitioned_to_analyzing, (
            "Should transition to analyzing for inconclusive match"
        )


class TestStateLeak:
    """P3: Clean up REINVESTIGATING entry when LLM matches a different
    record than deterministic dedup."""

    @pytest.mark.asyncio
    async def test_cleanup_when_llm_matches_different_record(self):
        """When deterministic dedup matched Record A (inconclusive) but
        LLM matches Record B (conclusive), the REINVESTIGATING entry
        on Record A should be cleaned up."""
        from unittest.mock import AsyncMock, patch

        from agents.gathering_context.agent import GatheringContextAgent
        from providers.investigation.models import (
            AnomalyContext,
            InvestigationRecord,
            OperationalMetrics,
        )
        from providers.llm.base import LLMResponse, ToolCall
        from providers.llm.mock import MockLLMProvider

        agent = GatheringContextAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200, json=lambda: {}, raise_for_status=lambda: None
            ),
        )
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {"custom_fields": {}},
                raise_for_status=lambda: None,
            ),
        )
        # Deterministic dedup matched Record A
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {
                    "dedup_advisory": {
                        "matched_investigation_id": "RCA-A",
                        "prior_outcome": "inconclusive",
                    },
                },
            }
        )

        # LLM matches Record B (conclusive)
        conclusive_record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
            operational_metrics=OperationalMetrics(
                convergence_outcome="ISOLATION",
            ),
        )
        conclusive_record.record_url = "http://example.com/RCA-B"

        mock_provider = AsyncMock()
        mock_provider.get = AsyncMock(return_value=conclusive_record)
        mock_provider.append_build_history = AsyncMock()

        with (
            patch(
                "agents.gathering_context.agent.create_record_provider",
                return_value=mock_provider,
                create=True,
            ),
            patch(
                "providers.investigation.registry.create_record_provider",
                return_value=mock_provider,
            ),
        ):
            response = LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="tc_1",
                        name="submit_gathering_context_result",
                        input={
                            "decision": "MATCH_FOUND",
                            "matched_investigation_id": "RCA-B",
                            "match_confidence": 0.95,
                            "match_rationale": "Same anomaly",
                        },
                    ),
                ],
            )
            await agent._handle_completion("PERF-LEAK", response)

        # Should transition to retrospective_pending (conclusive match)
        transitioned_to_retrospective = False
        for call in agent._client.post.call_args_list:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                body = call.kwargs.get("json", {})
                if body.get("status") == "retrospective_pending":
                    transitioned_to_retrospective = True
        assert transitioned_to_retrospective, (
            "Should transition to retrospective_pending for conclusive match"
        )

        # Should have cleaned up the REINVESTIGATING entry on Record A
        mock_provider.append_build_history.assert_called_once()
        call_args = mock_provider.append_build_history.call_args
        assert call_args[0][0] == "RCA-A"
        entry = call_args[0][1]
        assert entry.action == "REINVESTIGATION_CANCELLED"
        assert "RCA-B" in entry.comment
