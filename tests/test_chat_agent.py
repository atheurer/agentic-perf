"""Tests for the chat agent."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.chat.agent import ChatSession, ChatSessionStore
from agents.chat.tools import CHAT_TOOLS, ChatToolAudit, execute_tool


def _audit(client: AsyncMock) -> ChatToolAudit:
    """Use the production audit boundary without a real trace service."""
    return ChatToolAudit(
        client, "http://localhost:8090", "service-token", record=lambda _: None
    )


# --- Session tests ---


class TestChatSession:
    def test_add_user_message(self):
        session = ChatSession(user="test")
        session.add_user_message("hello")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello"

    def test_add_assistant_message(self):
        session = ChatSession(user="test")
        session.add_assistant_message("hi there")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "assistant"

    def test_truncation(self):
        session = ChatSession(user="test")
        for i in range(150):
            session.add_user_message(f"msg {i}")
        assert len(session.messages) <= 100

    def test_truncation_drops_orphaned_tool_results(self):
        session = ChatSession(
            user="test",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1"},
                        {"type": "tool_use", "id": "t2"},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1"},
                        {"type": "tool_result", "tool_use_id": "t2"},
                    ],
                },
                *[
                    message
                    for i in range(49)
                    for message in (
                        {"role": "assistant", "content": f"assistant {i}"},
                        {"role": "user", "content": f"user {i}"},
                    )
                ],
                {"role": "assistant", "content": "latest"},
            ],
        )

        session._truncate()

        assert len(session.messages) == 98
        assert session.messages[0] == {"role": "user", "content": "user 0"}
        assert all(not session._is_tool_result(message) for message in session.messages)

    def test_truncation_starts_with_user_when_suffix_starts_with_assistant(self):
        messages = [{"role": "user", "content": "initial"}]
        for i in range(50):
            messages.extend(
                (
                    {"role": "assistant", "content": f"assistant {i}"},
                    {"role": "user", "content": f"user {i}"},
                )
            )
        session = ChatSession(user="test", messages=messages)

        session._truncate()

        assert len(session.messages) == 99
        assert session.messages[0] == {"role": "user", "content": "user 0"}

    def test_record_usage(self):
        session = ChatSession(user="test")
        session.record_usage({"input_tokens": 100, "output_tokens": 50})
        session.record_usage({"input_tokens": 200, "output_tokens": 75})
        assert session.total_input_tokens == 300
        assert session.total_output_tokens == 125

    def test_record_usage_none(self):
        session = ChatSession(user="test")
        session.record_usage(None)
        assert session.total_input_tokens == 0


class TestChatSessionStore:
    def test_get_or_create(self):
        store = ChatSessionStore()
        session = store.get_or_create("alice")
        assert session.user == "alice"
        # Same user returns same session
        same = store.get_or_create("alice")
        assert same is session

    def test_different_users(self):
        store = ChatSessionStore()
        alice = store.get_or_create("alice")
        bob = store.get_or_create("bob")
        assert alice is not bob

    def test_eviction(self):
        store = ChatSessionStore(max_sessions=2)
        store.get_or_create("alice")
        store.get_or_create("bob")
        store.get_or_create("charlie")
        # alice should have been evicted
        usage = store.get_usage("alice")
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["llm_calls"] == 0

    def test_delete(self):
        store = ChatSessionStore()
        store.get_or_create("alice")
        assert store.delete("alice") is True
        assert store.delete("alice") is False

    def test_get_usage(self):
        store = ChatSessionStore()
        session = store.get_or_create("alice")
        session.record_usage({"input_tokens": 50, "output_tokens": 25})
        usage = store.get_usage("alice")
        assert usage["input_tokens"] == 50
        assert usage["output_tokens"] == 25

    def test_get_usage_nonexistent(self):
        store = ChatSessionStore()
        usage = store.get_usage("nobody")
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["llm_calls"] == 0


class TestConfirmationReplies:
    def test_accepts_conversational_confirmation(self):
        from agents.chat.agent import _is_confirmation

        assert _is_confirmation("yes, please submit it")
        assert _is_confirmation("Go ahead and create it")
        assert _is_confirmation("okay!")

    def test_rejects_non_confirmation(self):
        from agents.chat.agent import _is_confirmation

        assert not _is_confirmation("change the board to qc8775")

    def test_accepts_conversational_cancellation(self):
        from agents.chat.agent import _is_cancellation

        assert _is_cancellation("no, change the summary first")


# --- Tool definition tests ---


class TestToolDefinitions:
    def test_all_tools_have_names(self):
        names = [t.name for t in CHAT_TOOLS]
        assert "search_tickets" in names
        assert "get_ticket" in names
        assert "create_ticket" in names
        assert "start_ticket" in names
        assert "send_interjection" in names
        assert "reply_to_guidance" in names
        assert "stop_ticket" in names
        assert "list_available_benchmarks" in names

    def test_all_tools_have_schemas(self):
        for tool in CHAT_TOOLS:
            assert tool.input_schema is not None
            assert tool.input_schema.get("type") == "object"

    def test_create_ticket_requires_fields(self):
        create = next(t for t in CHAT_TOOLS if t.name == "create_ticket")
        required = create.input_schema.get("required", [])
        assert "summary" in required
        assert "description" in required
        assert "custom_fields" in required

    def test_stop_ticket_uses_supported_stop_mode(self):
        stop = next(t for t in CHAT_TOOLS if t.name == "stop_ticket")
        mode = stop.input_schema["properties"]["mode"]
        assert mode["enum"] == ["graceful", "hard"]


# --- Tool execution tests ---


class TestToolExecution:
    async def test_list_available_benchmarks_is_read_only_and_filterable(
        self, monkeypatch
    ):
        from agents.chat import tools
        from providers.skills.base import BenchmarkSuite

        class Provider:
            def list_harnesses(self):
                return ["crucible", "offline"]

            def get_provider(self, harness):
                if harness == "offline":
                    return OfflineProvider()
                return CrucibleProvider()

        class CrucibleProvider:
            async def list_benchmarks(self):
                return [
                    BenchmarkSuite(
                        name="fio",
                        description="Storage I/O",
                        harness="crucible",
                        roles=["client"],
                        min_hosts=1,
                    )
                ]

        class OfflineProvider:
            async def list_benchmarks(self):
                raise RuntimeError("catalog unavailable")

        monkeypatch.setattr(
            tools,
            "_get_benchmark_catalog_provider",
            AsyncMock(return_value=Provider()),
        )
        client = AsyncMock()
        result = json.loads(
            await execute_tool(
                "list_available_benchmarks",
                {"query": "storage"},
                client,
                "http://localhost:8090",
                "token123",
                audit=_audit(client),
            )
        )

        assert result["total"] == 1
        assert result["harnesses"] == ["crucible"]
        assert result["benchmarks"]["crucible"][0]["name"] == "fio"
        assert result["unavailable_harnesses"] == ["offline"]
        client.get.assert_not_awaited()
        client.patch.assert_not_awaited()
        client.post.assert_not_awaited()

    async def test_search_tickets(self):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(
            return_value=[
                {"id": "PERF-123", "summary": "test", "status": "closed"},
            ]
        )
        response.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=response)

        result = await execute_tool(
            "search_tickets",
            {"limit": 5},
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-123"

    async def test_get_ticket(self):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(
            return_value={
                "id": "PERF-123",
                "summary": "test ticket",
                "status": "closed",
                "status_trail": ["new", "closed"],
                "created_at": "2026-08-01",
                "custom_fields": {
                    "guidance_summary": {"reason": "timeout"},
                    "verdict": "confirmed",
                },
                "comments": [
                    {"author": "agent", "body": "done"},
                ],
            }
        )
        response.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=response)

        result = await execute_tool(
            "get_ticket",
            {"ticket_id": "PERF-123"},
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["id"] == "PERF-123"
        assert parsed["guidance_summary"]["reason"] == "timeout"

    async def test_create_ticket(self):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(return_value={"id": "PERF-NEW"})
        response.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=response)

        result = await execute_tool(
            "create_ticket",
            {
                "summary": "test",
                "description": "testing",
                "custom_fields": {"harness": "boot-time"},
            },
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["id"] == "PERF-NEW"

    async def test_stop_ticket_returns_structured_conflict_for_409(self):
        client = AsyncMock()
        details = (
            "Ticket PERF-123 is in terminal state 'closed' — nothing to stop",
            "Ticket PERF-123 is in paused state 'awaiting_customer_guidance'"
            " — nothing to stop",
        )
        responses = []
        for detail in details:
            response = AsyncMock()
            response.status_code = 409
            response.json = MagicMock(return_value={"detail": detail})
            responses.append(response)
        client.post = AsyncMock(side_effect=responses)

        for detail in details:
            result = await execute_tool(
                "stop_ticket",
                {"ticket_id": "PERF-123", "mode": "hard"},
                client,
                "http://localhost:8090",
                "token123",
                audit=_audit(client),
            )
            assert json.loads(result) == {"status": "cannot_stop", "detail": detail}

        assert client.post.await_count == 2
        assert client.post.await_args_list[0].kwargs == {
            "headers": {"Authorization": "Bearer token123"},
            "json": {"mode": "hard"},
        }

    async def test_stop_ticket_409_handles_non_object_error_json(self):
        client = AsyncMock()
        response = AsyncMock()
        response.status_code = 409
        response.json = MagicMock(return_value=["unexpected error shape"])
        client.post = AsyncMock(return_value=response)

        result = await execute_tool(
            "stop_ticket",
            {"ticket_id": "PERF-123"},
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )

        assert json.loads(result) == {
            "status": "cannot_stop",
            "detail": "Ticket cannot be stopped",
        }

    async def test_unknown_tool(self):
        client = AsyncMock()
        result = await execute_tool(
            "nonexistent",
            {},
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_tool_error_handling(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("connection failed"))

        result = await execute_tool(
            "search_tickets",
            {},
            client,
            "http://localhost:8090",
            "token123",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "connection failed" in parsed["error"]


# --- Search filtering tests ---


class TestSearchFiltering:
    async def test_status_filter(self):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(
            return_value=[
                # Server-side status filter returns only matches
                {"id": "PERF-1", "summary": "a", "status": "closed"},
            ]
        )
        response.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=response)

        result = await execute_tool(
            "search_tickets",
            {"status": "closed", "limit": 10},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"
        # Verify status passed as server-side param
        call_kwargs = client.get.call_args
        assert call_kwargs.kwargs.get("params", {}).get("status") == "closed"

    async def test_query_filter(self):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(
            return_value=[
                {"id": "PERF-1", "summary": "boot time test", "status": "closed"},
                {"id": "PERF-2", "summary": "network test", "status": "closed"},
            ]
        )
        response.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=response)

        result = await execute_tool(
            "search_tickets",
            {"query": "boot", "limit": 10},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"


# --- handle_message tests ---


class TestHandleMessage:
    async def test_cancelled_mutating_tool_reconciles_history_and_blocks_follow_up(
        self,
    ):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock()
        response.text = None
        response.tool_calls = [
            ToolCall(
                id="toolu-read",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            )
        ]
        response.raw_content = [{"type": "tool_use", "id": "toolu-read"}]
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        tool_started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        finish_tool = asyncio.Event()

        async def cancellation_resistant_tool(*_args, **_kwargs):
            tool_started.set()
            try:
                await finish_tool.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await finish_tool.wait()
            return '{"id": "PERF-1"}'

        with patch("agents.chat.agent.execute_tool", cancellation_resistant_tool):
            task = asyncio.create_task(
                agent.handle_message("alice", "inspect ticket", "token")
            )
            await tool_started.wait()
            task.cancel()
            await cancellation_seen.wait()

            assert "still finishing" in await agent.handle_message(
                "alice", "do something else", "token"
            )
            assert agent.clear_session("alice") is False
            finish_tool.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        session = agent._sessions.get_or_create("alice")
        assert session.pending_action is None
        tool_use_message = next(
            message
            for message in session.messages
            if message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            and any(block.get("type") == "tool_use" for block in message["content"])
        )
        result_message = session.messages[session.messages.index(tool_use_message) + 1]
        assert result_message["content"][0]["tool_use_id"] == "toolu-read"
        assert (
            "outcome is uncertain"
            in json.loads(result_message["content"][0]["content"])["error"]
        )
        assert "outcome is uncertain" in session.messages[-1]["content"]
        assert (
            "clear the chat session"
            in (await agent.handle_message("alice", "retry it", "token")).lower()
        )
        assert llm.complete.await_count == 1
        assert agent.clear_session("alice") is True

    async def test_later_read_cancellation_preserves_completed_mutation_result(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        calls = [
            ToolCall(
                id="toolu-start",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            ),
            ToolCall(
                id="toolu-read",
                name="get_ticket",
                input={"ticket_id": "PERF-1"},
            ),
        ]
        response = MagicMock(
            text=None,
            tool_calls=calls,
            raw_content=[
                {"type": "tool_use", "id": call.id, "name": call.name} for call in calls
            ],
        )
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        dispatches = 0

        async def execute(name, *_args, **_kwargs):
            nonlocal dispatches
            dispatches += 1
            if name == "start_ticket":
                return '{"id":"PERF-1","status":"running"}'
            raise asyncio.CancelledError

        with patch("agents.chat.agent.execute_tool", execute):
            with pytest.raises(asyncio.CancelledError):
                await agent.handle_message("alice", "start and inspect", "token")

        session = agent._sessions.get_or_create("alice")
        tool_use_message = next(
            message
            for message in session.messages
            if message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            and any(block.get("type") == "tool_use" for block in message["content"])
        )
        result_message = session.messages[session.messages.index(tool_use_message) + 1]
        assert [call["id"] for call in tool_use_message["content"]] == [
            "toolu-start",
            "toolu-read",
        ]
        assert [result["tool_use_id"] for result in result_message["content"]] == [
            "toolu-start",
            "toolu-read",
        ]
        assert json.loads(result_message["content"][0]["content"]) == {
            "id": "PERF-1",
            "status": "running",
        }
        assert (
            "read-only lookup was interrupted"
            in json.loads(result_message["content"][1]["content"])["error"]
        )
        assert dispatches == 2
        assert agent.is_session_indeterminate("alice")
        blocked = await agent.handle_message("alice", "start it again", "token")
        assert "outcome is uncertain" in blocked
        assert llm.complete.await_count == 1

    async def test_cancelled_followup_llm_blocks_retry_after_mutation(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(side_effect=[tool_response, asyncio.CancelledError()])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )

        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ) as execute:
            with pytest.raises(asyncio.CancelledError):
                await agent.handle_message("alice", "start PERF-1", "token")

        execute.assert_awaited_once()
        assert agent.is_session_indeterminate("alice")
        session = agent._sessions.get_or_create("alice")
        result_message = next(
            message["content"]
            for message in session.messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(item.get("type") == "tool_result" for item in message["content"])
        )
        assert result_message[0]["tool_use_id"] == "toolu-start"
        assert json.loads(result_message[0]["content"]) == {
            "id": "PERF-1",
            "status": "running",
        }
        assert "may have completed" in session.messages[-1]["content"]
        assert "may have completed" in await agent.handle_message(
            "alice", "start PERF-1 again", "token"
        )
        assert llm.complete.await_count == 2

    async def test_later_batch_read_cancellation_keeps_prior_mutation_indeterminate(
        self,
    ):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        mutation_call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        read_call = ToolCall(
            id="toolu-read",
            name="get_ticket",
            input={"ticket_id": "PERF-1"},
        )
        responses = []
        for call in (mutation_call, read_call):
            response = MagicMock(
                text=None,
                tool_calls=[call],
                raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
            )
            response.usage = {}
            responses.append(response)
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(side_effect=responses)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )

        async def execute(name, *_args, **_kwargs):
            if name == "start_ticket":
                return '{"id":"PERF-1","status":"running"}'
            raise asyncio.CancelledError

        with patch("agents.chat.agent.execute_tool", execute):
            with pytest.raises(asyncio.CancelledError):
                await agent.handle_message("alice", "start and inspect", "token")

        assert agent.is_session_indeterminate("alice")
        session = agent._sessions.get_or_create("alice")
        result_ids = [
            result["tool_use_id"]
            for message in session.messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            for result in message["content"]
            if result.get("type") == "tool_result"
        ]
        assert result_ids == ["toolu-start", "toolu-read"]
        assert json.loads(
            next(
                result["content"]
                for message in session.messages
                if message.get("role") == "user"
                and isinstance(message.get("content"), list)
                for result in message["content"]
                if result.get("tool_use_id") == "toolu-start"
            )
        ) == {"id": "PERF-1", "status": "running"}

    async def test_readonly_followup_cancellation_does_not_pause_without_mutation(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-read",
            name="get_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(side_effect=[tool_response, asyncio.CancelledError()])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )

        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1"}',
        ):
            with pytest.raises(asyncio.CancelledError):
                await agent.handle_message(
                    "anonymous-session", "inspect PERF-1", "", readonly=True
                )

        assert not agent.is_session_indeterminate("anonymous-session")
        fresh_response = MagicMock(
            text="The lookup can be repeated safely.", tool_calls=[], raw_content=[]
        )
        fresh_response.usage = {}
        llm.complete.side_effect = None
        llm.complete.return_value = fresh_response
        assert (
            await agent.handle_message(
                "anonymous-session", "inspect again", "", readonly=True
            )
            == "The lookup can be repeated safely."
        )

    async def test_closing_stream_after_mutation_pauses_before_retry(self):
        from unittest.mock import patch

        from agents.chat.agent import _CHAT_BUSY_MESSAGE, ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="Ticket started.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=[tool_response, final_response])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        stream = agent.handle_message_stream("alice", "start PERF-1", "token")

        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ):
            assert await anext(stream) == "Ticket started."
            assert "alice" in agent._pending_response_deliveries
            assert agent.clear_session("alice") is False
            assert await agent.handle_message("alice", "retry", "token") == (
                _CHAT_BUSY_MESSAGE
            )
            await stream.aclose()

        assert agent.is_session_indeterminate("alice")
        session = agent._sessions.get_or_create("alice")
        assert "Ticket started." in session.messages[-2]["content"]
        assert await agent.handle_message("alice", "start again", "token")
        assert llm.complete.await_count == 2

    async def test_later_read_audit_failure_blocks_after_completed_mutation(self):
        from unittest.mock import patch

        from agents.chat.agent import ChatAgent
        from agents.chat.tools import ChatAuditUnavailable
        from providers.llm.base import ToolCall

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        calls = [
            ToolCall(
                id="toolu-start",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            ),
            ToolCall(
                id="toolu-read",
                name="get_ticket",
                input={"ticket_id": "PERF-1"},
            ),
        ]
        response = MagicMock(
            text=None,
            tool_calls=calls,
            raw_content=[
                {"type": "tool_use", "id": call.id, "name": call.name} for call in calls
            ],
        )
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )

        async def execute(name, *_args, **_kwargs):
            if name == "start_ticket":
                return '{"id":"PERF-1","status":"running"}'
            raise ChatAuditUnavailable("audit ingestion unavailable")

        with patch("agents.chat.agent.execute_tool", execute):
            result = await agent.handle_message("alice", "start and inspect", "token")

        assert "outcome is uncertain" in result
        assert agent.is_session_indeterminate("alice")
        session = agent._sessions.get_or_create("alice")
        tool_results = next(
            message["content"]
            for message in session.messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(item.get("type") == "tool_result" for item in message["content"])
        )
        assert [item["tool_use_id"] for item in tool_results] == [
            "toolu-start",
            "toolu-read",
        ]
        assert json.loads(tool_results[0]["content"]) == {
            "id": "PERF-1",
            "status": "running",
        }
        assert llm.complete.await_count == 1

    async def test_readonly_dispatch_guard_rejects_model_mutation_and_unknown_tools(
        self,
    ):
        from unittest.mock import patch

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        calls = [
            ToolCall(
                id="toolu-read",
                name="get_ticket",
                input={"ticket_id": "PERF-1"},
            ),
            ToolCall(
                id="toolu-mutate",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            ),
            ToolCall(
                id="toolu-unknown",
                name="invented_tool",
                input={},
            ),
        ]
        tool_response = MagicMock(
            text=None,
            tool_calls=calls,
            raw_content=[
                {"type": "tool_use", "id": call.id, "name": call.name} for call in calls
            ],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="I only used the allowed read.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=[tool_response, final_response])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        executed_names = []
        audit_states = []

        async def post(_url, **kwargs):
            audit_states.append(kwargs["json"]["lifecycle"]["state"])
            audit_response = MagicMock()
            audit_response.raise_for_status = MagicMock()
            return audit_response

        agent._client.post = AsyncMock(side_effect=post)

        async def fake_execute(name, *_args, **_kwargs):
            executed_names.append(name)
            assert name == "get_ticket"
            return '{"id":"PERF-1"}'

        with patch("agents.chat.agent.execute_tool", fake_execute):
            result = await agent.handle_message(
                "anonymous-session", "inspect PERF-1", "", readonly=True
            )

        assert result == "I only used the allowed read."
        assert executed_names == ["get_ticket"]
        assert audit_states == ["started", "rejected"]
        assert not agent.is_session_indeterminate("anonymous-session")
        sent_messages = llm.complete.await_args_list[1].kwargs["messages"]
        tool_results = next(
            message["content"]
            for message in sent_messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(item.get("type") == "tool_result" for item in message["content"])
        )
        assert [item["tool_use_id"] for item in tool_results] == [
            "toolu-read",
            "toolu-mutate",
            "toolu-unknown",
        ]
        assert "read-only mode" in json.loads(tool_results[1]["content"])["error"]
        assert (
            "Unknown or unavailable tool"
            in json.loads(tool_results[2]["content"])["error"]
        )

    async def test_anonymous_adversarial_calls_do_not_accumulate_pause_state(self):
        from unittest.mock import patch

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        tool_calls = [
            ToolCall(
                id="toolu-mutate",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            ),
            ToolCall(id="toolu-unknown", name="unknown_tool", input={}),
        ]
        tool_response = MagicMock(
            text=None,
            tool_calls=tool_calls,
            raw_content=[
                {"type": "tool_use", "id": call.id, "name": call.name}
                for call in tool_calls
            ],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="No allowed action was requested.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(
            side_effect=[
                response
                for _ in range(120)
                for response in (tool_response, final_response)
            ]
        )
        agent = ChatAgent(
            llm=llm,
            store_url="http://localhost:8090",
            audit_token="service-token",
        )
        audit_response = MagicMock()
        audit_response.raise_for_status = MagicMock()
        agent._client.post = AsyncMock(return_value=audit_response)

        with patch("agents.chat.agent.execute_tool", new_callable=AsyncMock) as execute:
            for index in range(120):
                result = await agent.handle_message(
                    f"anonymous-{index}", "start a ticket", "", readonly=True
                )
                assert result == "No allowed action was requested."

        execute.assert_not_awaited()
        assert agent._client.post.await_count == 240
        assert agent._indeterminate_sessions == {}
        assert agent._session_locks == {}
        assert len(agent._sessions._sessions) == agent._sessions._max_sessions
        assert "anonymous-0" not in agent._sessions._sessions

    async def test_readonly_denial_audit_failures_are_reported_without_dispatch(self):
        from unittest.mock import patch

        from agents.chat.agent import (
            _CHAT_READONLY_AUDIT_UNAVAILABLE_MESSAGE,
            ChatAgent,
        )
        from providers.llm.base import ToolCall

        for failed_state in ("started", "rejected"):
            call = ToolCall(
                id=f"toolu-{failed_state}",
                name="start_ticket",
                input={"ticket_id": "PERF-1"},
            )
            llm_response = MagicMock(
                text=None,
                tool_calls=[call],
                raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
            )
            llm_response.usage = {}
            llm = AsyncMock()
            llm.max_tokens = 4096
            llm.timeout = 60
            llm.complete = AsyncMock(return_value=llm_response)
            agent = ChatAgent(
                llm=llm,
                store_url="http://localhost:8090",
                audit_token="service-token",
            )
            audit_states = []

            async def post(_url, **kwargs):
                state = kwargs["json"]["lifecycle"]["state"]
                audit_states.append(state)
                if state == failed_state:
                    raise RuntimeError("audit service unavailable")
                audit_response = MagicMock()
                audit_response.raise_for_status = MagicMock()
                return audit_response

            agent._client.post = AsyncMock(side_effect=post)
            with patch(
                "agents.chat.agent.execute_tool", new_callable=AsyncMock
            ) as execute:
                result = await agent.handle_message(
                    f"anonymous-{failed_state}",
                    "start PERF-1",
                    "",
                    readonly=True,
                )

            assert result == _CHAT_READONLY_AUDIT_UNAVAILABLE_MESSAGE
            assert audit_states == (
                ["started"] if failed_state == "started" else ["started", "rejected"]
            )
            execute.assert_not_awaited()
            assert not agent.is_session_indeterminate(f"anonymous-{failed_state}")

    async def test_readonly_denial_cancellation_finishes_audit_pair_without_dispatch(
        self,
    ):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-denied",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        llm_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        llm_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(return_value=llm_response)
        agent = ChatAgent(
            llm=llm,
            store_url="http://localhost:8090",
            audit_token="service-token",
        )
        audit_states = []
        terminal_started = asyncio.Event()
        finish_terminal = asyncio.Event()

        async def post(_url, **kwargs):
            state = kwargs["json"]["lifecycle"]["state"]
            audit_states.append(state)
            if state == "rejected":
                terminal_started.set()
                await finish_terminal.wait()
            audit_response = MagicMock()
            audit_response.raise_for_status = MagicMock()
            return audit_response

        agent._client.post = AsyncMock(side_effect=post)
        with patch("agents.chat.agent.execute_tool", new_callable=AsyncMock) as execute:
            task = asyncio.create_task(
                agent.handle_message(
                    "anonymous-cancel", "start PERF-1", "", readonly=True
                )
            )
            await terminal_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            finish_terminal.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        execute.assert_not_awaited()
        assert audit_states == ["started", "rejected"]
        assert not agent.is_session_indeterminate("anonymous-cancel")
        session = agent._sessions.get_or_create("anonymous-cancel")
        result_message = next(
            message["content"]
            for message in session.messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(item.get("type") == "tool_result" for item in message["content"])
        )
        assert result_message[0]["tool_use_id"] == "toolu-denied"
        assert (
            "No action was attempted"
            in json.loads(result_message[0]["content"])["error"]
        )

    async def test_anonymous_readonly_cancellations_do_not_accumulate_pause_state(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock()
        response.text = None
        response.tool_calls = [
            ToolCall(
                id="toolu-read",
                name="get_ticket",
                input={"ticket_id": "PERF-1"},
            )
        ]
        response.raw_content = [{"type": "tool_use", "id": "toolu-read"}]
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )

        async def cancelled_read(*_args, **_kwargs):
            raise asyncio.CancelledError

        with patch("agents.chat.agent.execute_tool", cancelled_read):
            for index in range(200):
                with pytest.raises(asyncio.CancelledError):
                    await agent.handle_message(
                        f"anonymous-{index}", "inspect ticket", "", readonly=True
                    )

        assert agent._indeterminate_sessions == {}
        assert len(agent._sessions._sessions) <= agent._sessions._max_sessions

        fresh_response = MagicMock(
            text="read-only chat remains available", tool_calls=[], raw_content=[]
        )
        fresh_response.usage = {}
        llm.complete.return_value = fresh_response
        assert (
            await agent.handle_message(
                "anonymous-199", "hello again", "", readonly=True
            )
            == "read-only chat remains available"
        )

    async def test_confirmation_remains_pending_until_action_finishes(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        session = agent._sessions.get_or_create("alice")
        session.messages = [
            {"role": "user", "content": "create a ticket"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc"}],
            },
            {"role": "assistant", "content": "Please confirm."},
        ]
        session.pending_action = {
            "tool": "create_ticket",
            "input": {"summary": "test"},
            "tool_call_id": "tc",
        }
        tool_started = asyncio.Event()

        async def blocked_tool(*_args, **_kwargs):
            tool_started.set()
            await asyncio.Event().wait()

        with patch("agents.chat.agent.execute_tool", blocked_tool):
            task = asyncio.create_task(agent.handle_message("alice", "yes", "token"))
            await tool_started.wait()
            assert session.pending_action is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert session.pending_action is None
        assert len(session.messages) == 2
        assert "outcome is uncertain" in session.messages[-1]["content"]

    async def test_failed_round_zero_retry_logs_sanitized_exception(self, caplog):
        from agents.chat.agent import ChatAgent

        class ProviderError(Exception):
            def __init__(self, status_code: int, detail: str) -> None:
                self.status_code = status_code
                super().__init__(detail)

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock(
            side_effect=[
                ProviderError(503, "first response echoed private chat text"),
                ProviderError(429, "retry response echoed private ticket text"),
            ]
        )
        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")
        caplog.set_level("WARNING", logger="agents.chat.agent")

        result = await agent.handle_message(
            user="alice", message="hello", auth_token="token123"
        )

        assert (
            result
            == "I'm having trouble processing your request right now. Please try again."
        )
        assert "private chat text" not in caplog.text
        assert "private ticket text" not in caplog.text
        assert "ProviderError (HTTP 503)" in caplog.text
        assert "ProviderError (HTTP 429)" in caplog.text

    async def test_simple_text_response(self):
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60

        # LLM returns text, no tool calls
        response = MagicMock()
        response.text = "Hello! How can I help?"
        response.tool_calls = []
        response.raw_content = []
        response.usage = {"input_tokens": 100, "output_tokens": 20}
        llm.complete = AsyncMock(return_value=response)

        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        response = MagicMock()
        response.raise_for_status = MagicMock()
        agent._client.post = AsyncMock(return_value=response)
        result = await agent.handle_message(
            user="alice",
            message="hello",
            auth_token="token123",
        )
        assert result == "Hello! How can I help?"
        assert agent.get_usage("alice")["input_tokens"] == 100

    async def test_confirmation_flow(self):
        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60

        # LLM returns a create_ticket tool call
        response = MagicMock()
        response.text = None
        response.tool_calls = [
            ToolCall(
                id="tc_1",
                name="create_ticket",
                input={"summary": "test", "description": "test", "custom_fields": {}},
            )
        ]
        response.raw_content = [{"type": "tool_use", "id": "tc_1"}]
        response.usage = {"input_tokens": 100, "output_tokens": 50}
        llm.complete = AsyncMock(return_value=response)

        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        audit_response = MagicMock()
        audit_response.raise_for_status = MagicMock()
        agent._client.post = AsyncMock(return_value=audit_response)

        # First call should return confirmation prompt
        result = await agent.handle_message(
            user="alice",
            message="create a test ticket",
            auth_token="token123",
        )
        assert "confirmation" in result.lower()
        assert "ready to create" in result.lower()

        # Session should have pending action
        session = agent._sessions.get_or_create("alice")
        assert session.pending_action is not None
        assert session.pending_action["tool"] == "create_ticket"
        original_root = session.pending_action["trace_context"]

        # Confirmation arrives in a separate HTTP request.  It must retain the
        # original root context and emit a real started/rejected pair instead
        # of silently dropping the user cancellation.
        assert (
            await agent.handle_message("alice", "no", "token123") == "Action cancelled."
        )
        audit_events = [
            call.kwargs["json"] for call in agent._client.post.await_args_list
        ]
        assert [event["lifecycle"]["state"] for event in audit_events] == [
            "started",
            "rejected",
        ]
        assert {event["trace_id"] for event in audit_events} == {original_root.trace_id}
        assert {event["parent_action_id"] for event in audit_events} == {
            original_root.action_id
        }

    async def test_cancel_pending_action(self):
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60

        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        response = MagicMock()
        response.raise_for_status = MagicMock()
        agent._client.post = AsyncMock(return_value=response)
        session = agent._sessions.get_or_create("alice")
        session.pending_action = {
            "tool": "create_ticket",
            "input": {"summary": "test"},
        }
        session.add_user_message("create ticket")
        session.add_assistant_message("confirm?")

        result = await agent.handle_message(
            user="alice",
            message="no",
            auth_token="token123",
        )
        assert result == "Action cancelled."
        assert session.pending_action is None
        assert [
            call.kwargs["json"]["lifecycle"]["state"]
            for call in agent._client.post.await_args_list
        ] == ["started", "rejected"]

    async def test_unrelated_reply_invalidates_pending_action_with_audit_pair(self):
        """A stale confirmation cannot disappear without a rejected lifecycle."""
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock()
        response.raise_for_status = MagicMock()
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        agent._client.post = AsyncMock(return_value=response)
        session = agent._sessions.get_or_create("alice")
        session.pending_action = {
            "tool": "create_ticket",
            "input": {"summary": "test"},
            "tool_call_id": "tc-stale",
        }
        llm_response = MagicMock(text="fresh response", tool_calls=[], raw_content=[])
        llm_response.usage = {}
        llm.complete = AsyncMock(return_value=llm_response)

        assert await agent.handle_message(
            "alice", "tell me something else", "token"
        ) == ("fresh response")
        assert session.pending_action is None
        assert [
            call.kwargs["json"]["lifecycle"]["state"]
            for call in agent._client.post.await_args_list
        ] == ["started", "rejected"]

    async def test_failed_rejection_terminal_blocks_confirmation_until_recovery(self):
        from agents.chat.agent import (
            _CHAT_AUDIT_INDETERMINATE_MESSAGE,
            ChatAgent,
        )

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock(text="fresh response", tool_calls=[], raw_content=[])
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        audit_response = MagicMock()
        audit_response.raise_for_status = MagicMock()
        agent._client.post = AsyncMock(
            side_effect=[audit_response, RuntimeError("terminal event lost")]
        )
        session = agent._sessions.get_or_create("alice")
        session.messages = [
            {"role": "user", "content": "create a ticket"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc"}],
            },
            {"role": "assistant", "content": "Please confirm."},
        ]
        session.pending_action = {
            "tool": "create_ticket",
            "input": {"summary": "must remain rejected"},
            "tool_call_id": "tc",
        }

        result = await agent.handle_message("alice", "no", "token")

        assert result == _CHAT_AUDIT_INDETERMINATE_MESSAGE
        assert session.pending_action is None
        assert agent.is_session_indeterminate("alice")
        assert [
            call.kwargs["json"]["lifecycle"]["state"]
            for call in agent._client.post.await_args_list
        ] == ["started", "rejected"]
        assert await agent.handle_message("alice", "yes", "token") == result
        llm.complete.assert_not_awaited()

        assert agent.clear_session("alice") is True
        assert await agent.handle_message("alice", "hello", "token") == (
            "fresh response"
        )
        llm.complete.assert_awaited_once()

    async def test_rejected_confirmation_stays_rejected_after_disconnect(self):
        from unittest.mock import patch

        import pytest

        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock(text="new response", tool_calls=[], raw_content=[])
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        session = agent._sessions.get_or_create("alice")
        session.messages = [
            {"role": "user", "content": "create a ticket"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc"}],
            },
            {"role": "assistant", "content": "Please confirm."},
        ]
        session.pending_action = {
            "tool": "create_ticket",
            "input": {"summary": "do not create"},
            "tool_call_id": "tc",
        }
        events = []
        terminal_started = asyncio.Event()
        finish_terminal = asyncio.Event()
        audit_response = MagicMock()
        audit_response.raise_for_status = MagicMock()

        async def post(_url, **kwargs):
            event = kwargs["json"]
            state = event["lifecycle"]["state"]
            if state == "rejected":
                terminal_started.set()
                await finish_terminal.wait()
            events.append(state)
            return audit_response

        agent._client.post = AsyncMock(side_effect=post)
        with patch("agents.chat.agent.execute_tool", new_callable=AsyncMock) as execute:
            task = asyncio.create_task(agent.handle_message("alice", "no", "token"))
            await terminal_started.wait()
            task.cancel()
            assert session.pending_action is None
            assert not task.done()

            finish_terminal.set()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert events == ["started", "rejected"]
            assert session.pending_action is None
            assert len(session.messages) == 1

            await agent.handle_message("alice", "yes", "token")
            execute.assert_not_awaited()

    async def test_session_locks_are_reclaimed_after_anonymous_churn(self):
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        response = MagicMock(text="answer", tool_calls=[], raw_content=[])
        response.usage = {}
        llm.complete = AsyncMock(return_value=response)
        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")

        for index in range(40):
            await agent.handle_message(f"anonymous-{index}", "hello", "", readonly=True)

        assert agent._session_locks == {}

    async def test_active_session_lock_is_preserved_until_request_finishes(self):
        from agents.chat.agent import _CHAT_BUSY_MESSAGE, ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm_started = asyncio.Event()
        finish_llm = asyncio.Event()
        response = MagicMock(text="answer", tool_calls=[], raw_content=[])
        response.usage = {}

        async def blocked_completion(*_args, **_kwargs):
            llm_started.set()
            await finish_llm.wait()
            return response

        llm.complete = AsyncMock(side_effect=blocked_completion)
        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")
        task = asyncio.create_task(agent.handle_message("alice", "hello", "token"))
        await llm_started.wait()

        lock = agent._session_locks["alice"]
        assert lock.locked()
        assert await agent.handle_message("alice", "another", "token") == (
            _CHAT_BUSY_MESSAGE
        )
        assert agent._session_locks["alice"] is lock

        finish_llm.set()
        assert await task == "answer"
        assert "alice" not in agent._session_locks

    async def test_ticket_context_only_first_message(self):
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60

        response = MagicMock()
        response.text = "I see the ticket."
        response.tool_calls = []
        response.raw_content = []
        response.usage = {"input_tokens": 50, "output_tokens": 10}
        llm.complete = AsyncMock(return_value=response)

        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")

        # Mock the HTTP client for ticket fetch
        ticket_response = AsyncMock()
        ticket_response.status_code = 200
        ticket_response.json = MagicMock(
            return_value={
                "id": "PERF-TEST",
                "status": "closed",
                "summary": "test ticket",
                "custom_fields": {"harness": "boot-time"},
                "comments": [],
            }
        )
        agent._client.get = AsyncMock(return_value=ticket_response)

        # First message gets context
        await agent.handle_message(
            user="alice",
            message="what happened?",
            auth_token="token123",
            ticket_context="PERF-TEST",
        )

        session = agent._sessions.get_or_create("alice")
        first_msg = session.messages[0]["content"]
        assert "[Context: viewing ticket PERF-TEST]" in first_msg

        # Second message gets identity prefix but not
        # full ticket data.
        await agent.handle_message(
            user="alice",
            message="tell me more",
            auth_token="token123",
            ticket_context="PERF-TEST",
        )
        second_user_msgs = [
            m
            for m in session.messages
            if m["role"] == "user" and "tell me more" in str(m["content"])
        ]
        assert len(second_user_msgs) == 1
        assert "[You are viewing ticket PERF-TEST]" in second_user_msgs[0]["content"]
        assert "[Context:" not in second_user_msgs[0]["content"]


# --- API endpoint tests ---


class TestChatAPI:
    def _chat_request(self, agent, *, delivery_ack=False):
        from fastapi import FastAPI
        from starlette.requests import Request

        app = FastAPI()
        app.state.chat_agent = agent
        app.state.api_token = "test-token"
        headers = [(b"authorization", b"Bearer test-token")]
        if delivery_ack:
            headers.append((b"x-chat-delivery-ack", b"1"))
        return Request(
            {
                "type": "http",
                "method": "POST",
                "headers": headers,
                "app": app,
                "state": {},
            }
        )

    async def test_history_reports_indeterminate_session_for_recovery_ui(self):
        from agents.chat.agent import ChatAgent
        from state_store.api.chat import get_history

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._indeterminate_sessions["default"] = "uncertain"

        history = await get_history(self._chat_request(agent))

        assert history.indeterminate is True

    async def test_history_reports_pending_delivery_and_active_request(self):
        from agents.chat.agent import ChatAgent
        from state_store.api.chat import get_history

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "request-A"
        lock = agent._session_locks.setdefault("default", asyncio.Lock())
        await lock.acquire()
        try:
            history = await get_history(self._chat_request(agent))
        finally:
            lock.release()

        assert history.delivery_pending is True
        assert history.request_in_flight is True
        assert history.indeterminate is False

    async def test_late_delivery_ack_only_resolves_its_matching_request(
        self, monkeypatch
    ):
        import agents.chat.agent as chat_agent_module
        from agents.chat.agent import ChatAgent

        monkeypatch.setattr(
            chat_agent_module, "_CHAT_DELIVERY_ACK_TIMEOUT_SECONDS", 0.01
        )
        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["alice"] = "request-A"
        agent.mark_response_delivery_sent("alice", "request-A")
        await asyncio.sleep(0.03)

        assert agent.is_session_indeterminate("alice")
        assert agent._pending_response_deliveries == {}
        response = await agent.handle_message("alice", "retry", "token")
        assert "before retrying" in response
        agent._llm.complete.assert_not_awaited()
        assert agent.mark_response_delivery_complete("alice", "request-B") is False
        assert agent.is_session_indeterminate("alice")
        assert agent.mark_response_delivery_complete("alice", "request-A") is True
        assert not agent.is_session_indeterminate("alice")
        assert agent.mark_response_delivery_complete("alice", "request-A") is False

    async def test_disconnected_active_mutation_becomes_indeterminate_when_it_finishes(
        self,
    ):
        from unittest.mock import patch

        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        tool_call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[tool_call],
            raw_content=[{"type": "tool_use", "id": tool_call.id}],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="Ticket started.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        completion_started = asyncio.Event()
        finish_completion = asyncio.Event()
        cancellation_seen = asyncio.Event()
        completion_calls = 0

        async def complete(**_kwargs):
            nonlocal completion_calls
            completion_calls += 1
            if completion_calls == 1:
                return tool_response
            completion_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await finish_completion.wait()
            return final_response

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=complete)
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ):
            task = asyncio.create_task(
                agent.handle_message(
                    "alice",
                    "start PERF-1",
                    "token",
                    track_response_delivery=True,
                    delivery_id="request-A",
                )
            )
            await completion_started.wait()
            task.cancel()
            agent.mark_response_delivery_interrupted("alice", "request-A")
            await cancellation_seen.wait()
            finish_completion.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert agent.is_session_indeterminate("alice")
        assert agent._pending_response_deliveries == {}
        assert "alice" not in agent._active_response_deliveries

    async def test_delivery_ack_is_bound_to_authenticated_user(self):
        from agents.chat.agent import ChatAgent
        from state_store.api.chat import (
            ChatDeliveryAckRequest,
            acknowledge_message_delivery,
        )
        from state_store.auth import Principal

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["alice"] = "request-A"
        request = self._chat_request(agent)
        request.state.principal = Principal(kind="user", username="bob", is_admin=False)

        result = await acknowledge_message_delivery(
            ChatDeliveryAckRequest(delivery_id="request-A"), request
        )

        assert result == {"acknowledged": False}
        assert agent._pending_response_deliveries == {"alice": "request-A"}

    async def test_early_ack_is_settled_after_asgi_body_send_returns(self):
        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "request-A"
        response = chat_api._ChatDeliveryJSONResponse(
            {"response": "complete"}, agent, "default", "request-A"
        )
        body_send_started = asyncio.Event()
        release_body_send = asyncio.Event()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                body_send_started.set()
                await release_body_send.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        response_task = asyncio.create_task(response(scope, receive, send))
        await body_send_started.wait()

        early_ack = await chat_api.acknowledge_message_delivery(
            chat_api.ChatDeliveryAckRequest(delivery_id="request-A"),
            self._chat_request(agent),
        )
        assert early_ack == {"acknowledged": False}
        assert agent._early_response_delivery_acks == {"default": "request-A"}
        assert agent._pending_response_deliveries == {"default": "request-A"}

        release_body_send.set()
        await response_task

        assert agent._pending_response_deliveries == {}
        assert agent._early_response_delivery_acks == {}
        assert agent._delivery_timeouts == {}

    async def test_stalled_asgi_send_is_bounded_and_late_send_needs_ack(
        self, monkeypatch
    ):
        import agents.chat.agent as chat_agent_module
        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent

        monkeypatch.setattr(
            chat_agent_module, "_CHAT_DELIVERY_SEND_TIMEOUT_SECONDS", 0.01
        )
        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "request-A"
        agent._arm_response_delivery_send_timeout("default", "request-A")
        response = chat_api._ChatDeliveryJSONResponse(
            {"response": "complete"}, agent, "default", "request-A"
        )
        body_send_started = asyncio.Event()
        release_body_send = asyncio.Event()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                body_send_started.set()
                await release_body_send.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        response_task = asyncio.create_task(response(scope, receive, send))
        await body_send_started.wait()
        await asyncio.sleep(0.03)

        assert agent.is_session_indeterminate("default")
        assert agent._pending_response_deliveries == {}
        assert agent._delivery_send_timeouts == {}

        release_body_send.set()
        await response_task

        # Finishing a blocked send after the watchdog cannot silently release
        # the mutation guard; only the matching explicit receipt can do that.
        assert agent.is_session_indeterminate("default")
        assert agent.mark_response_delivery_complete("default", "request-A")
        assert not agent.is_session_indeterminate("default")
        assert agent._delivery_timeouts == {}
        assert agent._sent_response_deliveries == {}

    async def test_early_ack_during_stalled_send_settles_only_after_send_returns(
        self, monkeypatch
    ):
        import agents.chat.agent as chat_agent_module
        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent

        monkeypatch.setattr(
            chat_agent_module, "_CHAT_DELIVERY_SEND_TIMEOUT_SECONDS", 0.01
        )
        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "request-A"
        agent._arm_response_delivery_send_timeout("default", "request-A")
        response = chat_api._ChatDeliveryJSONResponse(
            {"response": "complete"}, agent, "default", "request-A"
        )
        body_send_started = asyncio.Event()
        release_body_send = asyncio.Event()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                body_send_started.set()
                await release_body_send.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        response_task = asyncio.create_task(response(scope, receive, send))
        await body_send_started.wait()

        early_ack = await chat_api.acknowledge_message_delivery(
            chat_api.ChatDeliveryAckRequest(delivery_id="request-A"),
            self._chat_request(agent),
        )
        assert early_ack == {"acknowledged": False}
        assert agent._early_response_delivery_acks == {"default": "request-A"}

        await asyncio.sleep(0.03)
        assert agent.is_session_indeterminate("default")
        release_body_send.set()
        await response_task

        assert not agent.is_session_indeterminate("default")
        assert agent._pending_response_deliveries == {}
        assert agent._early_response_delivery_acks == {}

    async def test_cancelled_stalled_asgi_send_keeps_timed_out_guard(self, monkeypatch):
        import agents.chat.agent as chat_agent_module
        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent

        monkeypatch.setattr(
            chat_agent_module, "_CHAT_DELIVERY_SEND_TIMEOUT_SECONDS", 0.01
        )
        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "request-A"
        agent._arm_response_delivery_send_timeout("default", "request-A")
        response = chat_api._ChatDeliveryJSONResponse(
            {"response": "complete"}, agent, "default", "request-A"
        )
        body_send_started = asyncio.Event()
        never_release = asyncio.Event()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                body_send_started.set()
                await never_release.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        response_task = asyncio.create_task(response(scope, receive, send))
        await body_send_started.wait()
        await asyncio.sleep(0.03)
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task

        assert agent.is_session_indeterminate("default")
        assert agent._pending_response_deliveries == {}
        assert agent._delivery_send_timeouts == {}

    async def test_clear_session_endpoint_reports_in_flight_conflict(self):
        import pytest
        from fastapi import HTTPException

        from agents.chat.agent import ChatAgent
        from state_store.api.chat import clear_session

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        lock = agent._session_locks.setdefault("default", asyncio.Lock())
        await lock.acquire()
        try:
            with pytest.raises(HTTPException) as exc:
                await clear_session(self._chat_request(agent))
            assert exc.value.status_code == 409
        finally:
            lock.release()

    @pytest.mark.parametrize("response_kind", ["json", "stream"])
    @pytest.mark.parametrize("disconnect", [False, True])
    async def test_busy_response_cannot_finalize_another_request_delivery(
        self, response_kind, disconnect
    ):
        import pytest
        from starlette.requests import ClientDisconnect

        from agents.chat.agent import ChatAgent
        from state_store.api.chat import (
            _ChatDeliveryJSONResponse,
            _ChatStreamingResponse,
        )

        agent = ChatAgent(llm=AsyncMock(), store_url="http://localhost:8090")
        agent._pending_response_deliveries["default"] = "mutation-request-A"
        if response_kind == "json":
            response = _ChatDeliveryJSONResponse(
                {"response": "busy"},
                agent,
                "default",
                "busy-request-B",
            )
        else:
            response = _ChatStreamingResponse(
                iter(["data: busy\n\n"]),
                media_type="text/event-stream",
                agent=agent,
                user="default",
                delivery_id="busy-request-B",
            )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if (
                disconnect
                and message["type"] == "http.response.body"
                and message.get("body")
            ):
                raise OSError("request B disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        if disconnect:
            expected_exception = (
                OSError if response_kind == "json" else ClientDisconnect
            )
            with pytest.raises(expected_exception):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)

        assert agent._pending_response_deliveries == {"default": "mutation-request-A"}
        assert not agent.is_session_indeterminate("default")

    async def test_send_message_no_agent(self):
        """Chat returns 503 when agent not initialized."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from state_store.api.chat import router

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        # No chat_agent on app.state

        client = TestClient(app)
        r = client.post(
            "/api/v1/chat/message",
            json={"message": "hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 503

    async def test_send_message_no_auth(self):
        """Chat returns 401 without auth token."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from state_store.api.chat import router

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.state.chat_agent = MagicMock()

        client = TestClient(app)
        r = client.post(
            "/api/v1/chat/message",
            json={"message": "hello"},
        )
        assert r.status_code == 401

    async def test_delivery_ack_is_opt_in_for_json_and_sse_clients(self):
        import state_store.api.chat as chat_api

        class RecordingAgent:
            def __init__(self):
                self.kwargs = None
                self.sent = []
                self.acks = []

            async def handle_message(self, **kwargs):
                self.kwargs = kwargs
                return "answer"

            def get_usage(self, _user):
                return {}

            def is_session_indeterminate(self, _user):
                return False

            def is_response_delivery_pending(self, _user):
                return False

            def mark_response_delivery_sent(self, user, delivery_id):
                self.sent.append((user, delivery_id))

            def mark_response_delivery_interrupted(self, user, delivery_id):
                raise AssertionError((user, delivery_id))

            def mark_response_delivery_complete(self, user, delivery_id):
                self.acks.append((user, delivery_id))
                return True

        async def run_json(delivery_ack):
            agent = RecordingAgent()
            response = await chat_api.send_message(
                chat_api.ChatMessageRequest(message="hello"),
                self._chat_request(agent, delivery_ack=delivery_ack),
            )
            payload = json.loads(response.body)
            assert agent.kwargs["track_response_delivery"] is delivery_ack
            if delivery_ack:
                assert payload["delivery_id"]
                assert payload["indeterminate"] is False
                assert payload["delivery_pending"] is False
            else:
                assert set(payload) == {"response", "usage"}
                assert agent.kwargs["delivery_id"] is None
            return agent, response

        async def run_stream(delivery_ack):
            agent = RecordingAgent()
            response = await chat_api.send_message_stream(
                chat_api.ChatMessageRequest(message="hello"),
                self._chat_request(agent, delivery_ack=delivery_ack),
            )
            sent_messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent_messages.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "method": "POST",
                "path": "/api/v1/chat/message/stream",
                "headers": [],
            }
            await response(scope, receive, send)
            events = [
                json.loads(line[6:])
                for message in sent_messages
                if message["type"] == "http.response.body"
                for line in message["body"].decode().splitlines()
                if line.startswith("data: ")
            ]
            done = next(event for event in events if event.get("done"))
            assert agent.kwargs["track_response_delivery"] is delivery_ack
            if delivery_ack:
                assert done["delivery_id"]
                assert done["indeterminate"] is False
                assert done["delivery_pending"] is False
                assert agent.sent == [("default", done["delivery_id"])]
            else:
                assert set(done) == {"done", "usage"}
                assert agent.kwargs["delivery_id"] is None

        legacy_agent, _ = await run_json(False)
        assert legacy_agent.sent == []
        negotiated_agent, negotiated_response = await run_json(True)
        delivery_id = json.loads(negotiated_response.body)["delivery_id"]
        ack = await chat_api.acknowledge_message_delivery(
            chat_api.ChatDeliveryAckRequest(delivery_id=delivery_id),
            self._chat_request(negotiated_agent, delivery_ack=True),
        )
        assert ack == {"acknowledged": True}
        assert negotiated_agent.acks == [("default", delivery_id)]

        await run_stream(False)
        await run_stream(True)

    async def test_stream_disconnect_cancels_chat_task(self):
        from state_store.api.chat import ChatMessageRequest, send_message_stream

        class BlockingAgent:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()
                self.finished = asyncio.Event()

            async def handle_message(self, **_kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
                finally:
                    self.finished.set()

        agent = BlockingAgent()
        response = await send_message_stream(
            ChatMessageRequest(message="hello"), self._chat_request(agent)
        )
        stream = response.body_iterator

        assert await anext(stream) == ": heartbeat\n\n"
        await asyncio.wait_for(agent.started.wait(), timeout=0.2)
        await asyncio.wait_for(stream.aclose(), timeout=0.2)

        assert agent.cancelled.is_set()
        assert agent.finished.is_set()

    async def test_stream_disconnect_bounds_cancellation_cleanup(self, monkeypatch):
        import state_store.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_CHAT_TASK_CLEANUP_TIMEOUT_SECONDS", 0.01)

        class SlowCleanupAgent:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()
                self.release = asyncio.Event()
                self.task = None

            async def handle_message(self, **_kwargs):
                self.task = asyncio.current_task()
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    await self.release.wait()

        agent = SlowCleanupAgent()
        response = await chat_api.send_message_stream(
            chat_api.ChatMessageRequest(message="hello"),
            self._chat_request(agent),
        )
        stream = response.body_iterator

        assert await anext(stream) == ": heartbeat\n\n"
        await asyncio.wait_for(agent.started.wait(), timeout=0.2)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        await asyncio.wait_for(stream.aclose(), timeout=0.2)

        assert agent.cancelled.is_set()
        assert loop.time() - started_at < 0.2
        assert agent.task is not None
        assert not agent.task.done()
        assert agent.task in chat_api._CHAT_TASKS_AWAITING_CLEANUP
        agent.release.set()
        completed, _ = await asyncio.wait({agent.task}, timeout=0.2)
        assert completed == {agent.task}
        await asyncio.sleep(0)
        assert agent.task not in chat_api._CHAT_TASKS_AWAITING_CLEANUP

    async def test_asgi_24_send_failure_closes_chat_stream(self):
        import pytest
        from starlette.requests import ClientDisconnect

        from state_store.api.chat import ChatMessageRequest, send_message_stream

        class BlockingAgent:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def handle_message(self, **_kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        agent = BlockingAgent()
        response = await send_message_stream(
            ChatMessageRequest(message="hello"), self._chat_request(agent)
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                await agent.started.wait()
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message/stream",
            "headers": [],
        }
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

        assert agent.started.is_set()
        assert agent.cancelled.is_set()

    @pytest.mark.parametrize("failure_point", ["heartbeat", "text", "done", "final"])
    async def test_asgi_send_failure_pauses_after_completed_mutation(
        self, failure_point
    ):
        from unittest.mock import patch

        import pytest
        from starlette.requests import ClientDisconnect

        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="Ticket started.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=[tool_response, final_response])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        request_finished = asyncio.Event()
        original_handle_message = agent.handle_message

        async def tracked_handle_message(**kwargs):
            result = await original_handle_message(**kwargs)
            request_finished.set()
            return result

        agent.handle_message = tracked_handle_message
        response = await chat_api.send_message_stream(
            chat_api.ChatMessageRequest(message="start PERF-1"),
            self._chat_request(agent, delivery_ack=True),
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] != "http.response.body":
                return
            body = message["body"]
            should_fail = (
                (failure_point == "heartbeat" and body.startswith(b": heartbeat"))
                or (failure_point == "text" and b'"text"' in body)
                or (failure_point == "done" and b'"done": true' in body)
                or (failure_point == "final" and not body and not message["more_body"])
            )
            if should_fail:
                if failure_point == "heartbeat":
                    await request_finished.wait()
                raise OSError(f"client disconnected at {failure_point}")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message/stream",
            "headers": [],
        }
        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ):
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)

        assert agent.is_session_indeterminate("default")
        assert not agent._pending_response_deliveries
        session = agent._sessions.get_or_create("default")
        assert "Ticket started." in session.messages[-2]["content"]
        assert "may have completed" in session.messages[-1]["content"]

    async def test_json_response_send_failure_pauses_after_completed_mutation(self):
        from unittest.mock import patch

        import pytest

        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="Ticket started.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=[tool_response, final_response])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ):
            response = await chat_api.send_message(
                chat_api.ChatMessageRequest(message="start PERF-1"),
                self._chat_request(agent, delivery_ack=True),
            )
        assert "default" in agent._pending_response_deliveries

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                raise OSError("client disconnected before JSON delivery")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        with pytest.raises(OSError, match="before JSON delivery"):
            await response(scope, receive, send)

        assert agent.is_session_indeterminate("default")
        assert not agent._pending_response_deliveries

    @pytest.mark.parametrize("delivery_ack", [False, True])
    async def test_json_mutation_delivery_ack_is_opt_in(self, delivery_ack):
        from unittest.mock import patch

        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        call = ToolCall(
            id="toolu-start",
            name="start_ticket",
            input={"ticket_id": "PERF-1"},
        )
        tool_response = MagicMock(
            text=None,
            tool_calls=[call],
            raw_content=[{"type": "tool_use", "id": call.id, "name": call.name}],
        )
        tool_response.usage = {}
        final_response = MagicMock(
            text="Ticket started.", tool_calls=[], raw_content=[]
        )
        final_response.usage = {}
        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm._model = ""
        llm.complete = AsyncMock(side_effect=[tool_response, final_response])
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        with patch(
            "agents.chat.agent.execute_tool",
            new_callable=AsyncMock,
            return_value='{"id":"PERF-1","status":"running"}',
        ):
            response = await chat_api.send_message(
                chat_api.ChatMessageRequest(message="start PERF-1"),
                self._chat_request(agent, delivery_ack=delivery_ack),
            )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/v1/chat/message",
            "headers": [],
        }
        await response(scope, receive, send)

        response_body = json.loads(response.body)
        if not delivery_ack:
            assert set(response_body) == {"response", "usage"}
            assert agent._pending_response_deliveries == {}
            assert not agent.is_session_indeterminate("default")
            return

        delivery_id = response_body["delivery_id"]
        assert response_body["delivery_pending"] is True
        assert response_body["indeterminate"] is False
        assert agent._pending_response_deliveries == {"default": delivery_id}
        assert not agent.is_session_indeterminate("default")

        from state_store.api.chat import (
            ChatDeliveryAckRequest,
            acknowledge_message_delivery,
        )

        wrong_ack = await acknowledge_message_delivery(
            ChatDeliveryAckRequest(delivery_id="another-request"),
            self._chat_request(agent),
        )
        assert wrong_ack == {"acknowledged": False}
        assert agent._pending_response_deliveries == {"default": delivery_id}

        ack = await acknowledge_message_delivery(
            ChatDeliveryAckRequest(delivery_id=delivery_id),
            self._chat_request(agent),
        )
        assert ack == {"acknowledged": True}
        assert agent._pending_response_deliveries == {}
        assert not agent.is_session_indeterminate("default")

        duplicate_ack = await acknowledge_message_delivery(
            ChatDeliveryAckRequest(delivery_id=delivery_id),
            self._chat_request(agent),
        )
        assert duplicate_ack == {"acknowledged": False}

    async def test_asgi_20_disconnect_cancels_chat_stream(self):
        from state_store.api.chat import ChatMessageRequest, send_message_stream

        class BlockingAgent:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()
                self.finished = asyncio.Event()

            async def handle_message(self, **_kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
                finally:
                    self.finished.set()

        agent = BlockingAgent()
        response = await send_message_stream(
            ChatMessageRequest(message="hello"), self._chat_request(agent)
        )

        async def receive():
            await agent.started.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "method": "POST",
            "path": "/api/v1/chat/message/stream",
            "headers": [],
        }
        await asyncio.wait_for(response(scope, receive, send), timeout=0.5)

        assert agent.cancelled.is_set()
        assert agent.finished.is_set()

    async def test_stream_cleanup_does_not_interrupt_delayed_audit_terminal(
        self, monkeypatch
    ):
        from unittest.mock import patch

        import pytest

        import state_store.api.chat as chat_api
        from agents.chat.agent import ChatAgent
        from providers.llm.base import ToolCall

        monkeypatch.setattr(chat_api, "_CHAT_TASK_CLEANUP_TIMEOUT_SECONDS", 0.01)
        llm = MagicMock()
        llm.max_tokens = 4096
        llm.timeout = 60
        llm.complete = AsyncMock()
        llm_response = MagicMock()
        llm_response.text = None
        llm_response.tool_calls = [
            ToolCall(
                id="toolu-read",
                name="get_ticket",
                input={"ticket_id": "PERF-1"},
            )
        ]
        llm_response.raw_content = [{"type": "tool_use", "id": "toolu-read"}]
        llm_response.usage = {}
        llm.complete.return_value = llm_response
        agent = ChatAgent(
            llm=llm, store_url="http://localhost:8090", audit_token="service-token"
        )
        tool_started = asyncio.Event()
        terminal_started = asyncio.Event()
        finish_terminal = asyncio.Event()
        events = []
        worker_task = None

        async def post(_url, **kwargs):
            state = kwargs["json"]["lifecycle"]["state"]
            if state == "cancelled":
                terminal_started.set()
                await finish_terminal.wait()
            events.append(state)
            response = MagicMock()
            response.raise_for_status = MagicMock()
            return response

        async def blocked_dispatch(*_args, **_kwargs):
            nonlocal worker_task
            worker_task = asyncio.current_task()
            tool_started.set()
            await asyncio.Event().wait()

        agent._client.post = post
        response = await chat_api.send_message_stream(
            chat_api.ChatMessageRequest(message="inspect ticket"),
            self._chat_request(agent),
        )
        stream = response.body_iterator

        with patch("agents.chat.tools._dispatch_tool", blocked_dispatch):
            assert await anext(stream) == ": heartbeat\n\n"
            await tool_started.wait()
            await asyncio.wait_for(stream.aclose(), timeout=0.2)
            await asyncio.wait_for(terminal_started.wait(), timeout=0.2)

            assert worker_task is not None
            assert worker_task in chat_api._CHAT_TASKS_AWAITING_CLEANUP
            assert events == ["started"]

            finish_terminal.set()
            with pytest.raises(asyncio.CancelledError):
                await worker_task

        assert events == ["started", "cancelled"]
        session = agent._sessions.get_or_create("default")
        assert not agent.is_session_indeterminate("default")
        assert "read-only lookup was interrupted" in session.messages[-1]["content"]
        await asyncio.sleep(0)
        assert worker_task not in chat_api._CHAT_TASKS_AWAITING_CLEANUP


class TestBenchmarkCatalog:
    """Tests for the shared benchmark catalog helpers."""

    async def test_includes_standalone(self):
        from providers.skills.catalog import STANDALONE_BENCHMARKS, benchmark_entry

        entries = [benchmark_entry(s) for s in STANDALONE_BENCHMARKS]
        names = [e["name"] for e in entries]
        assert "boot-time" in names

    async def test_catalog_filters_harness(self):
        from providers.skills.catalog import (
            list_benchmark_catalog,
        )

        class StubProvider:
            def list_harnesses(self):
                return []

            def get_provider(self, harness):
                return None

        entries, _ = await list_benchmark_catalog(StubProvider())
        boot = [e for e in entries if e["harness"] == "boot-time"]
        assert len(boot) == 1
        assert boot[0]["name"] == "boot-time"

    async def test_get_catalog_benchmark_standalone(self):
        from providers.skills.catalog import get_catalog_benchmark

        class StubProvider:
            async def get_benchmark(self, name):
                return None

        result = await get_catalog_benchmark(StubProvider(), "boot-time")
        assert result is not None
        assert result["harness"] == "boot-time"

    async def test_get_catalog_benchmark_not_found(self):
        from providers.skills.catalog import get_catalog_benchmark

        class StubProvider:
            async def get_benchmark(self, name):
                return None

        result = await get_catalog_benchmark(StubProvider(), "nonexistent")
        assert result is None


class TestSearchMetadataFilters:
    """Tests for owner, created_by, harness, board_type, since filters."""

    def _mock_client(self, tickets):
        client = AsyncMock()
        response = AsyncMock()
        response.json = MagicMock(return_value=tickets)
        response.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=response)
        return client

    async def test_owner_filter(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "owners": ["mcurrier"],
                },
                {
                    "id": "PERF-2",
                    "summary": "b",
                    "status": "closed",
                    "owners": ["dustinblack"],
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"owner": "mcurrier"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"

    async def test_created_by_filter(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "created_by": "mcurrier",
                },
                {
                    "id": "PERF-2",
                    "summary": "b",
                    "status": "closed",
                    "created_by": "dustinblack",
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"created_by": "mcurrier"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["created_by"] == "mcurrier"

    async def test_harness_filter(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {"directives": {"harness": "boot-time"}},
                },
                {
                    "id": "PERF-2",
                    "summary": "b",
                    "status": "closed",
                    "custom_fields": {"directives": {"harness": "uperf"}},
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"harness": "boot-time"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["harness"] == "boot-time"

    async def test_harness_alias_filter_matches_canonical_name(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "Arcaflow alias",
                    "status": "closed",
                    "custom_fields": {
                        "directives": {"harness": "arcaflow"},
                    },
                },
                {
                    "id": "PERF-2",
                    "summary": "Other harness",
                    "status": "closed",
                    "custom_fields": {
                        "directives": {"harness": "uperf"},
                    },
                },
            ]
        )

        result = await execute_tool(
            "search_tickets",
            {"harness": "arcaflow-plugins"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )

        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"
        assert parsed["tickets"][0]["harness"] == "arcaflow"

    async def test_board_type_filter(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {
                        "directives": {"board_selector": "board-type=nxp-s32g-vnp-rdb3"}
                    },
                },
                {
                    "id": "PERF-2",
                    "summary": "b",
                    "status": "closed",
                    "custom_fields": {
                        "directives": {"board_selector": "board-type=qc8775"}
                    },
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"board_type": "s32g"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert "s32g" in parsed["tickets"][0]["board_type"]

    async def test_board_type_fallback_to_top_level(self):
        """board_selector at top-level custom_fields, not in directives."""
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {
                        "board_selector": "board-type=qc8775",
                        "directives": {},
                    },
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"board_type": "qc8775"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1

    async def test_since_filter(self):
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "created_at": "2026-09-20T10:00:00Z",
                },
                {
                    "id": "PERF-2",
                    "summary": "b",
                    "status": "closed",
                    "created_at": "2026-09-15T10:00:00Z",
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"since": "2026-09-18"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"

    async def test_null_harness_does_not_crash(self):
        """Explicit null in directives should not crash."""
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {"directives": {"harness": None}},
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"harness": "boot-time"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 0

    async def test_null_board_selector_does_not_crash(self):
        """Explicit null board_selector should not crash."""
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {"directives": {"board_selector": None}},
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {"board_type": "qc8775"},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 0

    async def test_null_directives_does_not_crash_while_building_output(self):
        """Explicit null directives should be treated as empty metadata."""
        client = self._mock_client(
            [
                {
                    "id": "PERF-1",
                    "summary": "a",
                    "status": "closed",
                    "custom_fields": {"directives": None},
                },
            ]
        )
        result = await execute_tool(
            "search_tickets",
            {},
            client,
            "http://localhost:8090",
            "token",
            audit=_audit(client),
        )
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["tickets"][0]["id"] == "PERF-1"
        assert "harness" not in parsed["tickets"][0]
        assert "board_type" not in parsed["tickets"][0]

    async def test_malformed_metadata_is_ignored_in_filters_and_output(self):
        """Ignore non-mapping containers and non-string metadata values."""
        tickets = [
            {
                "id": "PERF-1",
                "summary": "a",
                "status": "closed",
                "custom_fields": ["invalid"],
            },
            {
                "id": "PERF-2",
                "summary": "b",
                "status": "closed",
                "custom_fields": {"directives": ["invalid"]},
            },
            {
                "id": "PERF-3",
                "summary": "c",
                "status": "closed",
                "custom_fields": {
                    "directives": {
                        "harness": {"value": "boot-time"},
                        "board_selector": ["board-type=qc8775"],
                    }
                },
            },
        ]

        async def search(params):
            client = self._mock_client(tickets)
            result = await execute_tool(
                "search_tickets",
                params,
                client,
                "http://localhost:8090",
                "token",
                audit=_audit(client),
            )
            return json.loads(result)

        parsed = await search({})
        assert parsed["count"] == 3
        assert all(
            "harness" not in ticket and "board_type" not in ticket
            for ticket in parsed["tickets"]
        )
        assert (await search({"harness": "boot-time"}))["count"] == 0
        assert (await search({"board_type": "qc8775"}))["count"] == 0
