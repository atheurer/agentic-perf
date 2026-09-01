"""Tests for shared state-store endpoint resolution."""

from __future__ import annotations

from unittest.mock import patch

import paths
from cli import get_default_store_url
from orchestrator.config import OrchestratorConfig
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
