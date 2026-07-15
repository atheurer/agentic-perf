from __future__ import annotations

from providers.llm.base import ToolDefinition


def get_introspection_tools() -> list[ToolDefinition]:
    """Return tool definitions for the introspection agent.

    Phase 1 tools are read-only observation tools. Future phases
    will add intervention tools (soft-stop, comment injection).
    """
    return [
        ToolDefinition(
            name="get_ticket_events",
            description=(
                "Fetch recent events from a ticket's event stream. "
                "Returns JSONL events with agent activity, tool calls, "
                "LLM responses, and state transitions. Use the 'since' "
                "parameter to poll for new events incrementally."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to observe",
                    },
                    "since": {
                        "type": "integer",
                        "description": (
                            "Return events with seq > this value. "
                            "Use 0 for all events, or the last seen "
                            "seq to poll incrementally."
                        ),
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of events to return. Default 100."
                        ),
                        "default": 100,
                    },
                },
                "required": ["ticket_id"],
            },
        ),
        ToolDefinition(
            name="get_ticket_status",
            description=(
                "Get the current status and metadata of a ticket "
                "without the full event stream. Returns status, "
                "summary, active agent, and key custom fields."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to check",
                    },
                },
                "required": ["ticket_id"],
            },
        ),
        ToolDefinition(
            name="get_token_usage",
            description=(
                "Get cumulative LLM token usage for a ticket, "
                "broken down by agent. Useful for detecting "
                "token waste or budget burn rate."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to check",
                    },
                },
                "required": ["ticket_id"],
            },
        ),
        ToolDefinition(
            name="detect_anomalies",
            description=(
                "Analyze a ticket's event stream for anomalous "
                "patterns: retry loops, repeated errors, stalled "
                "progress, excessive iterations, and token waste. "
                "Returns a structured list of detected anomalies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to analyze",
                    },
                },
                "required": ["ticket_id"],
            },
        ),
        ToolDefinition(
            name="submit_observation",
            description=(
                "Submit the introspection observation for a ticket. "
                "Call this when you have completed your analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID observed",
                    },
                    "narrative": {
                        "type": "string",
                        "description": (
                            "Running narrative of what the ticket's "
                            "agents have been doing"
                        ),
                    },
                    "anomalies": {
                        "type": "array",
                        "description": "Detected anomalies",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "retry_loop",
                                        "repeated_error",
                                        "stalled_progress",
                                        "excessive_iterations",
                                        "token_waste",
                                        "unexpected_transition",
                                    ],
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Human-readable description of the anomaly"
                                    ),
                                },
                                "seq_range": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": (
                                        "Event sequence numbers where this was observed"
                                    ),
                                },
                            },
                            "required": [
                                "type",
                                "severity",
                                "description",
                            ],
                        },
                    },
                    "status_summary": {
                        "type": "string",
                        "description": (
                            "Current status summary: where the ticket "
                            "stands and what remains"
                        ),
                    },
                },
                "required": [
                    "ticket_id",
                    "narrative",
                    "anomalies",
                    "status_summary",
                ],
            },
        ),
    ]
