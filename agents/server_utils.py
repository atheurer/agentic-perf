"""Shared utilities for agent MCP servers.

All agent MCP servers run as subprocesses and need to set up the Python
path, construct providers, and resolve SSH credentials from tickets.
This module centralizes that setup to avoid duplication.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def setup_project_path() -> str:
    """Add the project root to sys.path. Returns the project root path."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _configured_crucible_source() -> str | None:
    """Return a configured source checkout or URL for triage discovery."""
    source = os.environ.get("CRUCIBLE_SOURCE_REPO")
    source_url = os.environ.get(
        "CRUCIBLE_SOURCE_REPO_URL", "https://github.com/perftool-incubator/crucible.git"
    )
    try:
        from paths import CONFIG_PATH

        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
        source = source or config.get("crucible_source_repo")
        source_url = config.get("crucible_source_repo_url", source_url)
    except (OSError, json.JSONDecodeError):
        pass
    if source:
        return source

    from providers.skills.repo_cache import RepoCache

    try:
        path = RepoCache().ensure_repo("crucible", source_url)
    except Exception:
        logger.warning("Unable to refresh Crucible source repository", exc_info=True)
        return None
    return str(path) if (path / "config" / "repos.json").is_file() else None


def build_skill_provider(source_repo: str | None = None):
    """Construct a MultiHarnessSkillProvider from environment variables.

    Reads CRUCIBLE_HOME and ZATHRAS_HOME from env vars.
    """
    from providers.skills.arcaflow_plugins import ArcaflowPluginSkillProvider
    from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
    from providers.skills.clusterbuster import ClusterbusterSkillProvider
    from providers.skills.crucible import CrucibleSkillProvider
    from providers.skills.forge import ForgeSkillProvider
    from providers.skills.ioscale import IoscaleSkillProvider
    from providers.skills.k8s_netperf import K8sNetperfSkillProvider
    from providers.skills.kube_burner import KubeBurnerSkillProvider
    from providers.skills.multi import MultiHarnessSkillProvider
    from providers.skills.private import PrivateSkillProvider
    from providers.skills.vstorm import VstormSkillProvider
    from providers.skills.zathras import ZathrasSkillProvider

    crucible_home = os.environ.get("CRUCIBLE_HOME", "/opt/crucible")
    source_repo = source_repo or os.environ.get("CRUCIBLE_SOURCE_REPO")
    if not source_repo:
        try:
            from paths import CONFIG_PATH

            config = (
                json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
            )
            source_repo = config.get("crucible_source_repo")
        except (OSError, json.JSONDecodeError):
            source_repo = None
    if not source_repo:
        from paths import SKILL_CACHE_DIR

        default_source = SKILL_CACHE_DIR / "crucible"
        if (default_source / "config" / "repos.json").is_file():
            source_repo = str(default_source)
    zathras_home = os.environ.get("ZATHRAS_HOME", "")

    harnesses: dict[str, Any] = {
        "crucible": CrucibleSkillProvider(crucible_home, source_repo=source_repo),
        "kube-burner": KubeBurnerSkillProvider(),
        "k8s-netperf": K8sNetperfSkillProvider(),
        "benchmark-runner": BenchmarkRunnerSkillProvider(),
        "clusterbuster": ClusterbusterSkillProvider(),
        "vstorm": VstormSkillProvider(),
        "ioscale": IoscaleSkillProvider(),
        "forge": ForgeSkillProvider(),
        "arcaflow-plugins": ArcaflowPluginSkillProvider(),
    }

    if zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(zathras_home)
    else:
        private = PrivateSkillProvider()
        zathras_tests = private._load_config("zathras").get("tests")
        if zathras_tests:
            harnesses["zathras"] = ZathrasSkillProvider(fallback_tests=zathras_tests)

    return MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )


