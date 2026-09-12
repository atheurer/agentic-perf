"""AST inventory for local subprocess boundaries (not strings in remote scripts)."""

from __future__ import annotations

import ast
from pathlib import Path

_TARGETS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
}


class _SubprocessCalls(ast.NodeVisitor):
    """Find direct subprocess calls while ignoring text in remote scripts."""

    def __init__(self) -> None:
        self.modules: dict[str, str] = {}
        self.direct: dict[str, str] = {}
        self.function: list[str] = ["<module>"]
        self.calls: list[tuple[str, int, str | None]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in {"asyncio", "subprocess"}:
                self.modules[alias.asname or alias.name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module not in {"asyncio", "subprocess"}:
            return
        for alias in node.names:
            self.direct[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def _qualified(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.direct.get(node.id) or self.modules.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self._qualified(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    @staticmethod
    def _executable(node: ast.Call) -> str | None:
        if not node.args:
            return None
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            item = first.elts[0]
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                return item.value
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function.append(node.name)
        self.generic_visit(node)
        self.function.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified(node.func)
        if qualified in _TARGETS:
            self.calls.append((self.function[-1], node.lineno, self._executable(node)))
        self.generic_visit(node)


def test_no_unexplained_local_subprocess_calls() -> None:
    root = Path(__file__).parents[1]
    allowed = {"providers/execution/subprocess.py", "providers/ssh.py"}
    findings: list[str] = []
    exceptions = {
        ("agents/infra/server.py", "transfer_file"),
        ("agents/provisioning/agent.py", "_ssh_run"),
    }
    for directory in ("agents", "orchestrator", "providers", "state_store"):
        for path in (root / directory).rglob("*.py"):
            relative = str(path.relative_to(root))
            if relative in allowed:
                continue
            visitor = _SubprocessCalls()
            visitor.visit(ast.parse(path.read_text()))
            for function, line, executable in visitor.calls:
                # Exact SSH transport literals are owned by #789.
                if (
                    executable in {"ssh", "scp", "sshpass"}
                    or (
                        relative,
                        function,
                    )
                    in exceptions
                ):
                    continue
                findings.append(f"{relative}:{line}:{function}")
    assert findings == []
