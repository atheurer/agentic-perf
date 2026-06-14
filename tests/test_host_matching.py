"""Tests for _match_to_provider_ip in the resource agent."""

from __future__ import annotations

import pytest

from agents.resource.agent import _match_to_provider_ip


IP_MAPPING = {
    "3.137.187.198": "172.31.6.215",
    "3.141.0.88": "172.31.1.239",
    "3.133.117.181": "172.31.14.60",
}


class TestMatchToProviderIp:
    def test_public_ip_input(self):
        result = _match_to_provider_ip("3.137.187.198", IP_MAPPING)
        assert result == ("3.137.187.198", "172.31.6.215")

    def test_private_ip_input(self):
        result = _match_to_provider_ip("172.31.6.215", IP_MAPPING)
        assert result == ("3.137.187.198", "172.31.6.215")

    def test_aws_hostname_input(self):
        result = _match_to_provider_ip(
            "ip-172-31-6-215.us-east-2.compute.internal", IP_MAPPING
        )
        assert result == ("3.137.187.198", "172.31.6.215")

    def test_aws_hostname_different_host(self):
        result = _match_to_provider_ip(
            "ip-172-31-14-60.us-east-2.compute.internal", IP_MAPPING
        )
        assert result == ("3.133.117.181", "172.31.14.60")

    def test_aws_hostname_short_form(self):
        result = _match_to_provider_ip("ip-172-31-1-239", IP_MAPPING)
        assert result == ("3.141.0.88", "172.31.1.239")

    def test_unknown_host_returns_none(self):
        result = _match_to_provider_ip("unknown.example.com", IP_MAPPING)
        assert result is None

    def test_unknown_ip_returns_none(self):
        result = _match_to_provider_ip("10.0.0.1", IP_MAPPING)
        assert result is None

    def test_empty_mapping(self):
        result = _match_to_provider_ip("3.137.187.198", {})
        assert result is None

    def test_aws_hostname_not_in_mapping(self):
        result = _match_to_provider_ip(
            "ip-10-0-0-1.us-east-2.compute.internal", IP_MAPPING
        )
        assert result is None
