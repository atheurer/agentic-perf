"""Tests for root-cause enforcement on alert-triggered tickets.

Covers two enforcement points:
  1. submit_analysis_result tool (agents/analyze/server.py) — rejects
     conclusive=True with empty root_cause for anomaly_context tickets.
  2. _check_awaiting_review handoff (orchestrator/handoff.py) — blocks
     the awaiting_review transition under the same condition.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.handoff import check_handoff

# ---------------------------------------------------------------------------
# Handoff enforcement (_check_awaiting_review)
# ---------------------------------------------------------------------------


class TestAwaitingReviewHandoff:
    """_check_awaiting_review: root_cause required for alert-triggered tickets."""

    # --- benchmark path (unaffected) ---

    def test_benchmark_path_passes(self):
        """Benchmark result alone is sufficient; alert guard does not apply."""
        ticket = {
            "custom_fields": {
                "run_id": "12345",
                "benchmark_status": "completed",
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_benchmark_path_no_run_id_blocked(self):
        """Missing run_id on benchmark path is still blocked."""
        ticket = {
            "custom_fields": {
                "benchmark_status": "failed",
            }
        }
        ok, _ = check_handoff("awaiting_review", ticket)
        assert not ok

    # --- analysis-only path, non-alert ticket ---

    def test_analysis_no_anomaly_context_passes(self):
        """Non-alert conclusive analysis without root_cause is allowed."""
        ticket = {
            "custom_fields": {
                "analysis_result": {
                    "conclusive": True,
                    "finding": "RHEL10 is 15% faster than RHEL9.",
                }
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_analysis_inconclusive_no_anomaly_passes(self):
        """Inconclusive non-alert analysis without root_cause is allowed."""
        ticket = {
            "custom_fields": {
                "analysis_result": {
                    "conclusive": False,
                    "finding": "Insufficient baseline data.",
                }
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    # --- analysis-only path, alert-triggered ticket ---

    def test_alert_conclusive_with_root_cause_passes(self):
        """Alert-triggered conclusive analysis WITH root_cause advances."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                    "platform": "NXP_S32G",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Kernel boot time regressed 31%.",
                    "root_cause": (
                        "Kernel updated from 6.12.8 to 6.12.10 between "
                        "builds 20260501 and 20260508. Regression isolated "
                        "to kernel post-timer phase (driver probes)."
                    ),
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_alert_conclusive_without_root_cause_blocked(self):
        """Alert-triggered conclusive analysis WITHOUT root_cause is blocked."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                    "platform": "NXP_S32G",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Kernel boot time regressed 31%.",
                    # root_cause intentionally absent
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason
        assert "alert-triggered" in reason.lower() or "anomaly" in reason.lower()

    def test_alert_conclusive_empty_root_cause_blocked(self):
        """Explicit empty string for root_cause is also blocked."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.time.total_ms",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Total boot time regressed.",
                    "root_cause": "",
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason

    def test_alert_inconclusive_without_root_cause_passes(self):
        """Alert-triggered INCONCLUSIVE analysis without root_cause is fine.

        Inconclusive means the agent couldn't answer the question and
        needs more data — no causal attribution is expected yet.
        """
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                },
                "analysis_result": {
                    "conclusive": False,
                    "finding": "Historical data is too sparse to determine cause.",
                    "benchmark_needed": {
                        "reason": "Need A/B boot-time comparison across two kernel versions."
                    },
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_alert_conclusive_root_cause_whitespace_blocked(self):
        """Whitespace-only root_cause does not satisfy the requirement."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "perf.fio.read.iops",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "IOPS regression confirmed.",
                    "root_cause": "   ",
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason


# ---------------------------------------------------------------------------
# Tool enforcement (submit_analysis_result)
# ---------------------------------------------------------------------------


class TestSubmitAnalysisResultTool:
    """submit_analysis_result: rejects conclusive+empty root_cause for alerts."""

    def _make_client(self, monkeypatch, has_anomaly: bool):
        """Patch AuditedAsyncHTTPClient at its source module."""
        cf: dict = {}
        if has_anomaly:
            cf["anomaly_context"] = {
                "source": "horreum",
                "metric": "boot.phase.kernel_ms",
                "platform": "SA8775P",
            }
        payload = {"custom_fields": cf}

        class _Resp:
            status_code = 200

            def json(self):
                return payload

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get(self, *_args, **_kwargs):
                return _Resp()

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **_kw: _Client(),
        )

    @pytest.mark.asyncio
    async def test_alert_conclusive_no_root_cause_rejected(self, monkeypatch):
        """conclusive=True + no root_cause + anomaly_context → error response."""
        import agents.analyze.server as server_module

        monkeypatch.setenv("TICKET_ID", "PERF-999")
        self._make_client(monkeypatch, has_anomaly=True)

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=True,
                finding="Kernel boot time regressed 31%.",
                evidence="z-score 4.2 vs baseline.",
                root_cause="",
            )
        )
        assert result.get("error") == "root_cause_required"
        assert "conclusive" not in result  # not stored as a valid result

    @pytest.mark.asyncio
    async def test_alert_conclusive_with_root_cause_accepted(self, monkeypatch):
        """conclusive=True + root_cause populated → accepted even for alerts."""
        import agents.analyze.server as server_module

        monkeypatch.setenv("TICKET_ID", "PERF-999")

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=True,
                finding="Kernel boot time regressed 31%.",
                evidence="z-score 4.2 vs baseline.",
                root_cause="Kernel updated 6.12.8→6.12.10 between builds.",
            )
        )
        assert result.get("conclusive") is True
        assert (
            result.get("root_cause") == "Kernel updated 6.12.8→6.12.10 between builds."
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_non_alert_conclusive_no_root_cause_accepted(self, monkeypatch):
        """Non-alert conclusive result without root_cause is accepted.

        The root_cause requirement is scoped to anomaly_context tickets only.
        """
        import agents.analyze.server as server_module

        monkeypatch.setenv("TICKET_ID", "PERF-888")
        self._make_client(monkeypatch, has_anomaly=False)

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=True,
                finding="RHEL10 is 15% faster than RHEL9.",
                evidence="Mean throughput: RHEL9 28.4 Gbps, RHEL10 32.7 Gbps.",
                root_cause="",
            )
        )
        assert result.get("conclusive") is True
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_inconclusive_no_root_cause_accepted(self, monkeypatch):
        """Inconclusive result never requires root_cause."""
        import agents.analyze.server as server_module

        monkeypatch.setenv("TICKET_ID", "PERF-777")

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=False,
                finding="Historical data too sparse.",
                benchmark_needed_reason="Need A/B kernel comparison.",
            )
        )
        assert result.get("conclusive") is False
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_fetch_failure_fails_open(self, monkeypatch):
        """If the ticket fetch fails, submission is not blocked.

        The handoff check is the enforcement backstop.
        """
        import agents.analyze.server as server_module

        monkeypatch.setenv("TICKET_ID", "PERF-666")

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get(self, *_args, **_kwargs):
                raise ConnectionError("store unreachable")

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **_kw: _Client(),
        )

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=True,
                finding="Regression confirmed.",
                root_cause="",
            )
        )
        # Fail open: submission accepted despite missing root_cause
        assert "error" not in result
        assert result.get("conclusive") is True

    @pytest.mark.asyncio
    async def test_no_ticket_id_env_skips_check(self, monkeypatch):
        """With no TICKET_ID env var the fetch is skipped; submission accepted."""
        import agents.analyze.server as server_module

        monkeypatch.delenv("TICKET_ID", raising=False)

        result = json.loads(
            await server_module.submit_analysis_result(
                conclusive=True,
                finding="Regression confirmed.",
                root_cause="",
            )
        )
        assert "error" not in result
        assert result.get("conclusive") is True
