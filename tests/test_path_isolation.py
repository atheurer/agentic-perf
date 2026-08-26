"""Verify that the test sandbox isolates all paths from ~/.agentic-perf/.

The conftest.py preamble sets AGENTIC_PERF_HOME (and related env vars)
to a temporary directory before any project module is imported.  These
tests assert that all path constants and store constructors resolve
inside that sandbox, never touching the real home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import paths

_SANDBOX = Path(os.environ["AGENTIC_PERF_HOME"]).resolve()


class TestPathIsolation:
    """All paths.py constants must resolve inside the test sandbox."""

    def test_home_is_sandboxed(self):
        assert paths.AGENTIC_PERF_HOME.resolve().is_relative_to(_SANDBOX)

    def test_ticket_dir_inside_sandbox(self):
        assert paths.TICKET_DIR.resolve().is_relative_to(_SANDBOX)

    def test_log_dir_inside_sandbox(self):
        assert paths.LOG_DIR.resolve().is_relative_to(_SANDBOX)

    def test_audit_log_inside_sandbox(self):
        assert paths.AUDIT_LOG.resolve().is_relative_to(_SANDBOX)

    def test_secrets_dir_inside_sandbox(self):
        assert paths.SECRETS_DIR.resolve().is_relative_to(_SANDBOX)

    def test_artifact_dir_inside_sandbox(self):
        assert paths.ARTIFACT_DIR.resolve().is_relative_to(_SANDBOX)

    def test_skills_dir_inside_sandbox(self):
        assert paths.PRIVATE_SKILLS_DIR.resolve().is_relative_to(_SANDBOX)

    def test_config_path_inside_sandbox(self):
        assert paths.CONFIG_PATH.resolve().is_relative_to(_SANDBOX)

    def test_lock_file_inside_sandbox(self):
        assert paths.LOCK_FILE.resolve().is_relative_to(_SANDBOX)


class TestStoreIsolation:
    """Store constructors must use the sandboxed paths."""

    def test_ticket_store_default_persist_dir(self):
        from state_store.store import TicketStore

        store = TicketStore()
        assert Path(store._persist_dir).resolve().is_relative_to(_SANDBOX)

    def test_audit_log_default_path(self):
        from state_store.audit import AuditLog

        log = AuditLog()
        assert Path(log._path).resolve().is_relative_to(_SANDBOX)

    def test_event_bus_default_log_dir(self):
        from providers.events import EventBus

        bus = EventBus()
        assert Path(bus._log_dir).resolve().is_relative_to(_SANDBOX)

    def test_auth_token_file_inside_sandbox(self):
        from state_store import auth

        assert Path(auth.TOKEN_FILE).resolve().is_relative_to(_SANDBOX)

    def test_create_app_uses_sandbox(self):
        from state_store.main import create_app

        app = create_app()
        store = app.state.store
        assert Path(store._persist_dir).resolve().is_relative_to(_SANDBOX)

    def test_module_level_app_uses_sandbox(self):
        from state_store import main

        store = main.app.state.store
        assert Path(store._persist_dir).resolve().is_relative_to(_SANDBOX)


class TestSandboxConfiguration:
    """The sandbox env vars are correctly configured."""

    def test_sandbox_is_not_real_home(self):
        real_home = Path.home() / ".agentic-perf"
        assert _SANDBOX != real_home.resolve()

    def test_env_var_points_to_sandbox(self):
        val = os.environ.get("AGENTIC_PERF_HOME", "")
        assert Path(val).resolve() == _SANDBOX
