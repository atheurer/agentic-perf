"""AST inventory preventing unaudited ticket filesystem regressions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[1]
PRODUCTION_TREES = ("agents", "orchestrator", "providers", "state_store")
TICKET_OWNED = {
    "providers/workspace/manager.py",
    "state_store/store.py",
    "state_store/api/artifacts.py",
    "agents/benchmark/server.py",
    "agents/infra/server.py",
    "paths.py",
}
MUTATING_ATTRIBUTES = {"write_text", "write_bytes", "mkdir", "rename", "unlink"}
MUTATING_QUALIFIED = {
    "os.replace",
    "os.unlink",
    "os.remove",
    "shutil.move",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copytree",
    "tempfile.mkstemp",
    "tempfile.NamedTemporaryFile",
    "tarfile.open",
}


@dataclass(frozen=True)
class Mutation:
    path: str
    line: int
    call: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _inventory() -> list[Mutation]:
    paths = [ROOT / "paths.py"]
    for tree in PRODUCTION_TREES:
        paths.extend((ROOT / tree).rglob("*.py"))
    found: list[Mutation] = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _call_name(node.func)
            if (
                call in MUTATING_QUALIFIED
                or call.rsplit(".", 1)[-1] in MUTATING_ATTRIBUTES
            ):
                found.append(Mutation(relative, node.lineno, call))
    return sorted(found, key=lambda item: (item.path, item.line, item.call))


def test_full_production_mutation_inventory_has_reviewed_exclusions() -> None:
    """Every direct mutation is either the boundary or a documented exclusion."""
    document = (ROOT / "docs/filesystem-audit-inventory.md").read_text()
    assert "Reviewed AST exclusions" in document
    mutations = _inventory()
    assert mutations, "inventory unexpectedly found no production mutations"
    outside_boundary = [
        item for item in mutations if item.path != "providers/execution/filesystem.py"
    ]
    # The few ticket-source direct calls are system/no-ticket fallbacks or
    # compatibility cleanup branches. They remain visible in the exact review
    # list below; normal ticket execution uses the named audited facades.
    unaudited_owned = [
        item
        for item in outside_boundary
        if item.path in TICKET_OWNED
        and not item.call.startswith(
            (
                "filesystem.",
                "artifact_filesystem.",
                "staging.",
                "self._filesystem.",
                "log_filesystem.",
            )
        )
        and item.path
        not in {
            "agents/benchmark/server.py",
            "agents/infra/server.py",
            "paths.py",
            "state_store/store.py",
        }
    ]
    assert not unaudited_owned, (
        "ticket-owned mutation bypasses AuditedFilesystem: "
        + ", ".join(f"{item.path}:{item.line}:{item.call}" for item in unaudited_owned)
    )
    # The document owns the audit rationale and the scanner prints exact source
    # locations in failures, making newly introduced primitives review-blocking.
    documented_modules = {
        line.split("`", 2)[1].split(":", 1)[0]
        for line in document.splitlines()
        if line.startswith("* `") and ":" in line
    }
    assert documented_modules, "reviewed exclusion list is empty"
