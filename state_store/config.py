from __future__ import annotations

import os
from typing import Any

from orchestrator.config import ConfigFileError

MAX_EXECUTOR_WORKERS = 64
MAX_EXECUTOR_QUEUE_SIZE = 1024


def _validate_positive_int(
    value: Any,
    name: str,
    *,
    allow_zero: bool = True,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if not allow_zero and value == 0:
        raise ValueError(f"{name} must be > 0, got 0")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value


def _coerce_config_int(value: Any, name: str) -> int:
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigFileError(f"{name} must be an integer") from exc
    return value


def _object_section(
    cfg: dict,
    name: str,
    *,
    parent: dict | None = None,
    label: str | None = None,
) -> dict:
    source = cfg if parent is None else parent
    value = source.get(name, {})
    if not isinstance(value, dict):
        raise ConfigFileError(f"{label or name} must be a JSON object")
    return value


def _executor_settings(cfg: dict) -> dict[str, int]:
    trace_cfg = _object_section(cfg, "trace")
    query_cfg = _object_section(trace_cfg, "query", label="trace.query")
    ingestion_cfg = _object_section(trace_cfg, "ingestion", label="trace.ingestion")

    query_workers = _validate_positive_int(
        query_cfg.get("workers", 2),
        "trace.query.workers",
        allow_zero=False,
        maximum=MAX_EXECUTOR_WORKERS,
    )
    queue_size = _validate_positive_int(
        _coerce_config_int(
            os.environ.get("TRACE_QUERY_MAX_QUEUE", query_cfg.get("queue_size", 8)),
            "trace.query.queue_size",
        ),
        "trace.query.queue_size",
        maximum=MAX_EXECUTOR_QUEUE_SIZE,
    )
    ingestion_workers = _validate_positive_int(
        ingestion_cfg.get("workers", 2),
        "trace.ingestion.workers",
        allow_zero=False,
        maximum=MAX_EXECUTOR_WORKERS,
    )
    ingestion_queue_size = _validate_positive_int(
        _coerce_config_int(
            os.environ.get(
                "TRACE_INGESTION_MAX_QUEUE", ingestion_cfg.get("queue_size", 8)
            ),
            "trace.ingestion.queue_size",
        ),
        "trace.ingestion.queue_size",
        maximum=MAX_EXECUTOR_QUEUE_SIZE,
    )
    max_rss_mb = _validate_positive_int(
        _coerce_config_int(
            os.environ.get("TRACE_QUERY_MAX_RSS_MB", query_cfg.get("max_rss_mb", 4096)),
            "trace.query.max_rss_mb",
        ),
        "trace.query.max_rss_mb",
    )
    control_workers = _validate_positive_int(
        _coerce_config_int(
            os.environ.get("STATE_STORE_CONTROL_WORKERS", 2),
            "STATE_STORE_CONTROL_WORKERS",
        ),
        "STATE_STORE_CONTROL_WORKERS",
        allow_zero=False,
        maximum=MAX_EXECUTOR_WORKERS,
    )
    lease_workers = _validate_positive_int(
        _coerce_config_int(
            os.environ.get("STATE_STORE_LEASE_WORKERS", 1),
            "STATE_STORE_LEASE_WORKERS",
        ),
        "STATE_STORE_LEASE_WORKERS",
        allow_zero=False,
        maximum=MAX_EXECUTOR_WORKERS,
    )
    return {
        "query_workers": query_workers,
        "query_queue_size": queue_size,
        "ingestion_workers": ingestion_workers,
        "ingestion_queue_size": ingestion_queue_size,
        "max_rss_mb": max_rss_mb,
        "control_workers": control_workers,
        "lease_workers": lease_workers,
    }


def _auth_settings(cfg: dict) -> tuple[bool, bool, int]:
    auth_cfg = _object_section(cfg, "auth")
    for name in ("multi_user", "anonymous_read"):
        if not isinstance(auth_cfg.get(name, False), bool):
            raise ConfigFileError(f"auth.{name} must be a boolean")
    token_ttl_days = _validate_positive_int(
        auth_cfg.get("token_ttl_days", 0), "auth.token_ttl_days"
    )
    return (
        auth_cfg.get("multi_user", False),
        auth_cfg.get("anonymous_read", False),
        token_ttl_days,
    )


def _rate_limit_settings(cfg: dict) -> tuple[bool, int, int, bool, int]:
    rate_cfg = _object_section(cfg, "rate_limit")
    enabled = rate_cfg.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigFileError("rate_limit.enabled must be a boolean")
    per_user_rpm = _validate_positive_int(
        rate_cfg.get("per_user_rpm", 600),
        "rate_limit.per_user_rpm",
        allow_zero=not enabled,
    )
    burst = _validate_positive_int(
        rate_cfg.get("burst", 30),
        "rate_limit.burst",
        allow_zero=not enabled,
    )
    exempt_service = rate_cfg.get("exempt_service", True)
    if not isinstance(exempt_service, bool):
        raise ConfigFileError("rate_limit.exempt_service must be a boolean")
    auth_failures_per_min = _validate_positive_int(
        rate_cfg.get("auth_failures_per_min", 30),
        "rate_limit.auth_failures_per_min",
        allow_zero=not enabled,
    )
    return enabled, per_user_rpm, burst, exempt_service, auth_failures_per_min


def validate_runtime_config(cfg: dict) -> None:
    """Validate startup settings without constructing writable components."""
    try:
        _executor_settings(cfg)
        _auth_settings(cfg)
        _rate_limit_settings(cfg)
        _object_section(cfg, "llm")
        agent_models = _object_section(cfg, "agent_models")
        _object_section(agent_models, "chat", label="agent_models.chat")
        chat_cfg = _object_section(cfg, "chat")
        if not isinstance(chat_cfg.get("enabled", False), bool):
            raise ConfigFileError("chat.enabled must be a boolean")
    except ConfigFileError:
        raise
    except ValueError as exc:
        raise ConfigFileError(str(exc)) from None
