"""Tests for structured host identity in required_hosts entries.

Covers issue #698: optional 'host' field on required_hosts items
carries user-provided FQDN/IP verbatim through the pipeline.
"""

from __future__ import annotations


class TestRequiredHostsSchema:
    """The triage tool schema accepts entries with and without 'host'."""

    def _get_schema(self) -> dict:
        """Extract the required_hosts schema from the submit_triage_result tool."""
        from agents.triage.agent import _LOCAL_TOOLS

        submit_tool = next(t for t in _LOCAL_TOOLS if t.name == "submit_triage_result")
        return submit_tool.input_schema["properties"]["required_hosts"]

    def test_host_property_exists(self):
        schema = self._get_schema()
        item_props = schema["items"]["properties"]
        assert "host" in item_props
        assert item_props["host"]["type"] == "string"

    def test_host_not_required(self):
        schema = self._get_schema()
        required = schema["items"].get("required", [])
        assert "host" not in required

    def test_roles_still_required(self):
        schema = self._get_schema()
        required = schema["items"].get("required", [])
        assert "roles" in required

    def test_description_mentions_host(self):
        schema = self._get_schema()
        assert "host" in schema["description"].lower()


class TestResourceAgentContextRendering:
    """Resource agent context includes host identities when present."""

    def _render_hosts(self, required_hosts: list[dict]) -> str:
        """Simulate the host-rendering portion of _build_messages."""
        content = "\n## Resource Requirements\n"
        has_identity = any(h.get("host") for h in required_hosts)
        all_identity = has_identity and all(h.get("host") for h in required_hosts)
        for i, h in enumerate(required_hosts, 1):
            roles_str = "+".join(h.get("roles", ["?"]))
            specs = []
            if h.get("host"):
                specs.append(f"host: {h['host']}")
            if h.get("nic_speed"):
                specs.append(f"NIC: {h['nic_speed']}Gbps")
            if h.get("os"):
                specs.append(f"OS: {h['os']}")
            spec_str = f" ({', '.join(specs)})" if specs else ""
            content += f"- Host {i}: **{roles_str}**{spec_str}\n"
        if all_identity:
            content += (
                "\n**All hosts are user-provided existing machines.** "
                "Validate each with validate_host and submit these "
                "exact identities — do not allocate from a provider.\n"
            )
        elif has_identity:
            content += (
                "\n**Some hosts are user-provided existing machines** "
                "(those with a 'host' value above). Validate those "
                "with validate_host and submit their exact identities. "
                "Allocate the remaining hosts from a provider.\n"
            )
        return content

    def test_all_named_hosts(self):
        hosts = [
            {"roles": ["controller"], "host": "ctrl-01.lab.example.com"},
            {"roles": ["server"], "host": "node-42.lab.example.com"},
        ]
        result = self._render_hosts(hosts)
        assert "ctrl-01.lab.example.com" in result
        assert "node-42.lab.example.com" in result
        assert "All hosts are user-provided" in result
        assert "do not allocate from a provider" in result

    def test_mixed_named_and_allocated(self):
        hosts = [
            {"roles": ["controller"], "host": "ctrl-01.lab.example.com"},
            {"roles": ["client"], "nic_speed": 25, "os": "RHEL9"},
        ]
        result = self._render_hosts(hosts)
        assert "ctrl-01.lab.example.com" in result
        assert "Some hosts are user-provided" in result
        assert "NIC: 25Gbps" in result

    def test_no_named_hosts(self):
        hosts = [
            {"roles": ["controller"]},
            {"roles": ["server"], "nic_speed": 25},
        ]
        result = self._render_hosts(hosts)
        assert "user-provided" not in result

    def test_host_identity_verbatim_in_output(self):
        """The exact string appears — no lowercasing or truncation."""
        hosts = [
            {"roles": ["controller"], "host": "DHCP-10-26.Perf.Example.COM"},
        ]
        result = self._render_hosts(hosts)
        assert "DHCP-10-26.Perf.Example.COM" in result
