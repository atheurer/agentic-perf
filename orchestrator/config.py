from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".agentic-perf" / "config.json"


def _load_config_file() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


class OrchestratorConfig:
    def __init__(
        self,
        state_store_url: str | None = None,
        poll_interval: float | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        anthropic_api_key: str | None = None,
        crucible_home: str | None = None,
        zathras_home: str | None = None,
    ) -> None:
        cfg = _load_config_file()
        llm_cfg = cfg.get("llm", {})
        store_cfg = cfg.get("state_store", {})

        self.state_store_url = (
            state_store_url
            or os.environ.get("STATE_STORE_URL")
            or store_cfg.get("url", "http://localhost:8090")
        )
        self.state_store_port = store_cfg.get("port", 8090)
        self.poll_interval = (
            poll_interval
            or _env_float("POLL_INTERVAL")
            or cfg.get("poll_interval")
            or 3.0
        )
        self.llm_provider = (
            llm_provider
            or os.environ.get("LLM_PROVIDER")
            or llm_cfg.get("provider", "mock")
        )
        self.llm_model = (
            llm_model
            or os.environ.get("LLM_MODEL")
            or llm_cfg.get("model", "claude-sonnet-4-6")
        )
        self.llm_backend = (
            os.environ.get("LLM_BACKEND")
            or llm_cfg.get("backend")
        )
        self.llm_project_id = (
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or llm_cfg.get("project_id")
        )
        self.llm_region = (
            os.environ.get("CLOUD_ML_REGION")
            or llm_cfg.get("region")
        )
        self.anthropic_api_key = (
            anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.crucible_home = (
            crucible_home
            or os.environ.get("CRUCIBLE_HOME")
            or cfg.get("crucible_home", "/opt/crucible")
        )
        self.zathras_home = (
            zathras_home
            or os.environ.get("ZATHRAS_HOME")
            or cfg.get("zathras_home", "")
        )
        default_repos = {
            "crucible": "https://github.com/perftool-incubator/crucible.git",
            "crucible-examples": "https://github.com/perftool-incubator/crucible-examples.git",
            "zathras": "https://github.com/redhat-performance/zathras.git",
            "kube-burner": "https://github.com/kube-burner/kube-burner.git",
            "k8s-netperf": "https://github.com/cloud-bulldozer/k8s-netperf.git",
            "benchmark-runner": "https://github.com/redhat-performance/benchmark-runner.git",
            "clusterbuster": "https://github.com/redhat-performance/clusterbuster.git",
        }
        env_repos = os.environ.get("HARNESS_REPOS")
        if env_repos:
            try:
                default_repos.update(json.loads(env_repos))
            except json.JSONDecodeError:
                pass
        default_repos.update(cfg.get("harness_repos", {}))
        self.harness_repos: dict[str, str] = default_repos
        self.ssh_key = (
            os.environ.get("SSH_KEY")
            or cfg.get("ssh_key")
        )


def _env_float(key: str) -> float | None:
    val = os.environ.get(key)
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return None
