from __future__ import annotations

from typing import Any

from .base import LLMProvider, LLMResponse, ToolCall, ToolDefinition

MOCK_TRIAGE_RESULT = {
    "parsed_specs": {"os": "rhel9", "cpu": 16, "ram": 64, "network": "10GbE"},
    "hypothesis": "Verify network throughput scales linearly with thread count",
    "benchmark_suite": "uperf",
    "absent_suite": False,
    "required_hosts": [
        {"roles": ["controller"]},
        {"roles": ["client"]},
        {"roles": ["server"]},
    ],
    "notes": "Standard uperf TCP stream test",
}

MOCK_RESOURCE_RESULT = {
    "assigned_hardware_ips": {
        "controller": "10.0.5.2",
        "targets": ["10.0.5.3", "10.0.5.4"],
    },
    "ssh_user": "root",
    "ssh_key_path": "~/.ssh/id_rsa",
    "lease_expiration": "2026-06-09T00:00:00Z",
    "validation_results": {
        "10.0.5.2": {"reachable": True, "message": "Host validated (simulated)"},
        "10.0.5.3": {"reachable": True, "message": "Host validated (simulated)"},
        "10.0.5.4": {"reachable": True, "message": "Host validated (simulated)"},
    },
    "notes": "All hosts validated successfully",
}

MOCK_PROVISIONING_RESULT = {
    "provisioning_complete": True,
    "hosts_provisioned": ["10.0.5.2", "10.0.5.3", "10.0.5.4"],
    "crucible_version": "crucible-main-abc1234",
    "configuration_applied": {
        "10.0.5.2": ["podman verified", "crucible installed", "controller configured"],
        "10.0.5.3": ["podman verified", "crucible installed", "endpoint configured"],
        "10.0.5.4": ["podman verified", "crucible installed", "endpoint configured"],
    },
    "notes": "All hosts provisioned and verified",
}

MOCK_BENCHMARK_RESULT = {
    "run_id": "RUN-20260608-a1b2c3",
    "benchmark_status": "completed",
    "run_file_used": {
        "benchmarks": [
            {
                "name": "uperf",
                "ids": "1",
                "mv-params": {"test-types": "stream", "protocols": "tcp"},
            }
        ],
        "endpoints": [
            {"type": "remotehosts", "host": "10.0.5.3", "user": "root", "client": "1"},
            {"type": "remotehosts", "host": "10.0.5.4", "user": "root", "server": "1"},
        ],
        "tags": {"run": "network-scaling-test"},
    },
    "benchmark_duration": 324,
    "notes": "Benchmark completed successfully across 3 iterations",
}

MOCK_REVIEW_RESULT = {
    "review_summary": "Network throughput scales at 0.94x per thread (near-linear) — hypothesis confirmed",
    "verdict": "hypothesis_confirmed",
    "detailed_analysis": (
        "## Performance Analysis\n\n"
        "**Benchmark:** uperf TCP stream\n"
        "**Duration:** 324 seconds (3 iterations)\n\n"
        "### Key Findings\n\n"
        "- **Throughput:** 9.42 Gbps mean (std: 0.25 Gbps)\n"
        "- **Latency:** P50=42us, P90=78us, P95=124us, P99=312us\n"
        "- **CPU utilization:** 34.2% mean, 87.1% max (16 cores)\n"
        "- **Scaling efficiency:** 0.94x per thread (near-linear)\n\n"
        "### Conclusion\n\n"
        "The hypothesis that throughput scales linearly with thread count is **confirmed**. "
        "Scaling efficiency of 0.94x is well within expected bounds for TCP stream workloads. "
        "P99 latency of 312us is acceptable but shows some tail effects at high thread counts."
    ),
    "key_metrics": {
        "throughput": {"value": 9.42, "unit": "Gbps", "assessment": "good"},
        "latency_p99": {"value": 312, "unit": "usec", "assessment": "good"},
        "cpu_utilization": {"value": 34.2, "unit": "%", "assessment": "good"},
    },
    "recommendations": [
        "Test with UDP to compare protocol overhead",
        "Increase thread count to 32 to find scaling ceiling",
        "Run with message sizes 64B-64KB to profile latency vs throughput tradeoff",
    ],
    "follow_up_needed": False,
}

AGENT_MOCK_RESPONSES = {
    "triage-agent": MOCK_TRIAGE_RESULT,
    "resource-agent": MOCK_RESOURCE_RESULT,
    "provisioning-agent": MOCK_PROVISIONING_RESULT,
    "benchmark-agent": MOCK_BENCHMARK_RESULT,
    "review-agent": MOCK_REVIEW_RESULT,
}


class MockLLMProvider(LLMProvider):
    """Returns canned responses for testing without an API key.

    When agent_name is set, returns the appropriate mock for that agent.
    Otherwise falls back to the triage mock.
    """

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        agent_name: str | None = None,
    ) -> None:
        self._responses = list(responses) if responses else []
        self._call_count = 0
        self._agent_name = agent_name

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse:
        if self._responses:
            resp = self._responses[min(self._call_count, len(self._responses) - 1)]
            self._call_count += 1
            return resp

        self._call_count += 1

        agent = self._agent_name or self._detect_agent(system_prompt)
        mock_data = AGENT_MOCK_RESPONSES.get(agent, MOCK_TRIAGE_RESULT)

        submit_tool_names = {
            "triage-agent": "submit_triage_result",
            "resource-agent": "submit_resource_result",
            "provisioning-agent": "submit_provisioning_result",
            "benchmark-agent": "submit_benchmark_result",
            "review-agent": "submit_review_result",
        }
        tool_name = submit_tool_names.get(agent, "submit_result")

        return LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc_submit", name=tool_name, input=mock_data)],
            stop_reason="tool_use",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": "tc_submit",
                    "name": tool_name,
                    "input": mock_data,
                }
            ],
        )

    @staticmethod
    def _detect_agent(system_prompt: str) -> str:
        prompt_lower = system_prompt.lower()
        if "triage agent" in prompt_lower:
            return "triage-agent"
        if "review agent" in prompt_lower:
            return "review-agent"
        if "benchmark agent" in prompt_lower:
            return "benchmark-agent"
        if "provisioning agent" in prompt_lower:
            return "provisioning-agent"
        if "resource agent" in prompt_lower:
            return "resource-agent"
        return "triage-agent"
