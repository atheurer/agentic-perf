from __future__ import annotations

from providers.llm.base import ToolDefinition


def get_retrospective_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_transcript_analysis",
            description=(
                "Read and pre-process the transcript for a completed ticket. "
                "Returns detected signals (tool errors, retry sequences, "
                "fail-then-succeed patterns, max_iterations hits, HITL "
                "escalations, self-correction language) with surrounding "
                "raw events as context, plus per-agent statistics."
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
            name="submit_retrospective",
            description=(
                "Submit the retrospective analysis. Call this after "
                "classifying all signals from the transcript."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "description": "List of classified findings",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "tool_defect",
                                        "skill_gap",
                                        "schema_mismatch",
                                        "prompt_gap",
                                        "convergence_failure",
                                        "deviation",
                                        "misuse",
                                    ],
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                },
                                "agent": {
                                    "type": "string",
                                    "description": ("Which agent was affected"),
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Human-readable description of the finding"
                                    ),
                                },
                                "recommended_action": {
                                    "type": "string",
                                    "enum": [
                                        "code_fix",
                                        "skill_doc",
                                        "schema_update",
                                        "prompt_update",
                                        "investigation",
                                    ],
                                },
                            },
                            "required": [
                                "category",
                                "severity",
                                "agent",
                                "description",
                                "recommended_action",
                            ],
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": ("One-paragraph summary of the retrospective"),
                    },
                    "stats": {
                        "type": "object",
                        "description": "Aggregate statistics",
                        "properties": {
                            "total_events": {"type": "integer"},
                            "total_tool_errors": {"type": "integer"},
                            "total_retries": {"type": "integer"},
                            "hitl_count": {"type": "integer"},
                            "estimated_wasted_iterations": {
                                "type": "integer",
                            },
                        },
                    },
                },
                "required": ["findings", "summary", "stats"],
            },
        ),
    ]
