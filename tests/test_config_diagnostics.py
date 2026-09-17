"""Tests for redacted runtime configuration diagnostics."""

from __future__ import annotations

import json

import pytest

from orchestrator.config import (
    OrchestratorConfig,
    build_redacted_config,
)

FAKE_CONFIG_DATA = {
    "instance_name": "test-instance",
    "llm": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "timeout": 120,
        "max_tokens": 8000,
    },
    "poll_interval": 5.0,
    "global_max_iterations": 50,
    "agent_task_timeout": 600,
    "stale_task_timeout": 1800,
    "max_concurrent_agents": 4,
    "skip_teardown": True,
    "crucible_home": "/opt/crucible",
    "agent_iterations": {"review": 30, "platform": 5},
    "agent_models": {
        "introspection": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "api_key": "sk-secret-should-not-appear",
        },
    },
    "ssh_key_path": "/home/user/.ssh/id_rsa",
    "ssh_key_vault_secret": "my-vault-secret-name",
}


@pytest.fixture
def fake_config(tmp_path, monkeypatch):
    """Set up paths and env vars for config testing."""
    import paths

    monkeypatch.setattr(paths, "AGENTIC_PERF_HOME", tmp_path)
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(paths, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(paths, "PRIVATE_SKILLS_DIR", tmp_path / "private-skills")
    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path / "artifacts")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-KEY-12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-FAKE-KEY-67890")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "bws-fake-token-99999")
    monkeypatch.setenv("AGENTIC_PERF_INSTANCE_NAME", "test-instance")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    return FAKE_CONFIG_DATA


class TestBuildRedactedConfig:
    def test_expected_keys_present(self, fake_config):
        config = OrchestratorConfig(raw_config=fake_config)
        snapshot = build_redacted_config(config)

        assert snapshot["instance_name"] == "test-instance"
        assert "agentic_perf_home" in snapshot
        assert "config_path" in snapshot
        assert "config_file_exists" in snapshot

        assert "url" in snapshot["state_store"]
        assert "port" in snapshot["state_store"]

        assert snapshot["llm"]["provider"] == "anthropic"
        assert snapshot["llm"]["model"] == "claude-sonnet-4-20250514"
        assert snapshot["llm"]["timeout"] == 120

        assert snapshot["orchestrator"]["poll_interval"] == 5.0
        assert snapshot["orchestrator"]["global_max_iterations"] == 50
        assert snapshot["orchestrator"]["skip_teardown"] is True

        assert snapshot["harness"]["crucible_home"] == "/opt/crucible"
        assert snapshot["agent_iterations"]["review"] == 30
        assert snapshot["ssh"]["key_configured"] is True
        assert snapshot["ssh"]["vault_secret_configured"] is True

    def test_secret_values_never_appear(self, fake_config):
        config = OrchestratorConfig(raw_config=fake_config)
        snapshot = build_redacted_config(config)
        output = json.dumps(snapshot)

        assert "sk-ant-FAKE-KEY-12345" not in output
        assert "sk-openai-FAKE-KEY-67890" not in output
        assert "bws-fake-token-99999" not in output
        assert "sk-secret-should-not-appear" not in output
        assert "ANTHROPIC_API_KEY" not in output
        assert "OPENAI_API_KEY" not in output
        assert "BWS_ACCESS_TOKEN" not in output

    def test_agent_models_exclude_api_keys(self, fake_config):
        config = OrchestratorConfig(raw_config=fake_config)
        snapshot = build_redacted_config(config)

        intro_model = snapshot["agent_models"]["introspection"]
        assert "api_key" not in intro_model
        assert intro_model["provider"] == "anthropic"
        assert intro_model["model"] == "claude-haiku-4-5-20251001"

    def test_paths_are_strings(self, fake_config, tmp_path):
        config = OrchestratorConfig(raw_config=fake_config)
        snapshot = build_redacted_config(config)

        assert isinstance(snapshot["agentic_perf_home"], str)
        assert isinstance(snapshot["config_path"], str)
        assert isinstance(snapshot["paths"]["private_skills_dir"], str)
        assert isinstance(snapshot["paths"]["artifact_dir"], str)
        assert "secrets_dir" not in snapshot["paths"]

    def test_defaults_when_no_config_file(self, tmp_path, monkeypatch):
        import paths

        nonexistent = tmp_path / "missing" / "config.json"
        monkeypatch.setattr(paths, "CONFIG_PATH", nonexistent)
        monkeypatch.setattr(paths, "AGENTIC_PERF_HOME", tmp_path)
        monkeypatch.setattr(paths, "PRIVATE_SKILLS_DIR", tmp_path / "ps")
        monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path / "art")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        config = OrchestratorConfig()
        snapshot = build_redacted_config(config)

        assert snapshot["config_file_exists"] is False
        assert snapshot["llm"]["provider"] == "mock"
        assert snapshot["ssh"]["key_configured"] is False

    def test_credential_bearing_url_sanitized(self, fake_config):
        config = OrchestratorConfig(
            raw_config=fake_config,
            state_store_url="https://user:s3cret@store.example:8090/api",
        )
        snapshot = build_redacted_config(config)
        output = json.dumps(snapshot)

        assert "s3cret" not in output
        assert "user:" not in output
        assert "store.example" in snapshot["state_store"]["url"]

    def test_non_int_agent_iterations_excluded(self, fake_config):
        fake_config["agent_iterations"]["bad"] = "secret-value"
        config = OrchestratorConfig(raw_config=fake_config)
        snapshot = build_redacted_config(config)

        assert "bad" not in snapshot["agent_iterations"]
        assert "secret-value" not in json.dumps(snapshot)
        assert snapshot["agent_iterations"]["review"] == 30


class TestCmdConfig:
    def test_cli_config_show(self, fake_config, tmp_path, monkeypatch, capsys):
        import orchestrator.config as cfg_mod

        (tmp_path / "config.json").write_text(json.dumps(fake_config))
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.json")

        from cli import cmd_config

        args = type("Args", (), {"config_action": "show"})()
        cmd_config(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["instance_name"] == "test-instance"
        assert "sk-ant-FAKE-KEY-12345" not in captured.out
