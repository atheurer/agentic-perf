"""Tests for the chat agent."""

from __future__ import annotations

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


# --- Tool execution tests ---


class TestToolExecution:
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
                {"id": "PERF-1", "summary": "a", "status": "closed"},
                {"id": "PERF-2", "summary": "b", "status": "running"},
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

        # Second message should NOT repeat context
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
        assert "[Context:" not in second_user_msgs[0]["content"]


# --- API endpoint tests ---


class TestChatAPI:
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


class TestListAvailableBenchmarks:
    """Tests for the list_available_benchmarks chat tool."""

    @pytest.mark.asyncio
    async def test_lists_benchmarks(self):
        from unittest.mock import patch

        from agents.chat.tools import _list_available_benchmarks
        from providers.skills.base import BenchmarkSuite

        mock_provider = AsyncMock()
        mock_provider.list_benchmarks = AsyncMock(
            return_value=[
                BenchmarkSuite(
                    name="uperf",
                    description="Network throughput",
                    harness="crucible",
                    endpoint_types=["remotehosts"],
                ),
                BenchmarkSuite(
                    name="fio",
                    description="Storage I/O",
                    harness="crucible",
                ),
                BenchmarkSuite(
                    name="stress-ng",
                    description="CPU stress",
                    harness="arcaflow-plugins",
                ),
            ],
        )

        with patch(
            "agents.chat.tools._get_skill_provider",
            return_value=mock_provider,
        ):
            result = json.loads(await _list_available_benchmarks({}))
            assert result["total"] == 3
            assert "crucible" in result["harnesses"]
            assert "arcaflow-plugins" in result["harnesses"]

    @pytest.mark.asyncio
    async def test_filters_by_harness(self):
        from unittest.mock import patch

        from agents.chat.tools import _list_available_benchmarks
        from providers.skills.base import BenchmarkSuite

        mock_provider = AsyncMock()
        mock_provider.list_benchmarks = AsyncMock(
            return_value=[
                BenchmarkSuite(
                    name="uperf",
                    description="Network",
                    harness="crucible",
                ),
                BenchmarkSuite(
                    name="fio",
                    description="Storage",
                    harness="arcaflow-plugins",
                ),
            ],
        )

        with patch(
            "agents.chat.tools._get_skill_provider",
            return_value=mock_provider,
        ):
            result = json.loads(
                await _list_available_benchmarks({"harness": "crucible"})
            )
            assert result["total"] == 1
            assert result["benchmarks"]["crucible"][0]["name"] == "uperf"

    @pytest.mark.asyncio
    async def test_filters_by_query(self):
        from unittest.mock import patch

        from agents.chat.tools import _list_available_benchmarks
        from providers.skills.base import BenchmarkSuite

        mock_provider = AsyncMock()
        mock_provider.list_benchmarks = AsyncMock(
            return_value=[
                BenchmarkSuite(
                    name="uperf",
                    description="Network throughput",
                    harness="crucible",
                ),
                BenchmarkSuite(
                    name="fio",
                    description="Storage I/O",
                    harness="crucible",
                ),
            ],
        )

        with patch(
            "agents.chat.tools._get_skill_provider",
            return_value=mock_provider,
        ):
            result = json.loads(await _list_available_benchmarks({"query": "storage"}))
            assert result["total"] == 1
            assert result["benchmarks"]["crucible"][0]["name"] == "fio"

    @pytest.mark.asyncio
    async def test_no_provider(self):
        from unittest.mock import patch

        from agents.chat.tools import _list_available_benchmarks

        with patch(
            "agents.chat.tools._get_skill_provider",
            return_value=None,
        ):
            result = json.loads(await _list_available_benchmarks({}))
            assert "error" in result

    def test_tool_in_readonly(self):
        from agents.chat.tools import READONLY_TOOLS

        assert "list_available_benchmarks" in READONLY_TOOLS
