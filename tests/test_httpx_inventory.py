from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    "agents/chat/agent.py",
    "agents/chat/tools.py",
    "agents/jumpstarter_mcp.py",
    "agents/mcp_client.py",
    "agents/server_utils.py",
    "agents/stub.py",
    "orchestrator/dispatcher.py",
    "orchestrator/main.py",
    "orchestrator/poller.py",
    "providers/execution/http.py",
    "providers/skills/arcaflow_plugins.py",
    "providers/skills/crucible.py",
    "providers/tracing/client.py",
}


def _direct_httpx_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "httpx"
            and node.func.attr
            in {"AsyncClient", "Client", "get", "post", "put", "patch", "delete"}
        ):
            calls.append(node.lineno)
    return calls


def test_ticket_httpx_inventory_is_complete_and_documented() -> None:
    documented = (ROOT / "docs/http-audit-inventory.md").read_text()
    offenders = []
    for top in ("agents", "orchestrator", "providers"):
        for path in (ROOT / top).rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if _direct_httpx_calls(path) and relative not in EXCLUDED:
                offenders.append(relative)
    assert not offenders, f"migrate or document direct httpx callers: {offenders}"
    for relative in EXCLUDED:
        assert f"`{relative}`" in documented
