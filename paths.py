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
TICKET_DIR = AGENTIC_PERF_HOME / "tickets"
LOCK_FILE = AGENTIC_PERF_HOME / "orchestrator.pid"
SKILL_CACHE_DIR = AGENTIC_PERF_HOME / "skill-cache"
PLUGIN_SCHEMA_CACHE_DIR = AGENTIC_PERF_HOME / "plugin-schema-cache"
INVESTIGATION_RECORDS_DIR = AGENTIC_PERF_HOME / "investigation-records"
PRICING_PATH = AGENTIC_PERF_HOME / "pricing.yaml"

SECRETS_DIR = Path(
    os.environ.get("AGENTIC_PERF_SECRETS", AGENTIC_PERF_HOME / "secrets")
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
