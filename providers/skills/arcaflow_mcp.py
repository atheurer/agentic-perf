"""Arcaflow remote MCP registration and harness ownership contract.

The upstream Arcaflow MCP server does not attach ownership metadata to tools.
This adapter therefore declares the supported upstream tool groups at the
integration boundary. Ownership controls dispatch, while ``visible_to_model``
keeps raw workflow execution and polling operations available to the local
workflow adapter without exposing them as direct model calls.
"""

from __future__ import annotations

from typing import Any

PLUGIN_LIST = "plugin_list"
PLUGIN_DESCRIBE = "plugin_describe"

WORKFLOW_LOAD = "workflow_load"
WORKFLOW_LIST = "workflow_list"
WORKFLOW_INPUT_BUILD = "workflow_input_build"
WORKFLOW_INPUT_VALIDATE = "workflow_input_validate"
WORKFLOW_INPUT_EXPORT = "workflow_input_export"
WORKFLOW_EXECUTE = "workflow_execute"
WORKFLOW_EXECUTION_STATUS = "workflow_execution_status"
WORKFLOW_EXECUTION_CANCEL = "workflow_execution_cancel"
WORKFLOW_EXECUTION_OUTPUT = "workflow_execution_output"


_ARCAFLOW_TOOL_GROUPS = (
    (
        "arcaflow-plugins",
        (PLUGIN_LIST, PLUGIN_DESCRIBE),
        True,
    ),
    (
        "arcaflow-workflows",
        (WORKFLOW_LOAD, WORKFLOW_LIST, WORKFLOW_INPUT_BUILD, WORKFLOW_INPUT_VALIDATE),
        True,
    ),
    (
        "arcaflow-workflows",
        (
            WORKFLOW_INPUT_EXPORT,
            WORKFLOW_EXECUTE,
            WORKFLOW_EXECUTION_STATUS,
            WORKFLOW_EXECUTION_CANCEL,
            WORKFLOW_EXECUTION_OUTPUT,
        ),
        False,
    ),
)


def register_arcaflow_tool_ownership(
    client: Any, server_name: str = "arcaflow"
) -> None:
    """Register the upstream Arcaflow tools under their owning adapters."""
    client.require_tool_ownership(server_name)
    for owner_harness, tool_names, visible_to_model in _ARCAFLOW_TOOL_GROUPS:
        for tool_name in tool_names:
            client.register_tool_owner(
                server_name,
                tool_name,
                owner_harness,
                visible_to_model=visible_to_model,
            )
