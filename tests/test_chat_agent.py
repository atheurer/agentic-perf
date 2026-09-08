"""Tests for the chat agent."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from agents.chat.agent import ChatSession, ChatSessionStore
from agents.chat.tools import CHAT_TOOLS, execute_tool

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

        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")
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

        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")

        # First call should return confirmation prompt
        result = await agent.handle_message(
            user="alice",
            message="create a test ticket",
            auth_token="token123",
        )
        assert "confirmation" in result.lower()
        assert "create_ticket" in result

        # Session should have pending action
        session = agent._sessions.get_or_create("alice")
        assert session.pending_action is not None
        assert session.pending_action["tool"] == "create_ticket"

    async def test_cancel_pending_action(self):
        from agents.chat.agent import ChatAgent

        llm = AsyncMock()
        llm.max_tokens = 4096
        llm.timeout = 60

        agent = ChatAgent(llm=llm, store_url="http://localhost:8090")
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
