from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Default timeout for LLM API calls (seconds).
# Can be overridden per-provider or per-call.
DEFAULT_LLM_TIMEOUT: float = 120.0


class LLMTimeoutError(Exception):
    """Raised when an LLM API call exceeds its timeout."""

    def __init__(self, timeout: float, provider: str = "unknown") -> None:
        self.timeout = timeout
        self.provider = provider
        super().__init__(f"LLM API call to {provider} timed out after {timeout}s")


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw_content: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None


class LLMProvider(ABC):
    # Per-instance default timeout. Set by the orchestrator
    # from config; individual complete() calls can override.
    # None means use DEFAULT_LLM_TIMEOUT; 0 means no timeout.
    default_timeout: float | None = None

    def _resolve_timeout(self, timeout: float | None) -> float:
        """Resolve effective timeout from call, instance, and global defaults.

        Precedence: explicit call parameter → instance default_timeout
        → module DEFAULT_LLM_TIMEOUT. Returns 0 to disable timeout.
        """
        if timeout is not None:
            return timeout
        if self.default_timeout is not None:
            return self.default_timeout
        return DEFAULT_LLM_TIMEOUT

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse: ...
