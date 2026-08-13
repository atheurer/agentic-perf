"""Per-user and per-group LLM budget quota enforcement.

Provides usage accounting via daily JSONL ledger files and
quota checking against rolling time windows.  Phase 1 ships
warn-only — log + ticket comment, never block — with an
``enforce`` flag to enable hard enforcement later.

Ledger files: ``~/.agentic-perf/logs/usage-ledger-YYYY-MM-DD.jsonl``
One ~200-byte line per LLM call, written by EventBus.record_llm_usage.
Daily files bound growth; a 7-day window reads ≤7 small files.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from paths import LOG_DIR as DEFAULT_LOG_DIR

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Quota model
# ------------------------------------------------------------------


class UserQuota(BaseModel):
    """Per-user or per-group LLM budget quota.

    All fields optional with safe defaults so that a malformed
    quota in users.json does not lock out authentication (the
    ``_load`` skip-on-validation-failure trap).
    """

    max_cost_usd_24h: float = Field(
        default=0.0,
        ge=0.0,
        description="Max LLM cost in a rolling 24-hour window. 0 = no limit.",
    )
    max_cost_usd_7d: float = Field(
        default=0.0,
        ge=0.0,
        description="Max LLM cost in a rolling 7-day window. 0 = no limit.",
    )
    max_tokens_24h: int = Field(
        default=0,
        ge=0,
        description="Max total tokens in a rolling 24-hour window. 0 = no limit.",
    )
    max_tokens_7d: int = Field(
        default=0,
        ge=0,
        description="Max total tokens in a rolling 7-day window. 0 = no limit.",
    )
    enforce: bool = Field(
        default=False,
        description=(
            "When False (default), quota violations are logged "
            "and commented but never block dispatch.  Set True "
            "to enable hard enforcement."
        ),
    )


class QuotaStatus(BaseModel):
    """Result of a quota check."""

    exceeded: bool = False
    warn_only: bool = True
    reasons: list[str] = Field(default_factory=list)
    usage_cost_usd_24h: float = 0.0
    usage_cost_usd_7d: float = 0.0
    usage_tokens_24h: int = 0
    usage_tokens_7d: int = 0


# ------------------------------------------------------------------
# Ledger entry
# ------------------------------------------------------------------


class LedgerEntry(BaseModel):
    """One line in the usage ledger JSONL."""

    ts: str
    ticket_id: str
    charged_to: str
    groups: list[str] = Field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0


# ------------------------------------------------------------------
# Usage ledger (append + windowed read)
# ------------------------------------------------------------------


class UsageLedger:
    """Append-only daily JSONL ledger for LLM usage accounting."""

    def __init__(self, log_dir: str | Path | None = None) -> None:
        self._log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._file_handle: Any = None
        self._current_date: str = ""

    def _ledger_path(self, date_str: str) -> Path:
        return self._log_dir / f"usage-ledger-{date_str}.jsonl"

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def append(self, entry: LedgerEntry) -> None:
        """Append a single usage record to today's ledger."""
        today = self._today()
        try:
            if self._current_date != today:
                if self._file_handle is not None:
                    self._file_handle.close()
                path = self._ledger_path(today)
                self._file_handle = open(path, "a", encoding="utf-8")
                self._current_date = today
            line = entry.model_dump_json() + "\n"
            self._file_handle.write(line)
            self._file_handle.flush()
        except Exception:
            logger.exception("Failed to write usage ledger entry")

    def read_window(
        self,
        window: timedelta,
        charged_to: str | None = None,
        groups: list[str] | None = None,
    ) -> list[LedgerEntry]:
        """Read ledger entries within a rolling time window.

        Filters by ``charged_to`` (exact match) and/or
        ``groups`` (any intersection).  Returns entries
        sorted by timestamp ascending.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - window
        cutoff_iso = cutoff.isoformat()

        days_to_read = window.days + 2
        entries: list[LedgerEntry] = []

        for day_offset in range(days_to_read):
            date = now - timedelta(days=day_offset)
            date_str = date.strftime("%Y-%m-%d")
            path = self._ledger_path(date_str)
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = raw.get("ts", "")
                        if ts < cutoff_iso:
                            continue
                        if charged_to and raw.get("charged_to") != charged_to:
                            continue
                        if groups:
                            entry_groups = set(raw.get("groups", []))
                            if not entry_groups.intersection(groups):
                                continue
                        try:
                            entries.append(LedgerEntry.model_validate(raw))
                        except Exception:
                            continue
            except Exception:
                logger.exception("Failed to read ledger file %s", path)

        entries.sort(key=lambda e: e.ts)
        return entries

    def aggregate_window(
        self,
        window: timedelta,
        charged_to: str | None = None,
        groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate usage within a time window.

        Returns a dict with total_tokens and total_cost_usd.
        """
        entries = self.read_window(window, charged_to=charged_to, groups=groups)
        total_tokens = 0
        total_cost = 0.0
        for e in entries:
            total_tokens += e.input_tokens + e.output_tokens
            total_cost += e.cost_usd
        return {
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "entry_count": len(entries),
        }

    def close(self) -> None:
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None
            self._current_date = ""


