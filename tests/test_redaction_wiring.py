"""Tests for redaction wiring — how Redactor integrates with EventBus,
AuditLog, Dispatcher, RecordingSecretsProvider, and the bypass path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from providers.events import EventBus
from providers.redaction import Redactor
from providers.secrets.recording import RecordingSecretsProvider
from state_store.audit import AuditLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET_VALUE = "super-secret-password-12345"
TICKET_ID = "T-WIRE-001"


class StubSecretsProvider:
    """Minimal stand-in for SecretsProvider (avoids ABC instantiation)."""

    def __init__(self, secrets: dict[str, str | None] | None = None) -> None:
        self._secrets = secrets or {}

    async def get_secret(self, path: str) -> str | None:
        return self._secrets.get(path)

    async def get_secret_file(self, path: str) -> Path | None:
        return None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return [k for k in self._secrets if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# RecordingSecretsProvider
# ---------------------------------------------------------------------------


class TestRecordingSecretsProvider:
    async def test_registers_values(self) -> None:
        inner = StubSecretsProvider({"ssh/key": SECRET_VALUE})
        redactor = Redactor()
        recording = RecordingSecretsProvider(inner, redactor, TICKET_ID)

        result = await recording.get_secret("ssh/key")

        assert result == SECRET_VALUE
        assert redactor.redact_string(TICKET_ID, SECRET_VALUE) != SECRET_VALUE
        assert "REDACTED" in redactor.redact_string(TICKET_ID, SECRET_VALUE)

    async def test_none_not_registered(self) -> None:
        inner = StubSecretsProvider({})
        redactor = Redactor()
        recording = RecordingSecretsProvider(inner, redactor, TICKET_ID)

        result = await recording.get_secret("missing/key")

        assert result is None
        # No registration happened — redact_string should pass through
        assert redactor.redact_string(TICKET_ID, "test") == "test"

    async def test_delegates_get_secret_file(self) -> None:
        inner = StubSecretsProvider({})
        redactor = Redactor()
        recording = RecordingSecretsProvider(inner, redactor, TICKET_ID)

        result = await recording.get_secret_file("any/path")
        assert result is None

    async def test_delegates_list_secrets(self) -> None:
        inner = StubSecretsProvider({"a/1": "x", "a/2": "y", "b/1": "z"})
        redactor = Redactor()
        recording = RecordingSecretsProvider(inner, redactor, TICKET_ID)

        result = await recording.list_secrets("a/")
        assert sorted(result) == ["a/1", "a/2"]


# ---------------------------------------------------------------------------
# EventBus redaction
# ---------------------------------------------------------------------------


class TestEventBusRedaction:
    def test_redacts_secret_in_emitted_event(self, tmp_path: Path) -> None:
        redactor = Redactor()
        redactor.register(TICKET_ID, "db/password", SECRET_VALUE)
        bus = EventBus(log_dir=tmp_path / "logs", redactor=redactor)

        bus.emit(
            TICKET_ID,
            "test-agent",
            "tool_result",
            {"output": f"Connected with {SECRET_VALUE}"},
        )
        bus.close()

        jsonl = (tmp_path / "logs" / f"{TICKET_ID}.jsonl").read_text()
        assert SECRET_VALUE not in jsonl
        assert "REDACTED" in jsonl

    def test_no_redactor_passes_data_unchanged(self, tmp_path: Path) -> None:
        bus = EventBus(log_dir=tmp_path / "logs")
        bus.emit(
            TICKET_ID,
            "test-agent",
            "tool_result",
            {"output": f"Connected with {SECRET_VALUE}"},
        )
        bus.close()

        jsonl = (tmp_path / "logs" / f"{TICKET_ID}.jsonl").read_text()
        assert SECRET_VALUE in jsonl

    def test_redacts_patterns_without_values(self, tmp_path: Path) -> None:
        redactor = Redactor()
        bus = EventBus(log_dir=tmp_path / "logs", redactor=redactor)

        bus.emit(
            TICKET_ID,
            "test-agent",
            "tool_result",
            {"output": "Authorization: Bearer eyJhbGciOiJIUzI1N"},
        )
        bus.close()

        jsonl = (tmp_path / "logs" / f"{TICKET_ID}.jsonl").read_text()
        assert "eyJhbGciOiJIUzI1N" not in jsonl
        assert "REDACTED" in jsonl


# ---------------------------------------------------------------------------
# AuditLog redaction
# ---------------------------------------------------------------------------


class TestAuditLogRedaction:
    def test_redacts_patterns(self, tmp_path: Path) -> None:
        redactor = Redactor()
        log = AuditLog(path=tmp_path / "audit.jsonl", redactor=redactor)

        log.log(
            "update",
            TICKET_ID,
            {"field": "Bearer sk-proj-abcdefghijklmnop1234"},
        )
        log.close()

        content = (tmp_path / "audit.jsonl").read_text()
        assert "sk-proj-abcdefghijklmnop1234" not in content
        assert "REDACTED" in content

    def test_redacts_registered_values(self, tmp_path: Path) -> None:
        redactor = Redactor()
        redactor.register(TICKET_ID, "api/key", SECRET_VALUE)
        log = AuditLog(path=tmp_path / "audit.jsonl", redactor=redactor)

        log.log("create", TICKET_ID, {"key": SECRET_VALUE})
        log.close()

        content = (tmp_path / "audit.jsonl").read_text()
        assert SECRET_VALUE not in content
        assert "REDACTED" in content

    def test_no_redactor_passes_data_unchanged(self, tmp_path: Path) -> None:
        log = AuditLog(path=tmp_path / "audit.jsonl")
        log.log("create", TICKET_ID, {"key": SECRET_VALUE})
        log.close()

        content = (tmp_path / "audit.jsonl").read_text()
        assert SECRET_VALUE in content


# ---------------------------------------------------------------------------
# Bypass path (_emit_tool_progress_event)
# ---------------------------------------------------------------------------


class TestBypassPathRedaction:
    def test_redacts_bearer_in_progress_event(self, tmp_path: Path) -> None:
        from agents.server_utils import (
            _emit_tool_progress_event,
            _get_progress_redactor,
        )

        _get_progress_redactor()

        with patch("paths.LOG_DIR", tmp_path):
            _emit_tool_progress_event(
                TICKET_ID,
                "test-agent/tool",
                "Using Bearer sk-ant-api03-supersecrettoken123",
            )

        content = (tmp_path / f"{TICKET_ID}.jsonl").read_text()
        assert "sk-ant-api03-supersecrettoken123" not in content
        assert "REDACTED" in content

    def test_clean_message_passes_through(self, tmp_path: Path) -> None:
        from agents.server_utils import _emit_tool_progress_event

        with patch("paths.LOG_DIR", tmp_path):
            _emit_tool_progress_event(
                TICKET_ID,
                "test-agent/tool",
                "Running benchmark iteration 3 of 10",
            )

        content = (tmp_path / f"{TICKET_ID}.jsonl").read_text()
        event = json.loads(content.strip())
        assert event["data"]["body"] == "Running benchmark iteration 3 of 10"


# ---------------------------------------------------------------------------
# Dispatcher wiring
# ---------------------------------------------------------------------------


class TestDispatcherRedactorWiring:
    def _make_dispatcher(
        self,
        redactor: Redactor | None = None,
        secrets: Any = None,
    ):
        """Create a Dispatcher with minimal mocks."""
        from orchestrator.dispatcher import Dispatcher

        return Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=secrets,
            event_bus=MagicMock(),
            redactor=redactor,
        )

    def test_wraps_secrets_with_recording(self) -> None:
        redactor = Redactor()
        secrets = StubSecretsProvider({"ssh/key": SECRET_VALUE})
        dispatcher = self._make_dispatcher(redactor=redactor, secrets=secrets)

        ticket_data = {"id": TICKET_ID}
        result = dispatcher._get_secrets_for_ticket(ticket_data)

        assert isinstance(result, RecordingSecretsProvider)

    def test_no_redactor_returns_raw_provider(self) -> None:
        secrets = StubSecretsProvider({"ssh/key": SECRET_VALUE})
        dispatcher = self._make_dispatcher(redactor=None, secrets=secrets)

        ticket_data = {"id": TICKET_ID}
        result = dispatcher._get_secrets_for_ticket(ticket_data)

        assert result is secrets
        assert not isinstance(result, RecordingSecretsProvider)

    def test_no_ticket_id_returns_raw_provider(self) -> None:
        redactor = Redactor()
        secrets = StubSecretsProvider({"ssh/key": SECRET_VALUE})
        dispatcher = self._make_dispatcher(redactor=redactor, secrets=secrets)

        # ticket_data with no id field
        result = dispatcher._get_secrets_for_ticket({})
        assert result is secrets

    def test_deregisters_on_mark_done(self) -> None:
        redactor = Redactor()
        redactor.register(TICKET_ID, "ssh/key", SECRET_VALUE)
        dispatcher = self._make_dispatcher(redactor=redactor)

        # Verify value is registered
        assert "REDACTED" in redactor.redact_string(TICKET_ID, SECRET_VALUE)

        dispatcher.mark_done(TICKET_ID)

        # After deregistration, value should pass through (only patterns fire)
        assert redactor.redact_string(TICKET_ID, SECRET_VALUE) == SECRET_VALUE

    def test_mark_done_without_redactor(self) -> None:
        dispatcher = self._make_dispatcher(redactor=None)
        # Should not raise
        dispatcher.mark_done(TICKET_ID)


# ---------------------------------------------------------------------------
# Construction site structural tests
# ---------------------------------------------------------------------------


class TestConstructionSites:
    def test_orchestrator_main_wires_redactor(self) -> None:
        """Verify orchestrator.main constructs EventBus with a redactor."""
        import inspect

        import orchestrator.main as mod

        source = inspect.getsource(mod)
        assert "Redactor()" in source
        assert "EventBus(redactor=" in source
        assert "redactor=redactor" in source

    def test_state_store_main_wires_redactor(self) -> None:
        """Verify state_store.main constructs AuditLog/EventBus with redactor."""
        import inspect

        import state_store.main as mod

        source = inspect.getsource(mod)
        assert "Redactor()" in source
        assert "AuditLog(redactor=" in source
        assert "EventBus(redactor=" in source
