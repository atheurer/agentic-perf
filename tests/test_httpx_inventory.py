from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    "agents/chat/agent.py",
    "agents/mcp_client.py",
    "orchestrator/poller.py",
    "providers/execution/http.py",
    "providers/skills/arcaflow_plugins.py",
    "providers/skills/crucible.py",
    "providers/tracing/client.py",
}
EXCLUDED_CALLS = {"orchestrator/dispatcher.py": {135}}


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
            unexpected = set(_direct_httpx_calls(path))
            if relative in EXCLUDED:
                unexpected = set()
            else:
                unexpected -= EXCLUDED_CALLS.get(relative, set())
            if unexpected:
                offenders.append(f"{relative}:{sorted(unexpected)}")
    assert not offenders, f"migrate or document direct httpx callers: {offenders}"
    for relative in EXCLUDED:
        assert f"`{relative}`" in documented
    for relative, lines in EXCLUDED_CALLS.items():
        for line in lines:
            assert f"`{relative}:{line}`" in documented
