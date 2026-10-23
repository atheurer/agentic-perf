"""ENMaaS (Enterprise Model as a Service) gateway provider.

Provides OpenAI-compatible API access to multiple models (GPT, Gemini, DeepSeek,
GLM, etc.) via an OpenAI-compatible gateway.

Configuration:
    provider: "enmaas"
    model: "gpt-5.4"
    base_url: "https://your-enmaas-instance.example.com"  # or set ENMAAS_BASE_URL

Environment variables:
    ENMAAS_API_KEY: API key for ENMaaS (falls back to OPENAI_API_KEY)
    ENMAAS_BASE_URL: ENMaaS endpoint URL (required)
"""

from __future__ import annotations

import os
from typing import Any

from .openai_compat import OpenAICompatLLMProvider


class ENMaaSLLMProvider(OpenAICompatLLMProvider):
    """ENMaaS gateway provider extending OpenAI-compatible provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-5.4",
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Resolve base URL from config or env
        resolved_base_url = base_url or os.environ.get("ENMAAS_BASE_URL")
        if not resolved_base_url:
            raise ValueError(
                "ENMaaS provider requires 'base_url' in config or ENMAAS_BASE_URL env var"
            )

        # Auto-detect API key from ENMaaS-specific env or fall back to OPENAI_API_KEY
        resolved_api_key = (
            api_key
            or os.environ.get("ENMAAS_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

        super().__init__(
            api_key=resolved_api_key,
            model=model,
            base_url=resolved_base_url,
            **kwargs,
        )
