from __future__ import annotations

import json
import os
import socket
from pathlib import Path

AGENTIC_PERF_HOME = Path(
    os.environ.get("AGENTIC_PERF_HOME", Path.home() / ".agentic-perf")
)

CONFIG_PATH = AGENTIC_PERF_HOME / "config.json"
LOG_DIR = AGENTIC_PERF_HOME / "logs"
AUDIT_LOG = LOG_DIR / "store-audit.jsonl"
TICKET_DIR = AGENTIC_PERF_HOME / "tickets"
LOCK_FILE = AGENTIC_PERF_HOME / "orchestrator.pid"
SKILL_CACHE_DIR = AGENTIC_PERF_HOME / "skill-cache"
PLUGIN_SCHEMA_CACHE_DIR = AGENTIC_PERF_HOME / "plugin-schema-cache"
INVESTIGATION_RECORDS_DIR = AGENTIC_PERF_HOME / "investigation-records"
PRICING_PATH = AGENTIC_PERF_HOME / "pricing.yaml"

SECRETS_DIR = Path(
    os.environ.get("AGENTIC_PERF_SECRETS", AGENTIC_PERF_HOME / "secrets")
)
ARTIFACT_DIR = Path(
    os.environ.get("AGENTIC_PERF_ARTIFACTS", AGENTIC_PERF_HOME / "artifacts")
)
PRIVATE_SKILLS_DIR = Path(
    os.environ.get("AGENTIC_PERF_SKILLS", AGENTIC_PERF_HOME / "private-skills")
)


def get_instance_name() -> str:
    """Return the identity name for this agentic-perf deployment.

    Resolution order:
    1. AGENTIC_PERF_INSTANCE_NAME env var
    2. instance_name in ~/.agentic-perf/config.json
    3. Short hostname (first label of socket.gethostname())
    """
    env_val = os.environ.get("AGENTIC_PERF_INSTANCE_NAME")
    if env_val:
        return env_val
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            name = cfg.get("instance_name")
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass
    return socket.gethostname().split(".")[0]


def get_default_ssh_key() -> str:
    """Return the default SSH key path for this deployment.

    Resolution order:
    1. SSH_KEY env var
    2. ssh_key_path in ~/.agentic-perf/config.json
    3. ssh_key in ~/.agentic-perf/config.json
    4. ~/.ssh/id_ed25519
    """
    env_val = os.environ.get("SSH_KEY")
    if env_val:
        return env_val
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if isinstance(cfg, dict):
                for key in ("ssh_key_path", "ssh_key"):
                    val = cfg.get(key)
                    if val and isinstance(val, str):
                        return val
        except (json.JSONDecodeError, OSError):
            pass
    return "~/.ssh/id_ed25519"


def create_artifact_dir(
    ticket_id: str,
    run_id: str,
) -> Path:
    """Create and return an artifact directory for a run.

    Structure: ARTIFACT_DIR/<ticket-id>/run-<run-id>/

    Falls back to a temp directory if ticket_id is not set.
    """
    import tempfile

    if ticket_id:
        artifact_dir = ARTIFACT_DIR / ticket_id / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir
    return Path(tempfile.mkdtemp(prefix=f"{run_id}-"))
