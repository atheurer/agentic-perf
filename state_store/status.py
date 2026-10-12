from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from paths import ORCHESTRATOR_STATUS_PATH, STATE_STORE_STATUS_PATH
from providers.execution import AuditedFilesystem

logger = logging.getLogger(__name__)


def record_startup_status(
    status_path: Path,
    phase: str,
    detail: str = "",
    pid: int | None = None,
) -> None:
    """Record startup progress to a status file using AuditedFilesystem.

    Status is advisory telemetry for watchdog progress tracking. It is never
    used as authoritative proof of readiness or lock ownership.
    """
    actual_pid = pid if pid is not None else os.getpid()
    payload = {
        "pid": actual_pid,
        "phase": phase,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "updated_at": time.time(),
    }
    try:
        content = json.dumps(payload) + "\n"
        parent = status_path.parent
        filesystem = AuditedFilesystem.system(parent)
        filesystem.mkdir(".", mode=0o777)
        filesystem.write(status_path.name, content, mode=0o644)
    except Exception:
        logger.debug("Failed to write startup status to %s", status_path, exc_info=True)


def clear_startup_status(status_path: Path) -> None:
    """Safely remove a startup status file using AuditedFilesystem."""
    try:
        if status_path.exists():
            filesystem = AuditedFilesystem.system(status_path.parent)
            filesystem.unlink(status_path.name)
    except Exception:
        logger.debug("Failed to clear startup status at %s", status_path, exc_info=True)


def record_store_status(phase: str, detail: str = "", pid: int | None = None) -> None:
    """Convenience helper for recording state store status."""
    record_startup_status(STATE_STORE_STATUS_PATH, phase, detail=detail, pid=pid)


def clear_store_status() -> None:
    """Convenience helper for clearing state store status."""
    clear_startup_status(STATE_STORE_STATUS_PATH)


def record_orchestrator_status(
    phase: str, detail: str = "", pid: int | None = None
) -> None:
    """Convenience helper for recording orchestrator status."""
    record_startup_status(ORCHESTRATOR_STATUS_PATH, phase, detail=detail, pid=pid)


def clear_orchestrator_status() -> None:
    """Convenience helper for clearing orchestrator status."""
    clear_startup_status(ORCHESTRATOR_STATUS_PATH)
