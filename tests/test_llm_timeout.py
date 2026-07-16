"""Tests for LLM API call timeout and stale-task watchdog.

Covers:
- LLMTimeoutError raised when LLM calls exceed timeout
- AgentBase handles LLMTimeoutError gracefully
- Orchestrator stale-task watchdog detects idle tasks
- Config controls for timeouts
- Timeout=0 disables the timeout
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.events import EventBus
from providers.llm.base import (
    DEFAULT_LLM_TIMEOUT,
    LLMProvider,
    LLMResponse,
    LLMTimeoutError,
    ToolDefinition,
)
from providers.llm.mock import MockLLMProvider


class TestLLMTimeoutError:
    """Test the LLMTimeoutError exception."""

    def test_attributes(self):
        err = LLMTimeoutError(120.0, "claude/claude-sonnet-4-6")
        assert err.timeout == 120.0
        assert err.provider == "claude/claude-sonnet-4-6"
        assert "120.0s" in str(err)
        assert "claude/claude-sonnet-4-6" in str(err)

    def test_default_provider(self):
        err = LLMTimeoutError(60.0)
        assert err.provider == "unknown"


class TestDefaultTimeout:
    """Test DEFAULT_LLM_TIMEOUT constant."""

    def test_default_value(self):
        assert DEFAULT_LLM_TIMEOUT == 120.0

    def test_provider_has_default_timeout_attr(self):
        provider = MockLLMProvider()
        assert hasattr(provider, "default_timeout")
        assert provider.default_timeout is None


class TestResolveTimeout:
    """Test LLMProvider._resolve_timeout precedence."""

    def test_explicit_call_wins(self):
        provider = MockLLMProvider()
        provider.default_timeout = 60.0
        assert provider._resolve_timeout(30.0) == 30.0

    def test_instance_default_used(self):
        provider = MockLLMProvider()
        provider.default_timeout = 60.0
        assert provider._resolve_timeout(None) == 60.0

    def test_global_default_fallback(self):
        provider = MockLLMProvider()
        assert provider._resolve_timeout(None) == DEFAULT_LLM_TIMEOUT

    def test_zero_disables(self):
        provider = MockLLMProvider()
        provider.default_timeout = 0
        assert provider._resolve_timeout(None) == 0

    def test_explicit_zero_overrides_instance(self):
        provider = MockLLMProvider()
        provider.default_timeout = 60.0
        assert provider._resolve_timeout(0) == 0


class TestMockProviderTimeout:
    """Test that MockLLMProvider accepts timeout parameter."""

    @pytest.mark.asyncio
    async def test_accepts_timeout(self):
        provider = MockLLMProvider()
        response = await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=30.0,
        )
        assert response is not None


class SlowLLMProvider(LLMProvider):
    """LLM provider that simulates a slow API call."""

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse:
        # Simulate a call that takes forever
        await asyncio.sleep(3600)
        return LLMResponse(text="should never reach here")


class TimeoutTestLLMProvider(LLMProvider):
    """LLM provider that raises LLMTimeoutError."""

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse:
        raise LLMTimeoutError(120.0, "test/model")


class TestClaudeTimeout:
    """Test Claude provider timeout wrapping."""

    @pytest.mark.asyncio
    async def test_timeout_raises_llm_timeout_error(self):
        """Verify asyncio.TimeoutError is converted to LLMTimeoutError."""
        from providers.llm.claude import ClaudeLLMProvider

        provider = ClaudeLLMProvider.__new__(ClaudeLLMProvider)
        provider._model = "test-model"
        provider.default_timeout = None

        # Mock the client to hang
        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(
            side_effect=lambda **kwargs: time.sleep(10)
        )
        provider._client = mock_client

        with pytest.raises(LLMTimeoutError) as exc_info:
            await provider.complete(
                system_prompt="test",
                messages=[{"role": "user", "content": "hi"}],
                timeout=0.1,
            )
        assert exc_info.value.timeout == 0.1
        assert "claude" in exc_info.value.provider

    @pytest.mark.asyncio
    async def test_timeout_zero_disables(self):
        """Verify timeout=0 disables the timeout wrapper."""
        from providers.llm.claude import ClaudeLLMProvider

        provider = ClaudeLLMProvider.__new__(ClaudeLLMProvider)
        provider._model = "test-model"
        provider.default_timeout = 0

        # Mock client returns immediately
        mock_response = MagicMock()
        mock_response.content = []
        mock_response.stop_reason = "end_turn"
        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(return_value=mock_response)
        provider._client = mock_client

        # Should not raise — timeout=0 means no wrapping
        response = await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response is not None


class TestOpenAITimeout:
    """Test OpenAI-compat provider timeout wrapping."""

    @pytest.mark.asyncio
    async def test_timeout_raises_llm_timeout_error(self):
        from providers.llm.openai_compat import OpenAICompatLLMProvider

        provider = OpenAICompatLLMProvider.__new__(OpenAICompatLLMProvider)
        provider._model = "test-model"
        provider.default_timeout = None

        mock_client = MagicMock()
        mock_client.chat.completions.create = MagicMock(
            side_effect=lambda **kwargs: time.sleep(10)
        )
        provider._client = mock_client

        with pytest.raises(LLMTimeoutError) as exc_info:
            await provider.complete(
                system_prompt="test",
                messages=[{"role": "user", "content": "hi"}],
                timeout=0.1,
            )
        assert exc_info.value.timeout == 0.1
        assert "openai" in exc_info.value.provider


class TestAgentTimeoutHandling:
    """Test AgentBase handling of LLMTimeoutError."""

    @pytest.mark.asyncio
    async def test_agent_retries_then_transitions_on_timeout(self, tmp_path):
        """Agent should retry LLM_TIMEOUT_RETRIES times then transition."""
        from agents.base import AgentBase

        class TestAgent(AgentBase):
            LLM_TIMEOUT_RETRIES = 2

            def _system_prompt(self, ticket):
                return "test prompt"

            def _build_messages(self, ticket):
                return [{"role": "user", "content": "test"}]

            async def _handle_completion(self, ticket_id, response):
                pass

        events = EventBus(log_dir=str(tmp_path))
        agent = TestAgent(
            agent_name="test-agent",
            llm_provider=TimeoutTestLLMProvider(),
            state_store_url="http://localhost:9999",
            event_bus=events,
        )

        # Mock the HTTP calls
        agent._get_ticket = AsyncMock(
            return_value={
                "id": "TEST-001",
                "summary": "test",
                "description": "test",
                "status": "executing_benchmark",
                "custom_fields": {},
            }
        )
        agent._transition_ticket = AsyncMock()
        agent._add_comment = AsyncMock()

        await agent.run("TEST-001")

        # Should have transitioned to awaiting_customer_guidance
        agent._transition_ticket.assert_called_once()
        call_args = agent._transition_ticket.call_args
        assert call_args[0][1] == "awaiting_customer_guidance"
        assert "timed out" in call_args[1]["comment"]
        assert "2 retries" in call_args[1]["comment"]

        # Should have added a user-facing comment
        agent._add_comment.assert_called_once()
        comment = agent._add_comment.call_args[0][1]
        assert "timed out" in comment
        assert "2 automatic retries" in comment

        # Check error events: 2 retries + 1 final = 3 events
        ticket_events = events.get_events("TEST-001", since=0, limit=100)
        error_events = [
            e for e in ticket_events if e.get("event_type") == "agent_error"
        ]
        assert len(error_events) == 3
        # First two are retries
        assert error_events[0]["data"]["retry"] == 1
        assert error_events[1]["data"]["retry"] == 2
        # Third is final (retries exhausted)
        assert error_events[2]["data"]["retries_exhausted"] is True

    @pytest.mark.asyncio
    async def test_agent_recovers_after_transient_timeout(self, tmp_path):
        """Agent should continue if retry succeeds."""
        from agents.base import AgentBase

        call_count = 0

        class TransientTimeoutProvider(LLMProvider):
            async def complete(
                self, system_prompt, messages, tools=None, max_tokens=4096, timeout=None
            ):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise LLMTimeoutError(120.0, "test/model")
                # Second call succeeds with end_turn
                return LLMResponse(
                    text="Done",
                    tool_calls=[],
                    stop_reason="end_turn",
                )

        class TestAgent(AgentBase):
            LLM_TIMEOUT_RETRIES = 2

            def _system_prompt(self, ticket):
                return "test prompt"

            def _build_messages(self, ticket):
                return [{"role": "user", "content": "test"}]

            async def _handle_completion(self, ticket_id, response):
                pass

        events = EventBus(log_dir=str(tmp_path))
        agent = TestAgent(
            agent_name="test-agent",
            llm_provider=TransientTimeoutProvider(),
            state_store_url="http://localhost:9999",
            event_bus=events,
        )
        agent._get_ticket = AsyncMock(
            return_value={
                "id": "TEST-002",
                "summary": "test",
                "description": "test",
                "status": "executing_benchmark",
                "custom_fields": {},
            }
        )
        agent._transition_ticket = AsyncMock()
        agent._add_comment = AsyncMock()

        await agent.run("TEST-002")

        # Should NOT have transitioned to guidance — retry succeeded
        agent._transition_ticket.assert_not_called()
        # LLM was called twice: 1 timeout + 1 success
        assert call_count == 2
        # Only 1 error event (the retry), not a final failure
        ticket_events = events.get_events("TEST-002", since=0, limit=100)
        error_events = [
            e for e in ticket_events if e.get("event_type") == "agent_error"
        ]
        assert len(error_events) == 1
        assert error_events[0]["data"]["retry"] == 1


class TestEventBusLastEventTime:
    """Test EventBus.last_event_time tracking."""

    def test_no_events_returns_none(self, tmp_path):
        bus = EventBus(log_dir=str(tmp_path))
        assert bus.last_event_time("NONEXISTENT") is None

    def test_tracks_last_event(self, tmp_path):
        bus = EventBus(log_dir=str(tmp_path))
        before = time.time()
        bus.emit("TRACK-001", "test-agent", "test_event", {"key": "val"})
        after = time.time()

        last = bus.last_event_time("TRACK-001")
        assert last is not None
        assert before <= last <= after

    def test_updates_on_new_events(self, tmp_path):
        bus = EventBus(log_dir=str(tmp_path))
        bus.emit("TRACK-002", "agent", "event1")
        t1 = bus.last_event_time("TRACK-002")

        time.sleep(0.01)
        bus.emit("TRACK-002", "agent", "event2")
        t2 = bus.last_event_time("TRACK-002")

        assert t2 > t1


class TestStaleTaskWatchdog:
    """Test _check_stale_tasks in the orchestrator."""

    @pytest.mark.asyncio
    async def test_cancels_stale_task(self, tmp_path):
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path))
        dispatcher = MagicMock()

        # Simulate a task that's been idle for a long time
        stale_task = MagicMock()
        stale_task.done.return_value = False
        dispatcher.active_tasks.return_value = {
            "STALE-001": stale_task,
        }

        # Emit an event long ago
        events.emit("STALE-001", "agent", "tool_called")
        # Backdate the last event time
        events._last_event_time["STALE-001"] = time.time() - 1000

        await _check_stale_tasks(
            dispatcher,
            events,
            stale_timeout=900,
            store_url="http://localhost:9999",
        )

        stale_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_cancel_active_task(self, tmp_path):
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path))
        dispatcher = MagicMock()

        active_task = MagicMock()
        active_task.done.return_value = False
        dispatcher.active_tasks.return_value = {
            "ACTIVE-001": active_task,
        }

        # Recent event
        events.emit("ACTIVE-001", "agent", "tool_called")

        await _check_stale_tasks(
            dispatcher,
            events,
            stale_timeout=900,
            store_url="http://localhost:9999",
        )

        active_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_done_tasks(self, tmp_path):
        """Done tasks are filtered by active_tasks(), not seen."""
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path))
        dispatcher = MagicMock()
        # active_tasks() returns empty — done tasks are pruned
        dispatcher.active_tasks.return_value = {}

        events.emit("DONE-001", "agent", "event")
        events._last_event_time["DONE-001"] = time.time() - 2000

        await _check_stale_tasks(
            dispatcher,
            events,
            stale_timeout=900,
            store_url="http://localhost:9999",
        )
        # Nothing to cancel

    @pytest.mark.asyncio
    async def test_ignores_unknown_tickets(self, tmp_path):
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path))
        dispatcher = MagicMock()

        task = MagicMock()
        task.done.return_value = False
        dispatcher.active_tasks.return_value = {
            "UNKNOWN-001": task,
        }

        # No events emitted for this ticket
        await _check_stale_tasks(
            dispatcher,
            events,
            stale_timeout=900,
            store_url="http://localhost:9999",
        )

        task.cancel.assert_not_called()


class TestDispatcherActiveTasks:
    """Test Dispatcher.active_tasks() method."""

    def test_returns_active_only(self):
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher._tasks = {}
        dispatcher._dispatched = set()
        dispatcher._handoff_blocked = set()

        active = MagicMock()
        active.done.return_value = False
        done = MagicMock()
        done.done.return_value = True

        dispatcher._tasks = {"A": active, "B": done}
        result = dispatcher.active_tasks()
        assert "A" in result
        assert "B" not in result
        # Done task should be cleaned up
        assert "B" not in dispatcher._tasks


class TestEnvOrCfg:
    """Test _env_or_cfg handles zero values correctly."""

    def test_env_zero(self):
        from orchestrator.config import _env_or_cfg

        with patch.dict("os.environ", {"MY_KEY": "0"}):
            assert _env_or_cfg("MY_KEY", {}, "k", 120.0) == 0.0

    def test_cfg_zero(self):
        from orchestrator.config import _env_or_cfg

        with patch.dict("os.environ", {}, clear=True):
            assert _env_or_cfg("MY_KEY", {"k": 0}, "k", 120.0) == 0.0

    def test_default_fallback(self):
        from orchestrator.config import _env_or_cfg

        with patch.dict("os.environ", {}, clear=True):
            assert _env_or_cfg("MY_KEY", {}, "k", 120.0) == 120.0


class TestOrchestratorConfig:
    """Test timeout configuration options."""

    def test_default_values(self):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            assert config.llm_timeout == 120.0
            assert config.agent_task_timeout == 0
            assert config.stale_task_timeout == 3600.0

    def test_env_override(self):
        from orchestrator.config import OrchestratorConfig

        with patch.dict(
            "os.environ",
            {
                "LLM_TIMEOUT": "60",
                "AGENT_TASK_TIMEOUT": "3600",
                "STALE_TASK_TIMEOUT": "300",
            },
        ):
            config = OrchestratorConfig()
            assert config.llm_timeout == 60.0
            assert config.agent_task_timeout == 3600.0
            assert config.stale_task_timeout == 300.0

    def test_disable_with_zero(self):
        from orchestrator.config import OrchestratorConfig

        with patch.dict(
            "os.environ",
            {
                "LLM_TIMEOUT": "0",
                "STALE_TASK_TIMEOUT": "0",
            },
        ):
            config = OrchestratorConfig()
            assert config.llm_timeout == 0
            assert config.stale_task_timeout == 0


class TestRunAgentTaskTimeout:
    """Test agent_task_timeout in run_agent_task."""

    @pytest.mark.asyncio
    async def test_task_timeout_emits_error(self, tmp_path):
        """Agent task that exceeds timeout should emit error event."""
        from orchestrator.main import run_agent_task

        events = EventBus(log_dir=str(tmp_path))

        # Create a slow agent using a real coroutine
        async def slow_run(tid):
            await asyncio.sleep(10)

        slow_agent = MagicMock()
        slow_agent.run = slow_run
        slow_agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = slow_agent
        dispatcher.store_url = "http://localhost:9999"
        dispatcher.events = events

        # Use a status not in PLAN_AGENT_STATUS to avoid
        # _advance_plan trying to reach the state store.
        await run_agent_task(dispatcher, "triaging", "SLOW-001", agent_task_timeout=0.1)

        # Should have emitted agent_error event
        ticket_events = events.get_events("SLOW-001", since=0, limit=100)
        error_events = [
            e for e in ticket_events if e.get("event_type") == "agent_error"
        ]
        assert len(error_events) == 1
        assert error_events[0]["data"]["reason"] == "agent_task_timeout"

    @pytest.mark.asyncio
    async def test_no_timeout_when_zero(self):
        """agent_task_timeout=0 should not wrap with wait_for."""
        from orchestrator.main import run_agent_task

        agent = MagicMock()
        agent.run = AsyncMock()
        agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = agent
        dispatcher.store_url = "http://localhost:9999"
        dispatcher.events = None

        await run_agent_task(
            dispatcher, "executing_benchmark", "FAST-001", agent_task_timeout=0
        )

        agent.run.assert_called_once_with("FAST-001")
