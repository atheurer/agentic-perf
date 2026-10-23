from __future__ import annotations

from typing import Any

from .base import LLMProvider


def create_llm_provider(
    provider: str = "mock", model: str = "", **config: Any
) -> LLMProvider:
    """Create an LLM provider by name.

    Supported providers:
      - "claude" / "anthropic": Anthropic Claude API (direct or Vertex AI)
      - "openai": OpenAI-compatible API (OpenAI, Azure, vLLM, Ollama, etc.)
      - "enmaas": Red Hat ENMaaS gateway (GPT, Gemini, DeepSeek, GLM, etc.)
      - "gemini" / "google": Google Gemini API (direct or Vertex AI)
      - "mock": Returns canned responses for testing
    """
    if provider in ("claude", "anthropic"):
        from .claude import ClaudeLLMProvider

        return ClaudeLLMProvider(
            api_key=config.get("api_key"),
            model=model or "claude-haiku-4-5",
            backend=config.get("backend"),
            project_id=config.get("project_id"),
            region=config.get("region"),
        )

    if provider in ("openai", "enmaas"):
        # ENMaaS uses dedicated provider for proper base_url validation
        if provider == "enmaas":
            from .enmaas import ENMaaSLLMProvider

            return ENMaaSLLMProvider(
                api_key=config.get("api_key"),
                model=model or "gpt-5.4",
                base_url=config.get("base_url"),
            )

        # OpenAI generic
        from .openai_compat import OpenAICompatLLMProvider

        return OpenAICompatLLMProvider(
            api_key=config.get("api_key"),
            model=model or "gpt-4o",
            base_url=config.get("base_url"),
            api=config.get("api", "chat_completions"),
        )

    if provider in ("gemini", "google"):
        from .gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key=config.get("api_key") or config.get("gemini_api_key"),
            model=model or "gemini-2.5-flash",
            project_id=config.get("project_id"),
            region=config.get("region"),
        )

    if provider == "mock":
        from .mock import MockLLMProvider

        return MockLLMProvider()

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        f"Supported: 'claude', 'anthropic', 'openai', 'enmaas', 'gemini', 'google', 'mock'"
    )
