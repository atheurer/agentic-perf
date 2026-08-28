"""Tests for verbatim agent directive parsing and injection."""

from __future__ import annotations

from agents.base import AgentBase
from state_store.directives import parse_verbatim_directives


class TestParseVerbatimDirectives:
    def test_empty_description(self):
        assert parse_verbatim_directives("") == {}

    def test_no_agent_blocks(self):
        desc = "Please run uperf with 16k messages.\n\n```python\nprint('hello')\n```"
        assert parse_verbatim_directives(desc) == {}

    def test_single_target(self):
        desc = (
            "Some text.\n\n"
            "```agent:provision\n"
            "- 32 combined queues\n"
            "- disable firewall\n"
            "```"
        )
        result = parse_verbatim_directives(desc)
        assert result == {"provision": "- 32 combined queues\n- disable firewall"}

    def test_multiple_targets(self):
        desc = "```agent:provision,benchmark\n- shared directive\n```"
        result = parse_verbatim_directives(desc)
        assert result == {
            "provision": "- shared directive",
            "benchmark": "- shared directive",
        }

    def test_multiple_blocks_same_target(self):
        desc = (
            "```agent:provision\n- directive one\n```\n\n"
            "```agent:provision\n- directive two\n```"
        )
        result = parse_verbatim_directives(desc)
        assert "provision" in result
        assert "directive one" in result["provision"]
        assert "directive two" in result["provision"]

    def test_multiple_blocks_different_targets(self):
        desc = (
            "```agent:provision\n- install nmap\n```\n\n"
            "```agent:benchmark\n- run with 8 threads\n```"
        )
        result = parse_verbatim_directives(desc)
        assert result["provision"] == "- install nmap"
        assert result["benchmark"] == "- run with 8 threads"

    def test_targets_with_spaces(self):
        desc = "```agent:provision, benchmark\n- content\n```"
        result = parse_verbatim_directives(desc)
        assert "provision" in result
        assert "benchmark" in result


class TestGetScopedContextWithVerbatim:
    def _make_ticket(self, verbatim=None, scoped=None):
        ticket = {"custom_fields": {}}
        if verbatim:
            ticket["custom_fields"]["verbatim_directives"] = verbatim
        if scoped:
            ticket["custom_fields"]["scoped_context"] = scoped
        return ticket

    def test_verbatim_only(self):
        ticket = self._make_ticket(
            verbatim={"provision": "- 32 combined queues"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert "Directives (authoritative" in result
        assert "32 combined queues" in result

    def test_verbatim_with_supplemental(self):
        ticket = self._make_ticket(
            verbatim={"provision": "- 32 queues"},
            scoped={"provision": "Also set MTU 9000"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert "Directives (authoritative" in result
        assert "32 queues" in result
        assert "Additional context" in result
        assert "MTU 9000" in result

    def test_verbatim_before_supplemental(self):
        ticket = self._make_ticket(
            verbatim={"provision": "VERBATIM"},
            scoped={"provision": "SUPPLEMENTAL"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert result.index("VERBATIM") < result.index("SUPPLEMENTAL")

    def test_shared_before_verbatim(self):
        ticket = self._make_ticket(
            verbatim={"provision": "VERBATIM"},
            scoped={"shared": "SHARED", "provision": "SUPPLEMENTAL"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert result.index("SHARED") < result.index("VERBATIM")

    def test_no_verbatim_legacy_format(self):
        """Without verbatim directives, output is plain text (no headers)."""
        ticket = self._make_ticket(
            scoped={"shared": "AWS env", "provision": "Install nmap"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert result == "AWS env\n\nInstall nmap"
        assert "Directives" not in result

    def test_verbatim_wrong_agent_key_returns_legacy(self):
        """Verbatim for a different agent does not affect this agent."""
        ticket = self._make_ticket(
            verbatim={"benchmark": "- run fast"},
            scoped={"provision": "Install nmap"},
        )
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert result == "Install nmap"
        assert "Directives" not in result

    def test_verbatim_with_parsed_specs(self):
        ticket = self._make_ticket(
            verbatim={"provision": "- 8 queues"},
            scoped={"shared": "400G test"},
        )
        ticket["custom_fields"]["parsed_specs"] = {
            "network_streams": 8,
            "nic_speed": "400G",
        }
        result = AgentBase._get_scoped_context(ticket, "provision")
        assert "Parsed Specifications" in result
        assert '"network_streams": 8' in result
        assert result.index("Parsed Specifications") < result.index("Directives")


class TestTicketCreationParsesVerbatim:
    def test_verbatim_stored_on_creation(self, tmp_path):
        from state_store.models import CreateTicketRequest
        from state_store.store import TicketStore

        description = (
            "Run uperf.\n\n"
            "```agent:provision\n"
            "- 32 combined queues\n"
            "- disable firewall\n"
            "```"
        )
        store = TicketStore(persist_dir=tmp_path)
        req = CreateTicketRequest(summary="Test ticket", description=description)
        ticket = store.create_ticket(req)

        verbatim = ticket.custom_fields.get("verbatim_directives", {})
        assert "provision" in verbatim
        assert "32 combined queues" in verbatim["provision"]
        assert "disable firewall" in verbatim["provision"]

    def test_no_verbatim_blocks_no_field(self, tmp_path):
        from state_store.models import CreateTicketRequest
        from state_store.store import TicketStore

        store = TicketStore(persist_dir=tmp_path)
        req = CreateTicketRequest(
            summary="Plain ticket",
            description="Just run uperf, no directives.",
        )
        ticket = store.create_ticket(req)
        assert "verbatim_directives" not in ticket.custom_fields
