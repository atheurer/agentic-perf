from __future__ import annotations

import ast
from pathlib import Path


def test_ticket_agents_do_not_use_unscoped_local_mcp_connect():
    agents_root = Path(__file__).parents[1] / "agents"
    violations = []
    for path in sorted(agents_root.glob("*/agent.py")):
        relative = path.relative_to(agents_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                continue
            function = node.value.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "mcp"
                and function.attr == "connect"
            ):
                violations.append(f"{relative.as_posix()}:{node.lineno}")

    assert violations == [], (
        "Ticket agents must use connect_ticket_server for local MCP servers: "
        + ", ".join(violations)
    )
