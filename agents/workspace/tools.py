from __future__ import annotations

from providers.llm.base import ToolDefinition

WORKSPACE_TOOLS = [
    ToolDefinition(
        name="jq_query",
        description="Execute a jq filter expression on a structured JSON workspace file to extract keys, arrays, or compute aggregated values. For large arrays, use slice ranges (e.g. '.values[0:50]', next chunk: '.values[50:100]') to paginate.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename (e.g. 'workspace://cdm_ts.json')",
                },
                "filter": {
                    "type": "string",
                    "description": "jq expression (e.g. '.uperf_100.values[0:50]' or '.[] | {name, status}')",
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
        description="Read a slice or chunk of a workspace file by lines or bytes. Returns 'next_start_line' and 'next_offset_bytes' to easily fetch the next chunk without re-reading previous data.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename",
                },
                "offset_bytes": {
                    "type": "integer",
                    "description": "Byte offset to start reading from. Set to previous result's 'next_offset_bytes' for the next chunk.",
                    "default": 0,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read (default 4096)",
                    "default": 4096,
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to start reading from. Set to previous result's 'next_start_line' for the next chunk.",
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
    ToolDefinition(
        name="generate_chart_from_workspace",
        description="Extract and generate a declarative Chart.js/Recharts performance chart from a workspace JSON or CSV file without needing to output raw numbers or code. Automatically saves chart specification to workspace://charts/<output_name>.json and returns chart summary.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename (e.g. 'workspace://cdm_metric_1.json')",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title (e.g. 'Server CPU Busy % by Core' or 'Throughput vs Thread Count')",
                },
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "doughnut"],
                    "description": "Chart type (default 'bar')",
                    "default": "bar",
                },
                "harness": {
                    "type": "string",
                    "description": "Optional benchmark harness name ('crucible', 'kube-burner', 'k8s-netperf', etc.) for harness-specific parsing",
                },
                "output_name": {
                    "type": "string",
                    "description": "Optional output JSON filename under workspace://charts/ (e.g. 'cpu_busy')",
                },
                "x_field": {
                    "type": "string",
                    "description": "Field name for X-axis labels (e.g. 'cpu', 'threads', 'time')",
                },
                "y_field": {
                    "type": "string",
                    "description": "Field name for Y-axis numeric values (e.g. 'busy_pct', 'gbps', 'iops')",
                },
                "group_by": {
                    "type": "string",
                    "description": "Field name to group multiple series by (e.g. 'host', 'queue')",
                },
                "metric": {
                    "type": "string",
                    "description": "Metric name for CDM/Crucible data (e.g. 'mpstat::Busy-CPU' or 'uperf::Gbps')",
                },
                "unit": {
                    "type": "string",
                    "description": "Metric unit (e.g. 'Gbps', '%', 'IOPS', 'ms')",
                },
                "max_points": {
                    "type": "integer",
                    "description": "Maximum data points to plot for line charts (default 60)",
                    "default": 60,
                },
                "jq_filter": {
                    "type": "string",
                    "description": "Optional in-flight jq expression to filter file content before charting",
                },
            },
            "required": ["file_ref"],
        },
    ),
]
