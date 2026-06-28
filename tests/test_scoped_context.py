"""Tests for agent-scoped context partitioning."""

from __future__ import annotations

from agents.base import AgentBase


class TestGetScopedContext:
    """Tests for AgentBase._get_scoped_context."""

    def test_returns_none_when_no_scoped_context(self):
        ticket = {"custom_fields": {"benchmark_suite": "uperf"}}
        assert AgentBase._get_scoped_context(ticket, "benchmark") is None

    def test_returns_none_when_empty_custom_fields(self):
        ticket = {"custom_fields": {}}
        assert AgentBase._get_scoped_context(ticket, "benchmark") is None

    def test_returns_none_when_no_custom_fields(self):
        ticket = {}
        assert AgentBase._get_scoped_context(ticket, "benchmark") is None

    def test_combines_shared_and_agent_section(self):
        ticket = {
            "custom_fields": {
                "scoped_context": {
                    "shared": "AWS m5n.4xlarge, RHEL9",
                    "benchmark": "Run uperf stream with 16k messages",
                },
            },
        }
        result = AgentBase._get_scoped_context(ticket, "benchmark")
        assert result == "AWS m5n.4xlarge, RHEL9\n\nRun uperf stream with 16k messages"

    def test_returns_only_shared_when_agent_key_missing(self):
        ticket = {
            "custom_fields": {
                "scoped_context": {
                    "shared": "AWS m5n.4xlarge, RHEL9",
                    "benchmark": "Run uperf stream",
                },
            },
        }
        result = AgentBase._get_scoped_context(ticket, "provisioning")
        assert result == "AWS m5n.4xlarge, RHEL9"

    def test_returns_only_agent_section_when_no_shared(self):
        ticket = {
            "custom_fields": {
                "scoped_context": {
                    "benchmark": "Run uperf stream",
                },
            },
        }
        result = AgentBase._get_scoped_context(ticket, "benchmark")
        assert result == "Run uperf stream"

    def test_returns_none_when_scoped_context_is_not_dict(self):
        ticket = {"custom_fields": {"scoped_context": "invalid"}}
        assert AgentBase._get_scoped_context(ticket, "benchmark") is None

    def test_returns_none_when_both_shared_and_key_empty(self):
        ticket = {"custom_fields": {"scoped_context": {}}}
        assert AgentBase._get_scoped_context(ticket, "benchmark") is None


def _make_ticket(scoped_context=None):
    ticket = {
        "id": "PERF-TEST",
        "summary": "Full ticket summary visible to all",
        "description": "Full description with benchmark params and everything",
        "custom_fields": {},
        "comments": [],
    }
    if scoped_context is not None:
        ticket["custom_fields"]["scoped_context"] = scoped_context
    return ticket


class TestProvisioningAgentScopedContext:
    def test_uses_scoped_context_when_present(self):
        from agents.provisioning.agent import ProvisioningAgent

        ticket = _make_ticket(
            {
                "shared": "AWS RHEL9 environment",
                "provisioning": "Install nmap-ncat on all hosts",
            }
        )
        agent = ProvisioningAgent.__new__(ProvisioningAgent)
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Install nmap-ncat" in content
        assert "AWS RHEL9 environment" in content
        assert "Full description with benchmark params" not in content

    def test_falls_back_to_full_text_when_absent(self):
        from agents.provisioning.agent import ProvisioningAgent

        ticket = _make_ticket()
        agent = ProvisioningAgent.__new__(ProvisioningAgent)
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Full ticket summary" in content
        assert "Full description with benchmark params" in content


class TestBenchmarkAgentScopedContext:
    def test_uses_scoped_context_when_present(self):
        from agents.benchmark.agent import BenchmarkAgent

        ticket = _make_ticket(
            {
                "shared": "AWS RHEL9 environment",
                "benchmark": "Run uperf stream with 16k messages, 1/8/32 threads",
            }
        )
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._repo_cache = None
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Run uperf stream with 16k messages" in content
        assert "Full description with benchmark params" not in content

    def test_falls_back_to_full_text_when_absent(self):
        from agents.benchmark.agent import BenchmarkAgent

        ticket = _make_ticket()
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._repo_cache = None
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Full description with benchmark params" in content


class TestResourceAgentScopedContext:
    def test_uses_scoped_context_when_present(self):
        from agents.resource.agent import ResourceAgent

        ticket = _make_ticket(
            {
                "shared": "AWS RHEL9 environment",
                "resource": "2x m5n.4xlarge compute instances plus controller",
            }
        )
        agent = ResourceAgent.__new__(ResourceAgent)
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "2x m5n.4xlarge" in content
        assert "Full description with benchmark params" not in content

    def test_falls_back_to_full_text_when_absent(self):
        from agents.resource.agent import ResourceAgent

        ticket = _make_ticket()
        agent = ResourceAgent.__new__(ResourceAgent)
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Full description with benchmark params" in content


class TestReviewAgentScopedContext:
    def test_uses_scoped_context_when_present(self):
        from agents.review.agent import ReviewAgent

        ticket = _make_ticket(
            {
                "shared": "AWS RHEL9 environment",
                "review": "Report throughput scaling across thread counts",
            }
        )
        agent = ReviewAgent.__new__(ReviewAgent)
        agent._repo_cache = None
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Report throughput scaling" in content
        assert "Full description with benchmark params" not in content

    def test_falls_back_to_full_text_when_absent(self):
        from agents.review.agent import ReviewAgent

        ticket = _make_ticket()
        agent = ReviewAgent.__new__(ReviewAgent)
        agent._repo_cache = None
        msgs = agent._build_messages(ticket)
        content = msgs[0]["content"]

        assert "Full description with benchmark params" in content


class TestTriageHandleCompletionPersistsContext:
    def test_scoped_context_included_in_fields(self):
        result = {
            "parsed_specs": {},
            "hypothesis": "test",
            "benchmark_suite": "uperf",
            "absent_suite": False,
            "min_hosts": 2,
            "roles": ["client", "server"],
            "directives": {},
            "scoped_context": {
                "shared": "AWS environment",
                "provisioning": "Install nmap-ncat",
                "benchmark": "Run uperf stream",
            },
        }

        scoped_context = result.get("scoped_context")
        fields = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
        }
        if scoped_context and isinstance(scoped_context, dict):
            fields["scoped_context"] = scoped_context

        assert "scoped_context" in fields
        assert fields["scoped_context"]["shared"] == "AWS environment"
        assert fields["scoped_context"]["provisioning"] == "Install nmap-ncat"
        assert fields["scoped_context"]["benchmark"] == "Run uperf stream"

    def test_scoped_context_not_included_when_absent(self):
        result = {
            "parsed_specs": {},
            "hypothesis": "test",
        }

        scoped_context = result.get("scoped_context")
        fields = {"parsed_specs": result.get("parsed_specs", {})}
        if scoped_context and isinstance(scoped_context, dict):
            fields["scoped_context"] = scoped_context

        assert "scoped_context" not in fields
