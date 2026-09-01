"""Tests for LLM reasoning effort support.

Covers:
- Base class reasoning_effort attribute
- Claude provider: thinking + output_config kwargs
- OpenAI provider: reasoning_effort kwarg
- Gemini provider: thinking_config in GenerateContentConfig
- OrchestratorConfig loading from env and config file
- Factory wiring: per-agent effort overrides global default
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.llm.mock import MockLLMProvider


class TestReasoningEffortAttribute:
    """Test the base class reasoning_effort instance variable."""

    def test_defaults_to_none(self):
        provider = MockLLMProvider()
        assert provider.reasoning_effort is None

    def test_can_be_set(self):
        provider = MockLLMProvider()
        provider.reasoning_effort = "high"
        assert provider.reasoning_effort == "high"

    def test_accepts_provider_specific_values(self):
        provider = MockLLMProvider()
        provider.reasoning_effort = "max"
        assert provider.reasoning_effort == "max"


class TestClaudeReasoningEffort:
    """Test Claude provider passes thinking and output_config kwargs."""

    def _make_provider(self, effort: str | None = None):
        from providers.llm.claude import ClaudeLLMProvider

        provider = ClaudeLLMProvider.__new__(ClaudeLLMProvider)
        provider._model = "claude-sonnet-4-6"
        provider.default_timeout = None
        provider.reasoning_effort = effort

        mock_response = MagicMock()
        mock_response.content = []
        mock_response.stop_reason = "end_turn"
        mock_response.usage = None
        mock_stream_cm = MagicMock()
        mock_stream_cm.__enter__ = MagicMock(return_value=mock_stream_cm)
        mock_stream_cm.__exit__ = MagicMock(return_value=False)
        mock_stream_cm.until_done = MagicMock()
        mock_stream_cm.get_final_message = MagicMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream_cm)
        provider._client = mock_client
        return provider, mock_client

    @pytest.mark.asyncio
    async def test_effort_adds_thinking_and_output_config(self):
        provider, client = self._make_provider("high")
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_no_effort_omits_thinking_params(self):
        provider, client = self._make_provider(None)
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert "thinking" not in kwargs
        assert "output_config" not in kwargs

    @pytest.mark.asyncio
    async def test_provider_specific_value_passes_through(self):
        provider, client = self._make_provider("max")
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.messages.stream.call_args[1]
        assert kwargs["output_config"] == {"effort": "max"}


class TestOpenAIReasoningEffort:
    """Test OpenAI provider passes reasoning_effort kwarg."""

    def _make_provider(self, effort: str | None = None):
        from providers.llm.openai_compat import OpenAICompatLLMProvider

        provider = OpenAICompatLLMProvider.__new__(OpenAICompatLLMProvider)
        provider._model = "o3-mini"
        provider.default_timeout = None
        provider.reasoning_effort = effort

        mock_choice = MagicMock()
        mock_choice.message.content = "response"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None
        mock_client = MagicMock()
        mock_client.chat.completions.create = MagicMock(
            return_value=mock_response,
        )
        provider._client = mock_client
        return provider, mock_client

    @pytest.mark.asyncio
    async def test_effort_adds_reasoning_effort(self):
        provider, client = self._make_provider("medium")
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_no_effort_omits_reasoning_effort(self):
        provider, client = self._make_provider(None)
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.chat.completions.create.call_args[1]
        assert "reasoning_effort" not in kwargs

    @pytest.mark.asyncio
    async def test_reasoning_model_uses_max_completion_tokens(self):
        provider, client = self._make_provider(None)
        provider._model = "gpt-5.6-luna"
        provider.max_tokens = 1234
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["max_completion_tokens"] == 1234
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_legacy_model_uses_max_tokens(self):
        provider, client = self._make_provider(None)
        provider._model = "gpt-4o"
        provider.max_tokens = 5678
        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )
        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["max_tokens"] == 5678
        assert "max_completion_tokens" not in kwargs


class TestGeminiReasoningEffort:
    """Test Gemini provider passes thinking_config."""

    @pytest.mark.asyncio
    async def test_effort_adds_thinking_config(self):
        from providers.llm.gemini import GeminiLLMProvider

        provider = GeminiLLMProvider.__new__(GeminiLLMProvider)
        provider._model = "gemini-3-pro"
        provider.default_timeout = None
        provider.reasoning_effort = "high"

        mock_candidate = MagicMock()
        mock_candidate.content.parts = []
        mock_candidate.finish_reason = MagicMock(value="STOP")
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = None

        mock_client = MagicMock()
        mock_generate = AsyncMock(return_value=mock_response)
        mock_client.aio.models.generate_content = mock_generate
        provider._client = mock_client

        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )

        call_kwargs = mock_generate.call_args[1]
        config = call_kwargs["config"]
        assert config.thinking_config is not None
        assert config.thinking_config.thinking_level.value == "HIGH"

    @pytest.mark.asyncio
    async def test_no_effort_omits_thinking_config(self):
        from providers.llm.gemini import GeminiLLMProvider

        provider = GeminiLLMProvider.__new__(GeminiLLMProvider)
        provider._model = "gemini-3-pro"
        provider.default_timeout = None
        provider.reasoning_effort = None

        mock_candidate = MagicMock()
        mock_candidate.content.parts = []
        mock_candidate.finish_reason = MagicMock(value="STOP")
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = None

        mock_client = MagicMock()
        mock_generate = AsyncMock(return_value=mock_response)
        mock_client.aio.models.generate_content = mock_generate
        provider._client = mock_client

        await provider.complete(
            system_prompt="test",
            messages=[{"role": "user", "content": "hi"}],
            timeout=0,
        )

        call_kwargs = mock_generate.call_args[1]
        config = call_kwargs["config"]
        assert config.thinking_config is None


class TestConfigReasoningEffort:
    """Test OrchestratorConfig reasoning effort loading."""

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_defaults_to_none(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            assert config.llm_reasoning_effort is None

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_env_override(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {"LLM_REASONING_EFFORT": "high"}):
            config = OrchestratorConfig()
            assert config.llm_reasoning_effort == "high"

    @patch(
        "orchestrator.config._load_config_file",
        return_value={"llm": {"reasoning_effort": "medium"}},
    )
    def test_config_file(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            assert config.llm_reasoning_effort == "medium"

    @patch(
        "orchestrator.config._load_config_file",
        return_value={"llm": {"reasoning_effort": "low"}},
    )
    def test_env_takes_precedence_over_config(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {"LLM_REASONING_EFFORT": "high"}):
            config = OrchestratorConfig()
            assert config.llm_reasoning_effort == "high"

    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "agent_models": {
                "review": {
                    "provider": "claude",
                    "model": "claude-sonnet-4-6",
                    "reasoning_effort": "high",
                },
            },
        },
    )
    def test_per_agent_effort_in_config(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            result = config.get_agent_llm_config("review")
            assert result["reasoning_effort"] == "high"


class TestFactoryWiring:
    """Test _make_llm_factory wires reasoning_effort correctly."""

    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "llm": {"reasoning_effort": "medium"},
            "agent_models": {
                "review": {
                    "provider": "mock",
                    "reasoning_effort": "high",
                },
            },
        },
    )
    def test_per_agent_overrides_global(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _make_llm_factory

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            factory = _make_llm_factory(config)
            provider = factory("review")
            assert provider.reasoning_effort == "high"

    @patch(
        "orchestrator.config._load_config_file",
        return_value={"llm": {"reasoning_effort": "medium"}},
    )
    def test_global_fallback(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _make_llm_factory

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            factory = _make_llm_factory(config)
            provider = factory("benchmark")
            assert provider.reasoning_effort == "medium"

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_no_config_leaves_none(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _make_llm_factory

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            factory = _make_llm_factory(config)
            provider = factory("benchmark")
            assert provider.reasoning_effort is None


class TestValidateModelsRobustness:
    """Ensure _validate_models handles malformed reasoning_effort values."""

    @pytest.mark.asyncio
    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "llm": {
                "provider": "mock",
                "model": "test",
                "reasoning_effort": ["high"],
            },
        },
    )
    async def test_list_reasoning_effort_does_not_crash(self, _mock_cfg, caplog):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _validate_models

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            await _validate_models(config)
        assert "reasoning_effort must be a string" in caplog.text
        assert "list" in caplog.text

    @pytest.mark.asyncio
    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "llm": {"provider": "mock", "model": "test"},
            "agent_models": {
                "review": {
                    "reasoning_effort": {"level": "high"},
                },
            },
        },
    )
    async def test_dict_reasoning_effort_does_not_crash(self, _mock_cfg, caplog):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _validate_models

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            await _validate_models(config)
        assert "reasoning_effort must be a string" in caplog.text
        assert "review" in caplog.text

    @pytest.mark.asyncio
    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "llm": {
                "provider": "mock",
                "model": "test",
                "reasoning_effort": "medium",
            },
            "agent_models": {
                "review": {
                    "reasoning_effort": {"level": "high"},
                },
            },
        },
    )
    async def test_invalid_per_agent_falls_back_to_global(self, _mock_cfg, caplog):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _validate_models

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            await _validate_models(config)
        assert "reasoning_effort must be a string" in caplog.text
        assert config.llm_reasoning_effort == "medium"

    @pytest.mark.asyncio
    @patch(
        "orchestrator.config._load_config_file",
        return_value={
            "llm": {
                "provider": "mock",
                "model": "test",
                "reasoning_effort": 42,
            },
        },
    )
    async def test_int_reasoning_effort_does_not_crash(self, _mock_cfg, caplog):
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _validate_models

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()
            await _validate_models(config)
        assert "reasoning_effort must be a string" in caplog.text
        assert "int" in caplog.text
