"""Regression coverage for the dashboard's activity/audit presentation split."""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).parents[1] / "state_store" / "static" / "index.html"


def test_dashboard_defaults_to_condensed_activity_view() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert '<option value="activity">Activity</option>' in html
    assert "var eventView = 'activity';" in html
    assert "var condenseEvents = true;" in html
    assert "function renderEventStream()" in html
    assert "function eventMatchesView(evt)" in html


def test_dashboard_keeps_raw_trace_access_separate_from_activity_stream() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="btn-raw-trace"' in html
    assert "function showRawTrace(ticketId)" in html
    assert "/traces/tickets/" in html
    assert "include_payloads=false" in html


def test_dashboard_bounds_browser_activity_projection() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert "eventRecords.length > 10000" in html
    assert "eventRecords = eventRecords.slice" in html
    assert "event_source === 'audit'" in html
    assert "isAuditFailure(evt)" in html