def build_secrets_provider():
    """Construct a SecretsProvider from environment and config.

    Builds a local provider from env vars, then wraps it in a cascade
    with a vault layer when Bitwarden Secrets Manager is configured
    in ``~/.agentic-perf/config.json``.
    """
    from providers.secrets.factory import create_secrets_provider

    backend = os.environ.get("SECRETS_BACKEND", "local")
    config: dict[str, Any] = {}
    secrets_path = os.environ.get("SECRETS_PATH")
    if secrets_path:
        config["path"] = secrets_path
    local = create_secrets_provider(backend, **config)

    vault_config = _load_vault_config()
    bw_config = (vault_config or {}).get("bitwarden", {})
    shared_project_id = bw_config.get("shared_project_id")
    if shared_project_id and bw_config.get("organization_id"):
        try:
            from providers.secrets.cascade import CascadingSecretsProvider
            from providers.secrets.factory import create_bitwarden_provider

            vault = create_bitwarden_provider(
                organization_id=bw_config["organization_id"],
                project_id=shared_project_id,
                server_url=bw_config.get("server_url"),
                cache_ttl_seconds=bw_config.get("cache_ttl_seconds", 60),
            )
            return CascadingSecretsProvider(
                [
                    ("shared", local),
                    ("vault:shared", vault),
                ]
            )
        except ImportError:
            logger.info(
                "bitwarden-sdk not installed; using local secrets only",
            )

    return local


def _load_vault_config() -> dict | None:
    """Load vault config from ``~/.agentic-perf/config.json``."""
    import json

    from paths import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("secrets")
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_vault_secret_name(
    ticket_fields: dict[str, Any] | None = None,
) -> str | None:
    """Determine the vault secret name for SSH key resolution.

    Precedence:
    1. ``ticket_fields["ssh_key_secret"]`` — per-ticket override
    2. ``SSH_KEY_VAULT_SECRET`` env var — deployment override
    3. ``config.json`` → ``ssh_key_vault_secret`` — global default
    """
    if ticket_fields:
        ticket_val = ticket_fields.get("ssh_key_secret")
        if ticket_val:
            return ticket_val

    env_val = os.environ.get("SSH_KEY_VAULT_SECRET")
    if env_val:
        return env_val

    vault_cfg = _load_config_value("ssh_key_vault_secret")
    if vault_cfg:
        return vault_cfg

    return None


def _load_config_value(key: str) -> Any | None:
    """Read a single top-level key from ``~/.agentic-perf/config.json``."""
    import json

    from paths import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get(key)
    except (json.JSONDecodeError, OSError):
        return None


@asynccontextmanager
async def resolve_ssh_key(
    ssh_key_path: str | None,
    secrets_provider: Any | None,
    vault_secret_name: str | None,
) -> AsyncIterator[str | None]:
    """Resolve an SSH key path, falling back to the secrets cascade.

    Yields a filesystem path to the SSH private key. When the key
    comes from a vault provider, it is materialized as an ephemeral
    file (mode 0600) that is removed when the context exits.

    Rules:
    1. ``ssh_key_path`` exists on disk → yield it (no vault call).
    2. ``vault_secret_name`` + provider → materialize from vault.
    3. Vault configured but secret missing → raise
       ``SSHKeyResolutionError`` (fail closed).
    4. Nothing configured → yield original ``ssh_key_path``.
    """
    from providers.ssh import SSHKeyResolutionError

    if ssh_key_path:
        try:
            expanded = Path(ssh_key_path).expanduser()
        except RuntimeError:
            expanded = Path(ssh_key_path)
        if expanded.is_file():
            yield str(expanded)
            return

    if vault_secret_name and secrets_provider:
        async with secrets_provider.secret_file(vault_secret_name) as path:
            if path is not None:
                logger.info(
                    "SSH key resolved from vault secret '%s'",
                    vault_secret_name,
                )
                yield str(path)
                return
        raise SSHKeyResolutionError(
            f"Vault secret '{vault_secret_name}' configured for SSH key "
            f"but not found in secrets provider",
        )

    yield ssh_key_path


_ssh_key_stack: AsyncExitStack | None = None


def build_repo_cache():
    """Construct a RepoCache with harness repos from environment variables."""
    import json

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache()

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

    for name, url in default_repos.items():
        try:
            cache.ensure_repo(name, url)
        except Exception:
            logger.warning("Failed to cache repo %s from %s", name, url, exc_info=True)

    return cache


