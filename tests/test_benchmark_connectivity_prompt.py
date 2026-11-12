"""Contract for workload-matched remote-host network preflight guidance."""

from __future__ import annotations

from agents.benchmark.prompts import BENCHMARK_BASE_PROMPT


def test_preflight_matches_workload_protocol() -> None:
    prompt = BENCHMARK_BASE_PROMPT
    assert 'protocol="udp"` for UDP workloads' in prompt
    assert 'protocol="tcp"` for TCP\n   streams' in prompt
    assert "A TCP connect is not evidence of UDP dataplane reachability" in prompt
    assert "UDP success requires the listener to receive the probe payload" in prompt
    assert "If no equivalent probe is available for the workload" in prompt
    assert "own traffic is the acceptance proof" in prompt
    assert (
        "Do not perform this host-to-host\n   check for `endpoint_type: kube`" in prompt
    )
    for reason in (
        "listener_start_failed",
        "sender_error",
        "verification_error",
        "no_payload_received",
    ):
        assert reason in prompt