# ------------------------------------------------------------------
# Quota checking (pure function)
# ------------------------------------------------------------------


def check_user_quota(
    username: str,
    user_quota: UserQuota | None,
    group_quotas: dict[str, UserQuota] | None,
    ledger: UsageLedger,
    *,
    is_service_account: bool = False,
) -> QuotaStatus:
    """Check whether a user is within their quota limits.

    Applies user AND group quotas (AND semantics — all must
    pass).  Service accounts are exempt unless they have an
    explicit quota set.

    Returns a QuotaStatus indicating whether the quota is
    exceeded and whether enforcement is warn-only.
    """
    if is_service_account and user_quota is None:
        return QuotaStatus()

    reasons: list[str] = []
    any_exceeded_enforced = False
    cost_24h = 0.0
    cost_7d = 0.0
    tokens_24h = 0
    tokens_7d = 0
    checked = False

    if user_quota is not None:
        checked = True
        agg_24h = ledger.aggregate_window(
            timedelta(hours=24),
            charged_to=username,
        )
        agg_7d = ledger.aggregate_window(
            timedelta(days=7),
            charged_to=username,
        )
        cost_24h = agg_24h["total_cost_usd"]
        cost_7d = agg_7d["total_cost_usd"]
        tokens_24h = agg_24h["total_tokens"]
        tokens_7d = agg_7d["total_tokens"]

        user_violated = False
        if user_quota.max_cost_usd_24h > 0 and cost_24h >= user_quota.max_cost_usd_24h:
            reasons.append(
                f"User {username} 24h cost ${cost_24h:.4f} "
                f">= limit ${user_quota.max_cost_usd_24h:.2f}"
            )
            user_violated = True
        if user_quota.max_cost_usd_7d > 0 and cost_7d >= user_quota.max_cost_usd_7d:
            reasons.append(
                f"User {username} 7d cost ${cost_7d:.4f} "
                f">= limit ${user_quota.max_cost_usd_7d:.2f}"
            )
            user_violated = True
        if user_quota.max_tokens_24h > 0 and tokens_24h >= user_quota.max_tokens_24h:
            reasons.append(
                f"User {username} 24h tokens {tokens_24h:,} "
                f">= limit {user_quota.max_tokens_24h:,}"
            )
            user_violated = True
        if user_quota.max_tokens_7d > 0 and tokens_7d >= user_quota.max_tokens_7d:
            reasons.append(
                f"User {username} 7d tokens {tokens_7d:,} "
                f">= limit {user_quota.max_tokens_7d:,}"
            )
            user_violated = True
        if user_violated and user_quota.enforce:
            any_exceeded_enforced = True

    if group_quotas:
        for group_name, gq in group_quotas.items():
            checked = True
            gagg_24h = ledger.aggregate_window(
                timedelta(hours=24),
                groups=[group_name],
            )
            gagg_7d = ledger.aggregate_window(
                timedelta(days=7),
                groups=[group_name],
            )
            gcost_24h = gagg_24h["total_cost_usd"]
            gcost_7d = gagg_7d["total_cost_usd"]
            gtokens_24h = gagg_24h["total_tokens"]
            gtokens_7d = gagg_7d["total_tokens"]

            group_violated = False
            if gq.max_cost_usd_24h > 0 and gcost_24h >= gq.max_cost_usd_24h:
                reasons.append(
                    f"Group {group_name} 24h cost ${gcost_24h:.4f} "
                    f">= limit ${gq.max_cost_usd_24h:.2f}"
                )
                group_violated = True
            if gq.max_cost_usd_7d > 0 and gcost_7d >= gq.max_cost_usd_7d:
                reasons.append(
                    f"Group {group_name} 7d cost ${gcost_7d:.4f} "
                    f">= limit ${gq.max_cost_usd_7d:.2f}"
                )
                group_violated = True
            if gq.max_tokens_24h > 0 and gtokens_24h >= gq.max_tokens_24h:
                reasons.append(
                    f"Group {group_name} 24h tokens {gtokens_24h:,} "
                    f">= limit {gq.max_tokens_24h:,}"
                )
                group_violated = True
            if gq.max_tokens_7d > 0 and gtokens_7d >= gq.max_tokens_7d:
                reasons.append(
                    f"Group {group_name} 7d tokens {gtokens_7d:,} "
                    f">= limit {gq.max_tokens_7d:,}"
                )
                group_violated = True
            if group_violated and gq.enforce:
                any_exceeded_enforced = True

    if not checked:
        return QuotaStatus()

    return QuotaStatus(
        exceeded=len(reasons) > 0,
        warn_only=not any_exceeded_enforced,
        reasons=reasons,
        usage_cost_usd_24h=cost_24h,
        usage_cost_usd_7d=cost_7d,
        usage_tokens_24h=tokens_24h,
        usage_tokens_7d=tokens_7d,
    )


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------