async def assert_ticket_active(
    ticket_id: str | None = None,
    state_store_url: str | None = None,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """Check that the ticket is still in an active, expected status.

    Returns the full ticket dict on success. Returns a rejection dict
    (with ``"status": "rejected"``) if the ticket has been aborted or
    drifted — the caller should return this to the LLM as a tool result
    instead of proceeding with the side-effecting operation.
    """
    import httpx

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return {}

    headers = {}
    api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(
            f"{state_store_url}/api/v1/tickets/{ticket_id}",
        )
        r.raise_for_status()
        ticket = r.json()

    cf = ticket.get("custom_fields", {})
    status = ticket.get("status", "")

    if cf.get("abort_requested"):
        return {
            "status": "rejected",
            "reason": "Ticket has been aborted",
            "ticket_status": status,
        }

    if expected_status and status != expected_status:
        return {
            "status": "rejected",
            "reason": (f"Ticket status is {status}, expected {expected_status}"),
            "ticket_status": status,
        }

    return ticket


async def build_ssh_from_ticket(
    ticket_id: str | None = None,
    state_store_url: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Fetch a ticket and create an SSHExecutor from its custom_fields.

    Returns (SSHExecutor, ticket_dict). If ticket_id is None, reads from
    TICKET_ID env var. If state_store_url is None, reads from STATE_STORE_URL.
    """
    import httpx

    from providers.ssh import SSHExecutor

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return SSHExecutor(user="root"), {}

    headers = {}
    api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(f"{state_store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()

    fields = ticket.get("custom_fields", {})
    ssh_key = fields.get("ssh_key_path")
    # Always use root — provisioning bootstraps root SSH access.
    # The ticket's ssh_user is the initial cloud login user (e.g., ec2-user),
    # not the runtime user for harness operations.
    ssh_user = "root"

    # Jumpstarter boards get reflashed constantly —
    # host keys change every time. Disable strict
    # checking to avoid stale key errors.
    strict = "no" if fields.get("resource_provider") == "jumpstarter" else "accept-new"

    vault_secret_name = _resolve_vault_secret_name(fields)
    resolved_key = ssh_key
    if vault_secret_name:
        global _ssh_key_stack
        if _ssh_key_stack is not None:
            await _ssh_key_stack.aclose()
        _ssh_key_stack = AsyncExitStack()
        sp = build_secrets_provider()
        resolved_key = await _ssh_key_stack.enter_async_context(
            resolve_ssh_key(ssh_key, sp, vault_secret_name),
        )

    return SSHExecutor(
        user=ssh_user, key_path=resolved_key, strict_host_key=strict
    ), ticket


async def tool_progress(
    message: str,
    tool_name: str,
    ticket_id: str | None = None,
    state_store_url: str | None = None,
) -> None:
    """Post a progress update to the ticket from within an MCP tool.

    Creates both a comment (via the state store API) and an event
    (appended directly to the JSONL event log) so the web UI can
    display progress in real time.

    The event uses type "tool_progress" so the UI can distinguish
    it from regular comments and allow collapsing/minimizing.

    Author is formatted as "agent-name/tool-name" (e.g.,
    "resource-agent/setup_ssh"). The agent name comes from the
    AGENT_NAME env var; tool_name is provided by the caller.

    Reads TICKET_ID and STATE_STORE_URL from env if not provided.
    Silently no-ops if ticket_id is unavailable (e.g., in tests).
    """
    import httpx

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL",
        "http://localhost:8090",
    )
    if not ticket_id:
        return

    agent_name = os.environ.get("AGENT_NAME", "system")
    author = f"{agent_name}/{tool_name}"

    try:
        headers = {}
        api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            await client.post(
                f"{state_store_url}/api/v1/tickets/{ticket_id}/comments",
                json={"author": author, "body": message},
            )
    except Exception:
        logger.debug("Failed to post progress comment for %s", ticket_id, exc_info=True)

    _emit_tool_progress_event(ticket_id, author, message)


_progress_redactor = None


def _get_progress_redactor():
    """Lazy-init a pattern-only Redactor for the bypass path."""
    global _progress_redactor
    if _progress_redactor is None:
        from providers.redaction import Redactor

        _progress_redactor = Redactor()
    return _progress_redactor


def _emit_tool_progress_event(
    ticket_id: str,
    author: str,
    message: str,
) -> None:
    """Append a tool_progress event directly to the JSONL event log.

    Applies pattern-only redaction (no value registry — the MCP
    subprocess has no access to the orchestrator's secret registry).
    """
    import json as _json
    from datetime import datetime, timezone

    from paths import LOG_DIR

    redactor = _get_progress_redactor()
    message = redactor.redact_string(ticket_id, message)

    log_dir = LOG_DIR
    path = log_dir / f"{ticket_id}.jsonl"

    try:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticket_id": ticket_id,
            "agent": author,
            "event_type": "tool_progress",
            "data": {"body": message},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(event, default=str) + "\n")
    except OSError:
        logger.debug(
            "Failed to write tool_progress event for %s", ticket_id, exc_info=True
        )


def build_investigation_provider():
    """Construct an InvestigationRecordProvider from config.

    Reads investigation_records.backend from config.json.
    Defaults to file-based storage.
    """
    from providers.investigation.registry import (
        create_record_provider,
    )

    return create_record_provider()


def read_skill_document(skills_dir: Path, harness: str, filename: str) -> dict:
    """Read a skill document from skills_dir, normalizing redundant prefixes in harness and filename.

    Handles:
    - Stripping leading 'skills/' from filename
    - Stripping redundant f'{harness}/' from filename
    - Splitting category/filename when harness is empty and filename contains '/'
    - Fallback to Path(filename).name if not found directly
    - Resolving when filename contains another valid harness category
    """
    orig_harness = harness or ""
    orig_filename = filename or ""

    harness = str(harness or "").strip().strip("/")
    filename = str(filename or "").strip().strip("/")

    if filename.startswith("skills/"):
        filename = filename[len("skills/") :].lstrip("/")

    if harness and filename.startswith(f"{harness}/"):
        filename = filename[len(f"{harness}/") :].lstrip("/")

    if not harness and "/" in filename:
        parts = filename.split("/", 1)
        harness = parts[0]
        filename = parts[1]

    skill_path = skills_dir / harness / filename
    if not skill_path.is_file():
        # Fallback 1: if filename contains directory parts that failed, try base name under harness
        name_only = Path(filename).name
        if name_only and (skills_dir / harness / name_only).is_file():
            skill_path = skills_dir / harness / name_only
            filename = name_only
        elif "/" in filename:
            # Fallback 2: check if filename itself matches cat/file relative to skills_dir
            cat, fn = filename.split("/", 1)
            cat = cat.strip("/")
            fn = fn.strip("/")
            if (skills_dir / cat / fn).is_file():
                harness = cat
                filename = fn
                skill_path = skills_dir / harness / filename
            elif (skills_dir / cat / Path(fn).name).is_file():
                harness = cat
                filename = Path(fn).name
                skill_path = skills_dir / harness / filename

    if not skill_path.is_file():
        display_harness = harness or orig_harness
        display_filename = filename or orig_filename
        msg_path = (
            f"{display_harness}/{display_filename}"
            if display_harness
            else display_filename
        )
        return {
            "found": False,
            "harness": display_harness,
            "filename": display_filename,
            "message": f"Skill not found: {msg_path}",
        }

    try:
        resolved = skill_path.resolve()
        if not resolved.is_relative_to(skills_dir.resolve()):
            return {
                "found": False,
                "harness": harness,
                "filename": filename,
                "message": "Invalid path",
            }
    except (OSError, ValueError):
        return {
            "found": False,
            "harness": harness,
            "filename": filename,
            "message": "Invalid path",
        }

    return {
        "found": True,
        "harness": harness,
        "filename": filename,
        "content": skill_path.read_text(),
    }
