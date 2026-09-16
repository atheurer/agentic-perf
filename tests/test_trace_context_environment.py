from __future__ import annotations

from providers.tracing import (
    new_trace_context,
    trace_context_environment,
    trace_context_from_environment,
)


def test_ticket_mcp_child_restores_parent_trace_context(monkeypatch):
    context = new_trace_context(ticket_id="PERF-12345678", agent_id="benchmark-agent")
    for key, value in trace_context_environment(context).items():
        monkeypatch.setenv(key, value)

    restored = trace_context_from_environment(
        ticket_id="PERF-12345678", agent_id="benchmark-agent"
    )

    assert restored == context
