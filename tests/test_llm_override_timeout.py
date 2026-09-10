"""Tests for llm_override.timeout in the orchestrator.

Verifies that per-ticket llm_override.timeout is applied to the
LLM provider through the real run_agent_task() path, that omitting
timeout inherits the global config value, and that invalid values
(negative, NaN, infinity, non-numeric) are rejected gracefully.

Closes #667.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.config import OrchestratorConfig


def _make_config(overrides: dict | None = None) -> OrchestratorConfig:
    """Build an OrchestratorConfig with sensible defaults."""
    cfg = {"llm": {"model": "mock", "timeout": 120}}
    if overrides:
        for key, val in overrides.items():
            if isinstance(val, dict) and key in cfg:
                cfg[key].update(val)
            else:
                cfg[key] = val
    return OrchestratorConfig(raw_config=cfg)


def _make_ticket(
    llm_override: dict | None = None,
    extra_fields: dict | None = None,
) -> dict:
    """Build a ticket dict with optional llm_override."""
    cf: dict = {}
    if llm_override is not None:
        cf["llm_override"] = llm_override
    if extra_fields:
        cf.update(extra_fields)
    return {
        "id": "TEST-001",
        "status": "executing_benchmark",
        "summary": "test",
        "custom_fields": cf,
    }


async def _run_override(
    config: OrchestratorConfig,
    ticket: dict,
) -> tuple[MagicMock, MagicMock]:
    """Run the llm_override path through the real run_agent_task().

    Returns (agent_mock, override_provider_mock) so callers can
    verify both the timeout value and that the override provider
    was installed on the agent.
    """
    import httpx

    import orchestrator.main as mod

    mock_provider = MagicMock()
    mock_provider.default_timeout = None
    mock_provider.reasoning_effort = None
    mock_provider.max_tokens = None

    mock_agent = MagicMock()
    mock_agent.llm = MagicMock()
    mock_agent.run = AsyncMock()
    mock_agent.close = AsyncMock()
    mock_agent.request_stop = MagicMock()
    mock_agent.DEFAULT_GLOBAL_MAX_ITERATIONS = 100

    dispatcher = MagicMock()
    dispatcher.store_url = "http://localhost:9999"
    dispatcher.create_agent.return_value = mock_agent

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = ticket

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(
            mod,
            "_make_llm_provider",
            return_value=mock_provider,
        ),
        patch.object(httpx, "AsyncClient", return_value=mock_client),
    ):
        await mod.run_agent_task(
            dispatcher=dispatcher,
            status="executing_benchmark",
            ticket_id="TEST-001",
            config=config,
            ticket_data=ticket,
        )

    mock_agent.run.assert_awaited_once_with("TEST-001")
    return mock_agent, mock_provider


class TestLLMOverrideTimeout:
    """Tests for the timeout field in llm_override."""

    async def test_timeout_applied(self) -> None:
        """Explicit timeout override is applied to the provider."""
        config = _make_config()
        ticket = _make_ticket(llm_override={"timeout": 300})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 300.0

    async def test_timeout_zero_disables(self) -> None:
        """timeout: 0 disables the timeout (0 means no timeout)."""
        config = _make_config()
        ticket = _make_ticket(llm_override={"timeout": 0})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 0.0

    async def test_no_timeout_inherits_global(self) -> None:
        """Without timeout in override, global llm_timeout applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"model": "custom"})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_invalid_timeout_keeps_global(self) -> None:
        """Invalid timeout value is ignored, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": "not-a-number"})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_timeout_as_string_coerced(self) -> None:
        """Numeric string timeout is coerced to float."""
        config = _make_config()
        ticket = _make_ticket(llm_override={"timeout": "300"})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 300.0

    async def test_timeout_with_other_overrides(self) -> None:
        """Timeout works alongside other override fields."""
        config = _make_config()
        ticket = _make_ticket(
            llm_override={
                "model": "custom-model",
                "reasoning_effort": "high",
                "max_tokens": 8192,
                "timeout": 600,
            },
        )
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 600.0
        assert agent.llm.reasoning_effort == "high"
        assert agent.llm.max_tokens == 8192

    async def test_negative_timeout_rejected(self) -> None:
        """Negative timeout is rejected, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": -5})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_nan_timeout_rejected(self) -> None:
        """NaN timeout is rejected, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": float("nan")})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_infinity_timeout_rejected(self) -> None:
        """Infinity timeout is rejected, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": float("inf")})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_negative_infinity_timeout_rejected(self) -> None:
        """Negative infinity timeout is rejected, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": float("-inf")})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120

    async def test_boolean_timeout_rejected(self) -> None:
        """Boolean True/False are rejected as timeout values."""
        config = _make_config({"llm": {"timeout": 120}})
        ticket = _make_ticket(llm_override={"timeout": True})
        agent, provider = await _run_override(config, ticket)
        assert agent.llm is provider
        assert agent.llm.default_timeout == 120
