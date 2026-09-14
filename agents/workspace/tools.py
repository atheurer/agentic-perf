from __future__ import annotations

from providers.llm.base import ToolDefinition

WORKSPACE_TOOLS = [
    ToolDefinition(
        name="jq_file_from_workspace",
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
                "include_alternates": {
                    "type": "boolean",
                    "description": "Explicit drift/comparison access to alternate source snapshots (default false)",
                    "default": False,
                },
            },
            "required": ["file_ref", "filter"],
        },
    ),
    ToolDefinition(
        name="grep_file_from_workspace",
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
                "include_alternates": {
                    "type": "boolean",
                    "description": "Explicit drift/comparison access to alternate source snapshots (default false)",
                    "default": False,
                },
            },
            "required": ["file_ref", "pattern"],
        },
    ),
    ToolDefinition(
        name="read_file_from_workspace",
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
                "include_alternates": {
                    "type": "boolean",
                    "description": "Explicit drift/comparison access to alternate source snapshots (default false)",
                    "default": False,
                },
            },
            "required": ["file_ref"],
        },
    ),
    ToolDefinition(
        name="list_files_from_workspace",
        description="List files visible to this agent's default audience in the ticket workspace. Alternate source snapshots require explicit comparison access.",
        input_schema={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="read_document_from_workspace",
        description=(
            "Read an exact context document previously inventoried by a context "
            "gateway into the ticket workspace. Pass a logical ref or URI returned "
            "by the gateway; the current phase-effective source is used by default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Logical document ref or URI returned by a context gateway",
                },
                "include_alternates": {
                    "type": "boolean",
                    "description": "Allow an explicitly indexed alternate source (default false)",
                    "default": False,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum document bytes to return (default 262144)",
                    "default": 262144,
                },
            },
            "required": ["ref"],
        },
    ),
    ToolDefinition(
        name="search_documents_from_workspace",
        description=(
            "Regex-search paths and contents in the context inventory already "
            "materialized in the ticket workspace. Search is restricted to the "
            "current phase-effective source unless alternates are explicitly enabled."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Regular expression to search for",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional logical namespace prefix, such as benchmark/perftest",
                    "default": "",
                },
                "include_alternates": {
                    "type": "boolean",
                    "description": "Search explicitly indexed alternate sources (default false)",
                    "default": False,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Use case-insensitive matching (default true)",
                    "default": True,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching documents to return (default 50)",
                    "default": 50,
                },
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="generate_chart_from_workspace",
        description="Extract and generate a declarative Chart.js/Recharts performance chart from a workspace JSON or CSV file without needing to output raw numbers or code. Supports single-metric charts as well as multi-metric stacked panels with synchronized X-axis timelines (e.g. uperf throughput + mpstat CPU busy). Automatically saves chart specification to workspace://charts/<output_name>.json and returns chart summary.",
        input_schema={
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": "workspace:// URI or relative filename (e.g. 'workspace://cdm_metric_1.json')",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title (e.g. 'Network Throughput & CPU Utilization' or 'Server CPU Busy % by Core')",
                },
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "doughnut"],
                    "description": "Chart type (default 'line' for timeseries, 'bar' for breakouts)",
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
                    "description": "Metric name for CDM/Crucible data (e.g. 'mpstat::Busy-CPU' or 'uperf::Gbps'). Can also be comma-separated list of metrics.",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of multiple metric names (e.g. ['uperf::Gbps', 'mpstat::Busy-CPU']) to generate synchronized stacked chart panels.",
                },
                "breakout": {
                    "type": "string",
                    "description": "Optional CDM breakout field to visualize",
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
