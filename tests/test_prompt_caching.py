"""Tests for prompt caching on direct vs Vertex backends (#452).

Verifies that cache_control is sent correctly for each backend:
direct uses top-level automatic caching, Vertex uses block-level
caching on the system prompt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from providers.llm.claude import ClaudeLLMProvider


def _make_provider(backend: str = "direct"):
    """Create a ClaudeLLMProvider with mocked client, bypassing __init__."""
    provider = ClaudeLLMProvider.__new__(ClaudeLLMProvider)
    provider._model = "claude-sonnet-4-6"
    provider._backend = backend
    provider.default_timeout = None
    provider.reasoning_effort = None
    provider.max_tokens = None

    mock_response = MagicMock()
    mock_response.content = []
    mock_response.stop_reason = "end_turn"
    mock_response.usage = MagicMock()
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_response.usage.cache_read_input_tokens = 80
    mock_response.usage.cache_creation_input_tokens = 20

    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_stream_cm
    mock_stream_cm.__exit__.return_value = False
    mock_stream_cm.until_done = MagicMock()
    mock_stream_cm.get_final_message = MagicMock(
        return_value=mock_response,
    )

    mock_client = MagicMock()
    mock_client.messages.stream = MagicMock(
        return_value=mock_stream_cm,
    )
    provider._client = mock_client
    return provider, mock_client, mock_response


class TestDirectBackendCaching:
    @pytest.mark.asyncio
    async def test_has_top_level_cache_control(self):
        """Direct backend should use top-level cache_control (automatic)."""
        provider, client, _ = _make_provider("direct")
        await provider.complete(
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_system_is_plain_string(self):
        """Direct backend should pass system as a plain string."""
        provider, client, _ = _make_provider("direct")
        await provider.complete(
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert isinstance(kwargs["system"], str)
        assert kwargs["system"] == "You are a helpful assistant."


class TestVertexBackendCaching:
    @pytest.mark.asyncio
    async def test_no_top_level_cache_control(self):
        """Vertex backend should NOT have top-level cache_control."""
        provider, client, _ = _make_provider("vertex")
        await provider.complete(
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert "cache_control" not in kwargs

    @pytest.mark.asyncio
    async def test_has_block_level_cache_control(self):
        """Vertex backend should use block-level caching on system."""
        provider, client, _ = _make_provider("vertex")
        await provider.complete(
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        system = kwargs["system"]
        assert isinstance(system, list)
        assert len(system) == 1
        block = system[0]
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_system_block_preserves_prompt_text(self):
        """The text content of the system block should match the input."""
        prompt = "You are a performance analysis expert."
        provider, client, _ = _make_provider("vertex")
        await provider.complete(
            system_prompt=prompt,
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["system"][0]["text"] == prompt

    @pytest.mark.asyncio
    async def test_empty_system_prompt_skips_block(self):
        """Vertex with empty system_prompt should not wrap it in a block."""
        provider, client, _ = _make_provider("vertex")
        await provider.complete(
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert "cache_control" not in kwargs
        assert kwargs["system"] == ""


class TestCacheTokenParsing:
    @pytest.mark.asyncio
    async def test_parse_response_surfaces_cache_tokens(self):
        """_parse_response should include both cache token fields."""
        provider, _, mock_resp = _make_provider("direct")
        response = provider._parse_response(mock_resp)
        assert response.usage["cache_read_input_tokens"] == 80
        assert response.usage["cache_creation_input_tokens"] == 20
