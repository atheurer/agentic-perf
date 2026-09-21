"""Regression coverage for the dashboard's activity/audit presentation split."""

from __future__ import annotations

import subprocess
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


def test_dashboard_renders_provider_record_links_safely() -> None:
    html = INDEX.read_text(encoding="utf-8")

    # The centralized renderer is used by both the list outcome and detail card.
    assert "function renderRecordLink(recordUrl, recordId, style)" in html
    assert html.count("renderRecordLink(") == 3
    assert "dedup.record_url," in html
    assert "dedupResult.record_url," in html

    renderer = html[
        html.index("function escHtml") : html.index("function renderMarkdown")
    ]

    def render(url: str) -> str:
        script = (
            renderer
            + "console.log(renderRecordLink("
            + repr(url)
            + ", 'RCA-<42>', 'color:inherit'));"
        )
        return subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    valid = render("https://horreum.example.com/run/42")
    assert 'href="https://horreum.example.com/run/42"' in valid
    assert 'rel="noopener noreferrer"' in valid
    assert "RCA-&lt;42&gt;" in valid

    # Missing, malformed, and unsafe schemes remain plain escaped IDs.
    assert render("") == "RCA-&lt;42&gt;"
    assert render("not a URL") == "RCA-&lt;42&gt;"
    assert render("javascript:alert(1)") == "RCA-&lt;42&gt;"
