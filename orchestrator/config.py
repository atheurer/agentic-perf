from __future__ import annotations

import json
import os

from paths import CONFIG_PATH, get_instance_name


def _load_config_file() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


class OrchestratorConfig:
    _BUILTIN_AGENT_MODELS: dict[str, dict[str, str]] = {
        "triage": {"model": "claude-sonnet-4-6"},
        "evaluating_convergence": {"model": "claude-sonnet-4-6"},
        "retrospective": {"model": "claude-sonnet-4-6"},
        # Introspection is a lightweight observer — default to
        # a cheap model since it makes periodic narrative calls
        # across the full ticket lifecycle.
        "introspection": {"model": "claude-haiku-4-5"},
    }

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
        self.raw = cfg  # Full config for subsystem access
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
            or llm_cfg.get("model", "claude-haiku-4-5")
        )
        self.llm_backend = os.environ.get("LLM_BACKEND") or llm_cfg.get("backend")
        self.llm_project_id = os.environ.get(
            "ANTHROPIC_VERTEX_PROJECT_ID"
        ) or llm_cfg.get("project_id")
        self.llm_region = os.environ.get("CLOUD_ML_REGION") or llm_cfg.get("region")
        self.anthropic_api_key = anthropic_api_key or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        self._gemini_api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or llm_cfg.get("gemini_api_key")
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
            "vstorm": "https://github.com/gqlo/vstorm.git",
            "ioscale": "https://github.com/ekuric/ioscale.git",
            "forge": "https://github.com/openshift-psap/forge.git",
            "boot-time-analysis-scripts": "https://gitlab.com/redhat/edge/tests/perfscale/boot-time-analysis-scripts.git",
        }
        env_repos = os.environ.get("HARNESS_REPOS")
        if env_repos:
            try:
                default_repos.update(json.loads(env_repos))
            except json.JSONDecodeError:
                pass
        default_repos.update(cfg.get("harness_repos", {}))
        self.harness_repos: dict[str, str] = default_repos
        self.instance_name: str = get_instance_name()
        self.ssh_key = os.environ.get("SSH_KEY") or cfg.get("ssh_key")
        self._agent_models: dict[str, dict[str, str]] = cfg.get("agent_models", {})
        self._openai_api_key = os.environ.get("OPENAI_API_KEY")
        self._openai_base_url = os.environ.get("OPENAI_BASE_URL") or llm_cfg.get(
            "base_url"
        )

        # LLM budget guardrails (per orchestrator session)
        budget_cfg = cfg.get("llm_budget", {})
        self.budget_session_cost_usd: float = budget_cfg.get("session_cost_usd", 0.0)

        # LLM request timeout (seconds). Applied to each
        # individual LLM API call. 0 disables the timeout.
        self.llm_timeout: float = _env_or_cfg(
            "LLM_TIMEOUT",
            llm_cfg,
            "timeout",
            120.0,
        )

        # Maximum wall-clock time (seconds) for an entire
        # agent task. 0 disables. Catches agents stuck in
        # tool loops or waiting on unresponsive services.
        self.agent_task_timeout: float = _env_or_cfg(
            "AGENT_TASK_TIMEOUT",
            cfg,
            "agent_task_timeout",
            0,
        )

        # Stale-task watchdog: cancel active tasks with no
        # events for this many seconds. 0 disables.
        # Default 3600s (1 hour) to accommodate long benchmark
        # runs with post-processing (e.g., procstat on 768-CPU
        # systems can take 40+ minutes).
        self.stale_task_timeout: float = _env_or_cfg(
            "STALE_TASK_TIMEOUT",
            cfg,
            "stale_task_timeout",
            3600.0,
        )

        # Introspection agent: continuous passive observer.
        # Enable globally via config or env var. Can also be
        # enabled per-ticket via custom_fields.introspection_enabled.
        introspection_cfg = cfg.get("introspection", {})
        self.introspection_enabled: bool = (
            os.environ.get("INTROSPECTION_ENABLED", "").lower() in ("1", "true", "yes")
        ) or introspection_cfg.get("enabled", False)

    def get_agent_llm_config(self, agent_type: str) -> dict[str, str]:
        """Get LLM provider/model config for an agent type.

        Resolution order:
        1. agent_models.<type>         — explicit per-agent config
        2. agent_models.default        — explicit catch-all config
        3. _BUILTIN_AGENT_MODELS.<type> — built-in defaults for
           reasoning-heavy agents (e.g. Sonnet for triage)
        4. top-level llm config        — global default
        """
        if agent_type in self._agent_models:
            return dict(self._agent_models[agent_type])
        if "default" in self._agent_models:
            return dict(self._agent_models["default"])
        builtin = self._BUILTIN_AGENT_MODELS.get(agent_type)
        if builtin:
            base = {"provider": self.llm_provider, "model": self.llm_model}
            base.update(builtin)
            return base
        return {"provider": self.llm_provider, "model": self.llm_model}


def _env_or_cfg(
    env_key: str,
    cfg: dict,
    cfg_key: str,
    default: float,
) -> float:
    """Resolve a float config value from env var or config dict.

    Uses explicit None checks instead of ``or`` so that
    legitimate zero values are not treated as missing.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return float(env_val)
    cfg_val = cfg.get(cfg_key)
    if cfg_val is not None:
        return float(cfg_val)
    return float(default)


def _env_float(key: str) -> float | None:
    val = os.environ.get(key)
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return None
