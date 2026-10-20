"""Harness ownership metadata and enforcement for benchmark tools."""

from __future__ import annotations

from typing import Any

TOOL_OWNERSHIP_META_KEY = "agentic-perf-tool-ownership"


class ToolOwnershipError(PermissionError):
    """Raised when a ticket invokes a tool owned by another harness."""


def ownership_metadata(
    owner_harness: str, *, visible_to_model: bool = True
) -> dict[str, Any]:
    """Return MCP metadata declaring the harness that owns a tool."""
    if not owner_harness:
        raise ValueError("owner_harness must be a non-empty string")
    return {
        TOOL_OWNERSHIP_META_KEY: {
            "harness": owner_harness,
            "visible_to_model": visible_to_model,
        }
    }


def owner_harness_from_tool(tool: Any) -> str | None:
    """Read a harness owner from an MCP tool's public metadata."""
    metadata = getattr(tool, "meta", None)
    if not isinstance(metadata, dict):
        metadata = getattr(tool, "_meta", None)
    if not isinstance(metadata, dict):
        return None

    ownership = metadata.get(TOOL_OWNERSHIP_META_KEY)
    if ownership is None:
        return None
    if not isinstance(ownership, dict):
        raise ValueError("tool ownership metadata must be an object")
    owner = ownership.get("harness")
    if not isinstance(owner, str) or not owner:
        raise ValueError("tool ownership metadata requires a harness")
    return owner


def tool_visible_to_model(tool: Any) -> bool:
    """Return the model-visibility flag from ownership metadata."""
    metadata = getattr(tool, "meta", None)
    if not isinstance(metadata, dict):
        metadata = getattr(tool, "_meta", None)
    if not isinstance(metadata, dict):
        return True

    ownership = metadata.get(TOOL_OWNERSHIP_META_KEY)
    if not isinstance(ownership, dict):
        return True
    visible = ownership.get("visible_to_model", True)
    if not isinstance(visible, bool):
        raise ValueError("tool ownership visibility must be a boolean")
    return visible


def selected_harness_from_ticket(ticket: dict[str, Any]) -> str:
    """Resolve an explicitly persisted harness selection from this ticket."""
    from providers.skills.base import HARNESS_ALIASES, WORKFLOW_HARNESS

    custom_fields = ticket.get("custom_fields", {})
    if not isinstance(custom_fields, dict):
        custom_fields = {}
    directives = custom_fields.get("directives", {})
    if not isinstance(directives, dict):
        directives = {}
    if directives.get("workflow_source"):
        return WORKFLOW_HARNESS

    selected = directives.get("harness") or custom_fields.get("harness")
    if not isinstance(selected, str):
        return ""
    return HARNESS_ALIASES.get(selected, selected)


def require_ticket_harness(
    ticket: dict[str, Any],
    owner_harness: str,
) -> None:
    """Reject an owned handler unless the server-loaded ticket selects it."""
    if not owner_harness:
        raise ValueError("owner_harness must be a non-empty string")
    selected_harness = selected_harness_from_ticket(ticket)
    if selected_harness != owner_harness:
        raise ToolOwnershipError(
            f"Tool requires ticket harness {owner_harness!r}; "
            f"selected harness is {selected_harness or 'unset'!r}"
        )
