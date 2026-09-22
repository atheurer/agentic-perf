from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paths import CONFIG_PATH, get_instance_name, resolve_state_store

logger = logging.getLogger(__name__)


def _load_config_file() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


class OrchestratorConfig:
    _BUILTIN_AGENT_ITERATIONS: dict[str, int] = {
        "review": 50,
        "platform": 10,
        "evaluating_convergence": 0,
        "analyze": 0,
    }

    # Per-agent capability defaults. These set output budget
    # requirements, NOT model preferences. The user's
    # llm.model is the default for all agents — override
    # per-agent via agent_models.<type> in config.
    _BUILTIN_AGENT_CAPABILITIES: dict[str, dict[str, str]] = {
        # Review generates long markdown reports with tables,
        # charts, and detailed analysis. With reasoning_effort
        # set, thinking tokens share the max_tokens budget.
        "review": {"max_tokens": "32000"},
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
        raw_config: dict | None = None,
    ) -> None:
        cfg = raw_config if raw_config is not None else _load_config_file()
        self.raw = cfg  # Full config for subsystem access
        llm_cfg = cfg.get("llm", {})

        resolved_store_url, resolved_store_port = resolve_state_store(cfg)
        self.state_store_url = state_store_url or resolved_store_url
        self.state_store_port = resolved_store_port
        self.poll_interval = (
            poll_interval
            or _env_float("POLL_INTERVAL")
            or cfg.get("poll_interval")
            or 3.0
        )
        lease_cfg = cfg.get("orchestrator_lease", {})
        self.leader_lease_ttl_seconds = float(
            os.environ.get(
                "ORCHESTRATOR_LEASE_TTL_SECONDS", lease_cfg.get("ttl_seconds", 30.0)
            )
        )
        self.leader_lease_renew_interval = float(
            os.environ.get(
                "ORCHESTRATOR_LEASE_RENEW_INTERVAL",
                lease_cfg.get(
                    "renew_interval", max(1.0, self.leader_lease_ttl_seconds / 3)
                ),
            )
        )
        self.llm_provider = (
            llm_provider
            or os.environ.get("LLM_PROVIDER")
            or llm_cfg.get("provider", "mock")
        )
        self.llm_model = (
            llm_model or os.environ.get("LLM_MODEL") or llm_cfg.get("model", "")
        )
        if self.llm_provider != "mock" and not self.llm_model:
            logger.warning(
                "No LLM model configured. Set llm.model in "
                "config.json or LLM_MODEL env var."
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
        self.ssh_key = (
            os.environ.get("SSH_KEY") or cfg.get("ssh_key_path") or cfg.get("ssh_key")
        )
        self.ssh_key_vault_secret = os.environ.get("SSH_KEY_VAULT_SECRET") or cfg.get(
            "ssh_key_vault_secret"
        )
        self._agent_models: dict[str, dict[str, str]] = cfg.get("agent_models", {})
        if "default" in self._agent_models:
            logger.warning(
                "agent_models.default is deprecated and "
                "ignored. Set llm.model for the global "
                "default. Use agent_models.<type> for "
                "per-agent overrides."
            )
        self._agent_iterations: dict[str, int] = cfg.get("agent_iterations", {})

        # Bridge legacy jumpstarter_images.provisioning_max_iterations
        if "provisioning" not in self._agent_iterations:
            legacy_val = cfg.get("jumpstarter_images", {}).get(
                "provisioning_max_iterations",
            )
            if legacy_val is not None:
                self._agent_iterations["provisioning"] = int(legacy_val)
                logger.warning(
                    "jumpstarter_images.provisioning_max_iterations is"
                    " deprecated; use agent_iterations.provisioning instead"
                )

        self.global_max_iterations: int = int(
            _env_or_cfg(
                "GLOBAL_MAX_ITERATIONS",
                cfg,
                "global_max_iterations",
                100,
            )
        )
        self._openai_api_key = os.environ.get("OPENAI_API_KEY")
        self._openai_base_url = os.environ.get("OPENAI_BASE_URL") or llm_cfg.get(
            "base_url"
        )
        self.llm_api = os.environ.get("OPENAI_API") or llm_cfg.get(
            "api", "chat_completions"
        )

        # LLM budget guardrails (per orchestrator session)
        budget_cfg = cfg.get("llm_budget", {})
        self.budget_session_cost_usd: float = budget_cfg.get("session_cost_usd", 0.0)

        # LLM reasoning effort. Controls how much "thinking"
        # models do. None uses the model's default behavior.
        self.llm_reasoning_effort: str | None = os.environ.get(
            "LLM_REASONING_EFFORT"
        ) or llm_cfg.get("reasoning_effort")

        # LLM request timeout (seconds). Applied to each
        # individual LLM API call. 0 disables the timeout.
        self.llm_timeout: float = _env_or_cfg(
            "LLM_TIMEOUT",
            llm_cfg,
            "timeout",
            120.0,
        )

        # Max output tokens for a single LLM completion. Applies
        # to agents without a more specific override (see
        # _BUILTIN_AGENT_CAPABILITIES and agent_models.<type>.max_tokens
        # in config.json).
        self.llm_max_tokens: int = int(
            _env_or_cfg(
                "LLM_MAX_TOKENS",
                llm_cfg,
                "max_tokens",
                8000,
            )
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

        # Per-user teardown default. When a ticket does not
        # explicitly set directives.skip_teardown, this value
        # is used. Protects run data on user-provided hardware
        # where hosts are shared across experiments.
        self.skip_teardown: bool = cfg.get("skip_teardown", False)

        # Maximum number of concurrent agent tasks. Prevents
        # resource exhaustion when the state store contains a
        # large backlog of pending tickets. Excess tickets are
        # skipped and picked up on subsequent poll cycles.
        _max_agents_raw = _env_or_cfg(
            "MAX_CONCURRENT_AGENTS",
            cfg,
            "max_concurrent_agents",
            8,
        )
        self.max_concurrent_agents: int = max(1, int(_max_agents_raw))

        # Introspection agent: continuous passive observer.
        # Enable globally via config or env var. Can also be
        # enabled per-ticket via custom_fields.introspection_enabled.
        introspection_cfg = cfg.get("introspection", {})
        self.introspection_enabled: bool = (
            os.environ.get("INTROSPECTION_ENABLED", "").lower() in ("1", "true", "yes")
        ) or introspection_cfg.get("enabled", False)

        # Whether introspection uses LLM for narrative and
        # guidance suggestions. When False, introspection runs
        # deterministic-only (anomaly detection, event counting,
        # guidance summary classification) without LLM calls.
        self.introspection_llm: bool = introspection_cfg.get("llm", True)

    def get_agent_llm_config(self, agent_type: str) -> dict[str, str]:
        """Get LLM provider/model config for an agent type.

        Layers (later wins per-key):
        1. ``llm.*``                       — global default
        2. ``_BUILTIN_AGENT_CAPABILITIES`` — per-agent output
           budget (e.g. max_tokens). Never touches model.
        3. ``agent_models.<type>``         — per-agent override

        ``llm.model`` is the default for ALL agents. Use
        ``agent_models.<type>`` only when a specific agent
        needs a different model (e.g. a cheaper model for
        introspection).
        """
        base = {"provider": self.llm_provider, "model": self.llm_model}
        if self.llm_provider == "openai":
            base["api"] = self.llm_api

        # Capability defaults (max_tokens etc) — always applied.
        capabilities = self._BUILTIN_AGENT_CAPABILITIES.get(agent_type)
        if capabilities:
            base.update(capabilities)

        # Explicit per-agent overrides from config.
        if agent_type in self._agent_models:
            base.update(self._agent_models[agent_type])
        if base.get("provider") == "openai" and "api" not in base:
            base["api"] = self.llm_api
        return base

    def get_agent_max_iterations(self, agent_type: str) -> int | None:
        """Get max_iterations for an agent type.

        Layers (first match wins):
        1. agent_iterations.<agent_type>  — explicit per-agent config
        2. agent_iterations.default       — explicit catch-all
        3. _BUILTIN_AGENT_ITERATIONS      — built-in defaults
        4. None                           — agent constructor default

        Uses ``is not None`` checks so 0 (unlimited) is a valid value.
        """
        val = self._agent_iterations.get(agent_type)
        if val is not None:
            return int(val)
        default_val = self._agent_iterations.get("default")
        if default_val is not None:
            return int(default_val)
        builtin = self._BUILTIN_AGENT_ITERATIONS.get(agent_type)
        if builtin is not None:
            return builtin
        return None


def _sanitize_url(url: str) -> str:
    """Strip credentials from a URL (userinfo, query, fragment)."""
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # ``hostname`` drops userinfo, but preserve the useful non-secret port.
        # Bracket IPv6 literals when rebuilding the authority component.
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunparse((parsed.scheme, hostname + port, parsed.path, "", "", ""))
    except (TypeError, ValueError):
        return "(invalid URL)"


_POLICY_KEYS = frozenset(
    {
        "on_existing_install",
        "install_method",
        "install_script",
        "install_target_path",
        "verify_command",
        "update_command",
        "run_install_as_root",
    }
)


def _redacted_private_skills(skills_dir: Path) -> dict:
    """Describe private skill files without exposing their private contents."""
    files: dict[str, dict] = {}
    if not skills_dir.is_dir():
        return {"path": str(skills_dir), "directory_exists": False, "files": files}

    for path in sorted(skills_dir.glob("*.json")):
        if not path.is_file():
            continue
        entry: dict = {"keys": [], "loaded": False, "policies": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            files[path.stem] = entry
            continue
        if not isinstance(raw, dict):
            files[path.stem] = entry
            continue

        entry["loaded"] = True
        entry["keys"] = sorted(str(key) for key in raw)
        for section_name, section in raw.items():
            if not isinstance(section, dict):
                continue
            policies = {
                key: section[key]
                for key in sorted(_POLICY_KEYS.intersection(section))
                if isinstance(section[key], (bool, int, float, str))
            }
            if policies:
                entry["policies"][str(section_name)] = policies
        files[path.stem] = entry

    return {"path": str(skills_dir), "directory_exists": True, "files": files}


def build_redacted_config(
    config: OrchestratorConfig, *, source: str = "in-process"
) -> dict:
    """Build a diagnostic snapshot of the effective config.

    Uses an allowlist — only known-safe fields are included.
    Secret values (API keys, tokens, vault credentials) are
    never present in the output, by construction. Values in
    user-controlled fields are sanitized or type-validated.
    """
    import paths

    # Import paths at call time so diagnostics describe the service's path
    # environment, including tests and multi-instance deployments.
    agentic_perf_home = paths.AGENTIC_PERF_HOME
    artifact_dir = paths.ARTIFACT_DIR
    config_path = paths.CONFIG_PATH
    private_skills_dir = paths.PRIVATE_SKILLS_DIR
    secrets_dir = paths.SECRETS_DIR

    safe_iterations: dict[str, int] = {}
    for k, v in config._agent_iterations.items():
        try:
            safe_iterations[str(k)] = int(v)
        except (ValueError, TypeError):
            continue

    safe_agent_models: dict[str, dict[str, str]] = {}
    for k, v in config._agent_models.items():
        if not isinstance(v, dict):
            continue
        safe_agent_models[str(k)] = {
            "provider": str(v.get("provider", "")),
            "model": str(v.get("model", "")),
        }

    snapshot: dict = {
        "instance_name": config.instance_name,
        "agentic_perf_home": str(agentic_perf_home),
        "config_path": str(config_path),
        "config_file_exists": config_path.exists(),
        "runtime": {
            "source": source,
            "pid": os.getpid(),
            "config_reload": "per-dispatch",
            "private_skills_cache": "until-restart",
            "restart_required_after_private_skill_change": True,
        },
        "paths": {
            "private_skills_dir": str(private_skills_dir),
            "secrets_dir": str(secrets_dir),
            "artifact_dir": str(artifact_dir),
        },
        "private_skills": _redacted_private_skills(private_skills_dir),
        "state_store": {
            "url": _sanitize_url(config.state_store_url),
            "port": config.state_store_port,
        },
        "llm": {
            "provider": config.llm_provider,
            "model": config.llm_model,
            "backend": config.llm_backend,
            "region": config.llm_region,
            "api": config.llm_api,
            "timeout": config.llm_timeout,
            "max_tokens": config.llm_max_tokens,
            "reasoning_effort": config.llm_reasoning_effort,
        },
        "orchestrator": {
            "poll_interval": config.poll_interval,
            "global_max_iterations": config.global_max_iterations,
            "agent_task_timeout": config.agent_task_timeout,
            "stale_task_timeout": config.stale_task_timeout,
            "max_concurrent_agents": config.max_concurrent_agents,
            "skip_teardown": config.skip_teardown,
            "leader_lease_ttl_seconds": config.leader_lease_ttl_seconds,
            "leader_lease_renew_interval": config.leader_lease_renew_interval,
        },
        "harness": {
            "crucible_home": config.crucible_home,
            "zathras_home": config.zathras_home,
        },
        "introspection": {
            "enabled": config.introspection_enabled,
            "llm": config.introspection_llm,
        },
        "budget": {
            "session_cost_usd": config.budget_session_cost_usd,
        },
        "ssh": {
            "key_configured": config.ssh_key is not None,
            "vault_secret_configured": config.ssh_key_vault_secret is not None,
        },
        "agent_iterations": safe_iterations,
        "agent_models": safe_agent_models,
    }
    return snapshot


def _effective_config_path() -> Path:
    import paths

    return paths.AGENTIC_PERF_HOME / "effective-config.json"


def write_effective_config(config: OrchestratorConfig) -> dict:
    """Persist the service's redacted config for authenticated diagnostics.

    The file contains an allowlisted snapshot only and is atomically replaced.
    It is intentionally not treated as input configuration.
    """
    snapshot = build_redacted_config(config, source="orchestrator")
    destination = _effective_config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot["runtime"]["written_at"] = datetime.now(timezone.utc).isoformat()
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return snapshot


def read_effective_config() -> dict | None:
    """Read a live service snapshot, ignoring stale or malformed files."""
    path = _effective_config_path()
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    runtime = snapshot.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("source") != "orchestrator":
        return None
    pid = runtime.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return snapshot


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