def quota_from_config(config: dict[str, Any]) -> UserQuota | None:
    """Extract default user quota from orchestrator config.

    Config format::

        {
            "llm_budget": {
                "default_user_quota": {
                    "max_cost_usd_24h": 10.00,
                    "max_cost_usd_7d": 50.00,
                    "enforce": false
                }
            }
        }

    Returns None if not configured.
    """
    raw = config.get("llm_budget", {}).get("default_user_quota")
    if not raw:
        return None
    try:
        return UserQuota(**raw)
    except Exception:
        logger.warning("Invalid default_user_quota in config: %s", raw)
        return None


def default_group_quota_from_config(config: dict[str, Any]) -> UserQuota | None:
    """Extract default group quota from orchestrator config.

    Config format::

        {
            "llm_budget": {
                "default_group_quota": {
                    "max_cost_usd_24h": 50.00,
                    "enforce": false
                }
            }
        }
    """
    raw = config.get("llm_budget", {}).get("default_group_quota")
    if not raw:
        return None
    try:
        return UserQuota(**raw)
    except Exception:
        logger.warning("Invalid default_group_quota in config: %s", raw)
        return None


def resolve_quota_inputs(
    username: str,
    user_store: Any,
    config: dict[str, Any],
) -> tuple[
    UserQuota | None,
    dict[str, UserQuota] | None,
    bool,
]:
    """Resolve quota, group quotas, and service account flag.

    Shared by both dispatch-time and in-loop checks so that
    the same resolution logic applies everywhere.

    Returns (user_quota, group_quotas, is_service_account).
    Returns (None, None, False) if the user is not found.
    """
    try:
        user = user_store.get_user(username)
    except Exception:
        return None, None, False

    is_svc = getattr(user, "service_account", False)
    user_quota = getattr(user, "llm_quota", None)

    if user_quota is None and not is_svc:
        user_quota = quota_from_config(config)

    group_quotas: dict[str, UserQuota] = {}
    if not is_svc or user_quota is not None:
        default_gq = default_group_quota_from_config(config)
        for gname in getattr(user, "groups", []):
            try:
                group = user_store.get_group(gname)
                gq = (
                    getattr(group, "llm_quota", None)
                    if getattr(group, "llm_quota", None) is not None
                    else default_gq
                )
            except Exception:
                gq = default_gq
            if gq is not None:
                group_quotas[gname] = gq

    return (
        user_quota,
        group_quotas if group_quotas else None,
        is_svc,
    )
