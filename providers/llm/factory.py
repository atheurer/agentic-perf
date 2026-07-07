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
      - "gemini" / "google": Google Gemini API (direct or Vertex AI)
      - "mock": Returns canned responses for testing
    """
    if provider in ("claude", "anthropic"):
        from .claude import ClaudeLLMProvider

        return ClaudeLLMProvider(
            api_key=config.get("api_key"),
            model=model or "claude-sonnet-4-6",
            backend=config.get("backend"),
            project_id=config.get("project_id"),
            region=config.get("region"),
        )

    if provider == "openai":
        from .openai_compat import OpenAICompatLLMProvider

        return OpenAICompatLLMProvider(
            api_key=config.get("api_key"),
            model=model or "gpt-4o",
            base_url=config.get("base_url"),
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
        f"Supported: 'claude', 'anthropic', 'openai', 'gemini', 'google', 'mock'"
    )
