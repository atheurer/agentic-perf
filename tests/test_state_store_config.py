"""Tests for shared state-store endpoint resolution."""

from __future__ import annotations

import os
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import paths
from cli import get_default_store_url
from orchestrator.config import ConfigFileError, OrchestratorConfig, _load_config_file
from orchestrator.main import _ensure_state_store_environment
from paths import resolve_state_store


def test_config_is_shared_by_components(monkeypatch):
    cfg = {"state_store": {"url": "http://localhost:8091", "port": 8091}}
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    monkeypatch.delenv("STORE_PORT", raising=False)
    assert resolve_state_store(cfg) == ("http://localhost:8091", 8091)
    with patch("orchestrator.config._load_config_file", return_value=cfg):
        orchestrator = OrchestratorConfig()
    assert orchestrator.state_store_url == "http://localhost:8091"
    assert orchestrator.state_store_port == 8091


def test_port_only_config_derives_local_url(monkeypatch):
    cfg = {"state_store": {"port": 8092}}
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    monkeypatch.delenv("STORE_PORT", raising=False)
    assert resolve_state_store(cfg) == ("http://localhost:8092", 8092)


def test_cli_reads_instance_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        '{"state_store": {"url": "http://localhost:8094", "port": 8094}}\n'
    )
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    monkeypatch.delenv("STORE_PORT", raising=False)
    assert get_default_store_url() == "http://localhost:8094"


def test_environment_overrides_are_shared(monkeypatch):
    cfg = {"state_store": {"url": "http://localhost:8091", "port": 8091}}
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    monkeypatch.setenv("STORE_PORT", "8093")
    assert resolve_state_store(cfg) == ("http://localhost:8093", 8093)
    assert get_default_store_url() == "http://localhost:8093"


def test_orchestrator_exports_resolved_store_url_for_audited_execution(monkeypatch):
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    config = OrchestratorConfig(
        raw_config={"state_store": {"url": "http://localhost:8095", "port": 8095}}
    )

    _ensure_state_store_environment(config)

    assert os.environ["STATE_STORE_URL"] == "http://localhost:8095"


def test_orchestrator_preserves_explicit_store_url_override(monkeypatch):
    config = OrchestratorConfig(
        raw_config={"state_store": {"url": "http://localhost:8095", "port": 8095}}
    )
    monkeypatch.setenv("STATE_STORE_URL", "http://state-store:8090")

    _ensure_state_store_environment(config)

    assert os.environ["STATE_STORE_URL"] == "http://state-store:8090"


@pytest.mark.parametrize("initialize_immediately", [False, True])
def test_malformed_config_refuses_startup_before_auth_or_backend_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    initialize_immediately: bool,
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    secret = "never-log-this-config-credential"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{\n  "llm": {"api_key": "' + secret + '"},\n'
        '  "state_store": {"port": 8090}\n'
        '  "auth": {"multi_user": true}\n}\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    acquire = MagicMock()
    trace_store = MagicMock()
    token_loader = MagicMock()
    monkeypatch.setattr(main, "_acquire_runtime_lock", acquire)
    monkeypatch.setattr(main, "TraceStore", trace_store)
    monkeypatch.setattr(main, "load_or_generate_token", token_loader)
    with pytest.raises(ConfigFileError, match=r"line 4, column 3") as error:
        app = main.create_app(initialize_immediately=initialize_immediately)
        if not initialize_immediately:
            with TestClient(app):
                pytest.fail("malformed config must never become ready")
    assert "State store refused startup" in caplog.text
    assert "line 4, column 3" in caplog.text
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(error.value))
    acquire.assert_not_called()
    trace_store.assert_not_called()
    token_loader.assert_not_called()


@pytest.mark.parametrize("content", ["null", "[]", '"never-log-this-value"'])
def test_non_object_config_refuses_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content: str,
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    config_path = tmp_path / "config.json"
    config_path.write_text(content)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with pytest.raises(ConfigFileError, match="expected a JSON object"):
        main.create_app(initialize_immediately=True)
    assert "never-log-this-value" not in caplog.text


@pytest.mark.parametrize(
    "auth_config",
    [
        '"never-log-this-value"',
        '{"multi_user": "never-log-this-value"}',
        '{"anonymous_read": "never-log-this-value"}',
        '{"token_ttl_days": "never-log-this-value"}',
    ],
)
def test_invalid_auth_config_refuses_startup_without_exposing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    auth_config: str,
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    config_path = tmp_path / "config.json"
    config_path.write_text('{"auth": ' + auth_config + "}")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with pytest.raises(ValueError, match="auth"):
        main.create_app(initialize_immediately=True)
    assert "never-log-this-value" not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("unreadable"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
    ],
)
def test_unreadable_config_is_not_replaced_by_auth_defaults(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    config_path = MagicMock()
    config_path.read_text.side_effect = failure
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with pytest.raises(ConfigFileError, match="Cannot read"):
        main.create_app(initialize_immediately=True)


def test_missing_config_keeps_auth_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "missing-config.json")
    with TestClient(main.create_app()) as client:
        assert client.get("/api/v1/tickets").status_code == 401
        assert client.get("/api/v1/health").status_code == 200
        assert client.app.state.multi_user is False
        assert client.app.state.anonymous_read is False


def test_valid_config_preserves_multi_user_and_dashboard_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orchestrator.config as config
    import state_store.main as main

    config_path = tmp_path / "config.json"
    config_path.write_text('{"auth": {"multi_user": true, "anonymous_read": false}}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with TestClient(main.create_app()) as client:
        assert client.app.state.multi_user is True
        assert client.get("/api/v1/tickets").status_code == 401
        headers = {"Authorization": f"Bearer {client.app.state.api_token}"}
        assert client.get("/api/v1/tickets", headers=headers).status_code == 200
        assert 'window.API_TOKEN=""' in client.get("/").text


def test_strict_loader_does_not_change_tolerant_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orchestrator.config as config

    config_path = tmp_path / "config.json"
    config_path.write_text("{")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    assert _load_config_file() == {}
    with pytest.raises(ConfigFileError):
        _load_config_file(strict=True)
