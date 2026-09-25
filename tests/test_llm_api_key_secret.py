"""Tests for resolving LLM API keys through the secrets provider."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.config import OrchestratorConfig, build_redacted_config
from orchestrator.main import (
    SecretReferenceError,
    _make_llm_factory,
    _resolve_api_key_secret,
    _validate_models,
)


def _config(llm: dict, agent_models: dict | None = None) -> OrchestratorConfig:
    return OrchestratorConfig(
        raw_config={"llm": llm, "agent_models": agent_models or {}}
    )


class TestAPIKeySecretConfig:
    def test_global_secret_only_inherits_for_global_provider(self):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"},
            {"review": {"provider": "claude", "model": "claude-test-model"}},
        )

        assert (
            config.get_agent_llm_config("benchmark")["api_key_secret"] == "openai/key"
        )
        assert "api_key_secret" not in config.get_agent_llm_config("review")

    def test_per_agent_secret_selects_credential_for_provider_override(self):
        secret_ref_field = "".join(("api", "_", "key", "_", "secret"))
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"},
            {
                "review": {
                    "provider": "anthropic",
                    secret_ref_field: "vault/ref-a",
                }
            },
        )

        assert config.get_agent_llm_config("review")[secret_ref_field] == "vault/ref-a"

    def test_alias_of_global_provider_inherits_global_secret(self):
        config = _config(
            {
                "provider": "claude",
                "model": "claude-test-model",
                "api_key_secret": "llm/key",
            },
            {"review": {"provider": "anthropic", "model": "claude-haiku-4-5"}},
        )

        assert config.get_agent_llm_config("review")["api_key_secret"] == "llm/key"

    def test_redacted_config_reports_reference_without_exposing_path(self):
        secret_ref_field = "".join(("api", "_", "key", "_", "secret"))
        config = _config(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key_secret": "private/openai/key",
            },
            {
                "review": {
                    "provider": "anthropic",
                    secret_ref_field: "vault/ref-b",
                }
            },
        )

        snapshot = build_redacted_config(config)
        rendered = json.dumps(snapshot)

        assert snapshot["llm"]["api_key_secret_configured"] is True
        assert snapshot["agent_models"]["review"]["api_key_secret_configured"] is True
        assert "private/openai/key" not in rendered
        assert "private/anthropic/credential-ref" not in rendered


class TestAPIKeySecretResolution:
    async def test_resolves_and_strips_secret_value(self):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value="  test-key\n")

        result = await _resolve_api_key_secret(config, "benchmark", secrets)

        assert result == "test-key"
        secrets.get_secret.assert_awaited_once_with("openai/key")

    @pytest.mark.parametrize("value", [None, "", "  \n"])
    async def test_missing_or_empty_secret_raises_clear_error(self, value):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value=value)

        with pytest.raises(SecretReferenceError, match="was not found"):
            await _resolve_api_key_secret(config, "benchmark", secrets)

    async def test_provider_failure_does_not_leak_backend_error(self):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(side_effect=RuntimeError("backend exposed-key"))

        with pytest.raises(SecretReferenceError) as exc_info:
            await _resolve_api_key_secret(config, "benchmark", secrets)

        assert "backend exposed-key" not in str(exc_info.value)

    async def test_missing_provider_is_reported(self):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )

        with pytest.raises(SecretReferenceError, match="no secrets provider"):
            await _resolve_api_key_secret(config, "benchmark", None)


class TestAPIKeySecretHandoff:
    def test_factory_passes_resolved_key_to_effective_provider(self):
        config = _config({"provider": "openai", "model": "gpt-4o"})
        calls = []

        def capture_provider(*args, **kwargs):
            calls.append(kwargs)
            provider = MagicMock()
            provider.default_timeout = config.llm_timeout
            provider.reasoning_effort = None
            provider.max_tokens = config.llm_max_tokens
            return provider

        with patch(
            "orchestrator.main._make_llm_provider", side_effect=capture_provider
        ):
            _make_llm_factory(config, api_keys={"benchmark": "secret-key"})("benchmark")

        assert calls[0]["api_key"] == "secret-key"
        assert calls[0]["provider"] == "openai"

    async def test_startup_model_probe_uses_shared_secret(self):
        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value="startup-key")
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=MagicMock())
        calls = []

        def capture_provider(*args, **kwargs):
            calls.append(kwargs)
            return provider

        with patch(
            "orchestrator.main._make_llm_provider", side_effect=capture_provider
        ):
            await _validate_models(config, secrets)

        assert len(calls) == 1
        assert calls[0]["api_key"] == "startup-key"

    async def test_ticket_dispatch_resolves_key_before_agent_creation(
        self, monkeypatch
    ):
        import orchestrator.main as main

        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value="ticket-key")
        agent = MagicMock()
        agent.trace_context = None
        agent.DEFAULT_GLOBAL_MAX_ITERATIONS = 100
        agent.run = AsyncMock()
        agent.close = AsyncMock()

        provider_calls = []

        def capture_provider(*args, **kwargs):
            provider_calls.append(kwargs)
            provider = MagicMock()
            provider.default_timeout = config.llm_timeout
            provider.reasoning_effort = None
            provider.max_tokens = config.llm_max_tokens
            return provider

        dispatcher = MagicMock()
        dispatcher.store_url = "http://state-store"
        dispatcher.events = None
        dispatcher._session_id = None
        dispatcher._fencing_epoch = None
        dispatcher._claim_ids = {}
        dispatcher._trace_contexts = {}
        dispatcher._get_secrets_for_ticket.return_value = secrets
        dispatcher.is_deposed.return_value = False
        dispatcher.mark_done = AsyncMock()

        def create_agent(status, **kwargs):
            assert status == "executing_benchmark"
            agent.llm = kwargs["llm_factory"]("benchmark")
            assert kwargs["secrets_provider"] is secrets
            return agent

        dispatcher.create_agent.side_effect = create_agent

        response = MagicMock(status_code=404)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=response)
        client.patch = AsyncMock()
        monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **kwargs: client)

        with patch(
            "orchestrator.main._make_llm_provider", side_effect=capture_provider
        ):
            await main.run_agent_task(
                dispatcher,
                "executing_benchmark",
                "PERF-123",
                config=config,
                ticket_data={"id": "PERF-123"},
            )

        secrets.get_secret.assert_awaited_once_with("openai/key")
        assert provider_calls[0]["api_key"] == "ticket-key"
        assert agent.run.await_count == 1

    @pytest.mark.parametrize(
        ("override_provider", "expected_provider", "expected_override_key"),
        [
            (None, "openai", "ticket-key"),
            ("openai", "openai", "ticket-key"),
            ("anthropic", "anthropic", None),
        ],
    )
    async def test_ticket_llm_override_preserves_key_only_for_same_vendor(
        self,
        monkeypatch,
        override_provider,
        expected_provider,
        expected_override_key,
    ):
        import orchestrator.main as main

        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value="ticket-key")
        ticket = {
            "id": "PERF-123",
            "custom_fields": {
                "llm_override": {
                    **({"provider": override_provider} if override_provider else {}),
                    "model": "override-model",
                }
            },
        }
        agent = MagicMock()
        agent.trace_context = None
        agent.DEFAULT_GLOBAL_MAX_ITERATIONS = 100
        agent.run = AsyncMock()
        agent.close = AsyncMock()

        provider_calls = []

        def capture_provider(*args, **kwargs):
            provider_calls.append(kwargs)
            provider = MagicMock()
            provider.default_timeout = config.llm_timeout
            provider.reasoning_effort = None
            provider.max_tokens = config.llm_max_tokens
            return provider

        dispatcher = MagicMock()
        dispatcher.store_url = "http://state-store"
        dispatcher.events = None
        dispatcher._session_id = None
        dispatcher._fencing_epoch = None
        dispatcher._claim_ids = {}
        dispatcher._trace_contexts = {}
        dispatcher._get_secrets_for_ticket.return_value = secrets
        dispatcher.is_deposed.return_value = False
        dispatcher.mark_done = AsyncMock()

        def create_agent(status, **kwargs):
            agent.llm = kwargs["llm_factory"]("benchmark")
            return agent

        dispatcher.create_agent.side_effect = create_agent

        response = MagicMock(status_code=200)
        response.json.return_value = ticket
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=response)
        client.patch = AsyncMock()
        monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **kwargs: client)

        with patch(
            "orchestrator.main._make_llm_provider", side_effect=capture_provider
        ):
            await main.run_agent_task(
                dispatcher,
                "executing_benchmark",
                "PERF-123",
                config=config,
                ticket_data=ticket,
            )

        assert provider_calls[0]["api_key"] == "ticket-key"
        assert provider_calls[1]["provider"] == expected_provider
        assert provider_calls[1]["api_key"] == expected_override_key

    async def test_introspection_resolves_key_before_start(self):
        from orchestrator.main import _start_introspection_with_secret

        config = _config(
            {"provider": "openai", "model": "gpt-4o", "api_key_secret": "openai/key"}
        )
        secrets = MagicMock()
        secrets.get_secret = AsyncMock(return_value="introspection-key")
        dispatcher = MagicMock()
        dispatcher._get_secrets_for_ticket.return_value = secrets
        dispatcher._introspection_starting = {"PERF-123"}
        provider_calls = []

        def capture_provider(*args, **kwargs):
            provider_calls.append(kwargs)
            provider = MagicMock()
            provider.default_timeout = config.llm_timeout
            provider.reasoning_effort = None
            provider.max_tokens = config.llm_max_tokens
            return provider

        def start_introspection(ticket_id, llm_factory):
            llm_factory("introspection")
            return True

        dispatcher.start_introspection.side_effect = start_introspection

        with patch(
            "orchestrator.main._make_llm_provider", side_effect=capture_provider
        ):
            await _start_introspection_with_secret(
                dispatcher,
                config,
                {"id": "PERF-123"},
                "PERF-123",
            )

        secrets.get_secret.assert_awaited_once_with("openai/key")
        assert provider_calls[0]["api_key"] == "introspection-key"
        assert dispatcher._introspection_starting == set()
