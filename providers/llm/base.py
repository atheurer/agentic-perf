from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Default timeout for LLM API calls (seconds).
# Can be overridden per-provider or per-call.
DEFAULT_LLM_TIMEOUT: float = 120.0

# Default max output tokens for a single LLM completion.
# Can be overridden per-provider or per-call. Agents that do
# extended thinking (reasoning_effort set) share this budget
# between thinking tokens and visible output, so agents doing
# heavy analysis (e.g. review) are given a much higher value
# via _BUILTIN_AGENT_MODELS in orchestrator/config.py.
DEFAULT_LLM_MAX_TOKENS: int = 8000


class LLMTimeoutError(Exception):
    """Raised when an LLM API call exceeds its timeout."""

    def __init__(self, timeout: float, provider: str = "unknown") -> None:
        self.timeout = timeout
        self.provider = provider
        super().__init__(f"LLM API call to {provider} timed out after {timeout}s")


class LLMRateLimitError(Exception):
    """Raised when an LLM API call is rejected due to rate limiting."""

    def __init__(
        self,
        provider: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        self.provider = provider
        self.retry_after = retry_after
        msg = f"LLM API call to {provider} was rate-limited"
        if retry_after:
            msg += f" (retry after {retry_after}s)"
        super().__init__(msg)


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

    # Per-instance reasoning effort. Controls how much "thinking"
    # the model does. None means use the model's default behavior.
    # Standard levels: "low", "medium", "high". Providers may
    # accept additional values (e.g. Claude's "xhigh"/"max").
    reasoning_effort: str | None = None

    # Per-instance default max output tokens. Set by the
    # orchestrator from config; individual complete() calls can
    # override. None means use DEFAULT_LLM_MAX_TOKENS. When
    # reasoning_effort is set, thinking tokens and visible output
    # share this budget, so agents doing heavy analysis need a
    # much higher value than agents that just call tools.
    max_tokens: int | None = None

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

    def _resolve_max_tokens(self, max_tokens: int | None) -> int:
        """Resolve effective max_tokens from call, instance, and global defaults.

        Precedence: explicit call parameter → instance max_tokens
        → module DEFAULT_LLM_MAX_TOKENS.
        """
        if max_tokens is not None:
            return max_tokens
        if self.max_tokens is not None:
            return self.max_tokens
        return DEFAULT_LLM_MAX_TOKENS

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse: ...
