"""Tests for cross-ticket artifact resolution."""

from __future__ import annotations

from agents.server_utils import extract_ticket_references


class TestExtractTicketReferences:
    """Test PERF-XXXXXXXX extraction from text."""

    def test_single_reference(self):
        text = "Compare with PERF-0D71DDDE"
        assert extract_ticket_references(text) == ["PERF-0D71DDDE"]

    def test_multiple_references(self):
        text = "Compare PERF-0D71DDDE and PERF-04E41F3F"
        refs = extract_ticket_references(text)
        assert refs == ["PERF-0D71DDDE", "PERF-04E41F3F"]

    def test_deduplication(self):
        text = "PERF-AABBCCDD mentioned twice: PERF-AABBCCDD"
        assert extract_ticket_references(text) == ["PERF-AABBCCDD"]

    def test_preserves_order(self):
        text = "First PERF-11111111 then PERF-22222222"
        refs = extract_ticket_references(text)
        assert refs == ["PERF-11111111", "PERF-22222222"]

    def test_no_references(self):
        text = "Run boot-time on qc8775"
        assert extract_ticket_references(text) == []

    def test_partial_id_ignored(self):
        text = "PERF-ABC is too short"
        assert extract_ticket_references(text) == []

    def test_lowercase_ignored(self):
        text = "perf-0d71ddde lowercase"
        assert extract_ticket_references(text) == []

    def test_embedded_in_url(self):
        text = "See https://example.com/tickets/PERF-0D71DDDE for details"
        assert extract_ticket_references(text) == ["PERF-0D71DDDE"]
