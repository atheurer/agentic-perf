from __future__ import annotations

from providers.llm.base import ToolDefinition

WORKSPACE_TOOLS = [
    ToolDefinition(
        name="jq_query",
        description="Execute a jq filter expression on a structured JSON workspace file to extract keys, arrays, or compute aggregated values.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename (e.g. 'workspace://cdm_ts.json')",
                },
                "filter": {
                    "type": "string",
                    "description": "jq expression (e.g. '.uperf_100.values' or '.[] | {name, status}')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum list items to return in result (default 50)",
                    "default": 50,
                },
            },
            "required": ["file_ref", "filter"],
        },
    ),
    ToolDefinition(
        name="grep_file",
        description="Search for a string or regex pattern in a workspace text file.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename (e.g. 'workspace://ethtool_stats.txt')",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum matching lines to return (default 50)",
                    "default": 50,
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before and after each match (default 0)",
                    "default": 0,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive match (default True)",
                    "default": True,
                },
            },
            "required": ["file_ref", "pattern"],
        },
    ),
    ToolDefinition(
        name="read_file_slice",
        description="Read a slice or chunk of a workspace file by lines or bytes.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename",
                },
                "offset_bytes": {
                    "type": "integer",
                    "description": "Byte offset to start reading from (default 0)",
                    "default": 0,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read (default 4096)",
                    "default": 4096,
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to start reading from",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Optional maximum number of lines to read",
                },
            },
            "required": ["file_ref"],
        },
    ),
    ToolDefinition(
        name="list_workspace_files",
        description="List all files stored in the ticket's scratchpad workspace directory.",
        input_schema={
            "type": "object",
            "properties": {},
        },
    ),
]
