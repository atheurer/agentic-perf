"""CI guardrail for auditable MCP and native agent tool registrations."""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from agents.side_effect_inventory import (
    INVENTORIED_SIDE_EFFECTS,
    INVENTORY_DISPOSITIONS,
)
from agents.tool_audit_policy import (
    AUDIT_BYPASS_ALLOWLIST,
    OPERATION_OWNER_CONTRACTS,
    POLICY_BY_REGISTRATION,
    REGISTRATION_DISCOVERY_EXCEPTIONS,
    TOOL_AUDIT_POLICY,
)
from providers.tracing import (
    LifecycleState,
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)

ROOT = Path(__file__).parents[1]
SERVER_ROOT = ROOT / "agents"

# Calls in a handler that always mutate local state.  This deliberately avoids
# vague names such as ``run``: a fixed remote read command is still read-only.
_PROTECTED_MUTATORS = {
    "open",
    "os.open",
    "os.mkdir",
    "os.makedirs",
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "subprocess.run",
    "subprocess.Popen",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
_PROTECTED_METHODS = {
    "chmod",
    "chown",
    "mkdir",
    "rename",
    "rmdir",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}


@dataclass(frozen=True, order=True)
class Registration:
    key: str
    path: str
    function: str
    line: int
    kind: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Resolve imports before deciding whether a tool reaches a protected API."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _resolved_call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
        constructor = _resolved_call_name(node.value.func, aliases)
        if constructor == "pathlib.Path" and node.attr == "open":
            return f"{constructor}.{node.attr}"
    raw = _call_name(node)
    root, dot, rest = raw.partition(".")
    return f"{aliases.get(root, root)}{dot}{rest}" if raw else raw


def _enclosing_symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current.name
    return "<module>"


def _side_effect_category(path: str, call: str) -> str | None:
    """Classify a sensitive call after resolving its import spelling.

    The categories deliberately describe capability domains rather than a
    particular package spelling.  This catches ``import os as operating`` and
    ``from asyncio import create_subprocess_exec as spawn`` just as it catches
    their canonical spellings.
    """
    lowered = f"{path}:{call}".lower()
    leaf = call.rsplit(".", 1)[-1]
    if "mcp" in lowered and ("call_tool" in lowered or "fastmcp" in lowered):
        return "mcp"
    if "ssh" in lowered and leaf in {"run", "copy_to", "copy_from", "connect"}:
        return "ssh"
    if "image_build" in path or "image_builder" in path:
        return "image"
    if "resource/" in path or "boto3" in lowered or "ec2" in lowered:
        return "cloud_resource"
    if "github" in lowered or "repo_cache" in path:
        return "github"
    if leaf in {
        "run",
        "Popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "start",
    } and ("subprocess" in lowered or "auditedsubprocess" in lowered):
        return "subprocess"
    if (
        leaf
        in _PROTECTED_METHODS
        | {
            "open",
            "makedirs",
            "remove",
            "replace",
        }
        or call in _PROTECTED_MUTATORS
    ):
        return "filesystem"
    if leaf in {"post", "put", "patch", "delete"}:
        return "mutating_http_state"
    return None


def _discover_side_effect_inventory() -> set[tuple[str, str, str]]:
    """Return all capability boundaries in production code, never test code."""
    paths: list[Path] = []
    for candidate in ("agents", "orchestrator", "providers", "state_store"):
        paths.extend((ROOT / candidate).rglob("*.py"))
    paths.append(ROOT / "cli.py")
    discovered: set[tuple[str, str, str]] = set()
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        aliases = _import_aliases(tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            category = _side_effect_category(
                relative, _resolved_call_name(node.func, aliases)
            )
            if category:
                discovered.add((relative, _enclosing_symbol(node, parents), category))
    return discovered


def _tool_name(node: ast.Call, default: str) -> str:
    for keyword in node.keywords:
        if (
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return default


def _factory_aliases(tree: ast.AST) -> set[str]:
    """Return every local spelling of the canonical audited MCP factory."""
    names = {"create_ticket_mcp"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "agents.mcp_audit":
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "create_ticket_mcp"
            )
        elif isinstance(node, ast.Import):
            names.update(
                f"{alias.asname or alias.name}.create_ticket_mcp"
                for alias in node.names
                if alias.name == "agents.mcp_audit"
            )
    return names


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Return simple names bound by an assignment without guessing attributes."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _assignment_handler_targets(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Return named and attribute map targets without resolving arbitrary objects."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [
        target.id if isinstance(target, ast.Name) else target.attr
        for target in targets
        if isinstance(target, (ast.Name, ast.Attribute))
    ]


def _literal_tool_name(decorator: ast.expr, default: str) -> tuple[str, str | None]:
    """Resolve a FastMCP decorator name or report an unreviewable dynamic name."""
    if not isinstance(decorator, ast.Call):
        return default, None
    for keyword in decorator.keywords:
        if keyword.arg != "name":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(
            keyword.value.value, str
        ):
            return keyword.value.value, None
        return default, "dynamic MCP advertised name"
    # FastMCP accepts its advertised name as the first positional argument.
    if decorator.args:
        candidate = decorator.args[0]
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value, None
        return default, "dynamic MCP advertised name"
    return default, None


def _mcp_registrations_from_tree(
    relative: str, tree: ast.Module
) -> tuple[set[Registration], list[str]]:
    """Discover decorators through simple aliases and audited wrapper bindings.

    Registration is deliberately conservative.  When a decorator cannot be
    tied to a ``create_ticket_mcp`` instance at parse time, it is a CI failure;
    silently treating a dynamic wrapper as a local convention would reopen the
    unaudited-tool bypass this policy is intended to close.
    """
    registrations: set[Registration] = set()
    violations: list[str] = []
    audited_instances: set[str] = set()
    tool_decorators: set[str] = set()
    audited_wrappers: set[str] = set()

    # Resolve the intentionally small, local alias graph to a fixed point:
    # ``server = factory()``, ``alias = server``, and ``register = alias.tool``.
    # This covers ordinary aliases without pretending arbitrary code execution
    # or dynamically selected factories are statically auditable.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            names = _assignment_names(node)
            is_audited_instance = (
                isinstance(value, ast.Call)
                and _call_name(value.func) in _factory_aliases(tree)
            ) or (isinstance(value, ast.Name) and value.id in audited_instances)
            if is_audited_instance:
                before = len(audited_instances)
                audited_instances.update(names)
                changed |= len(audited_instances) != before
            is_tool_decorator = (
                (
                    isinstance(value, ast.Attribute)
                    and value.attr == "tool"
                    and _call_name(value.value) in audited_instances
                )
                or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "tool"
                    and _call_name(value.func.value) in audited_instances
                )
                or (isinstance(value, ast.Name) and value.id in tool_decorators)
            )
            if is_tool_decorator:
                before = len(tool_decorators)
                tool_decorators.update(names)
                changed |= len(tool_decorators) != before

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            returns_audited_tool = any(
                isinstance(descendant, ast.Return)
                and isinstance(descendant.value, ast.Call)
                and isinstance(descendant.value.func, ast.Attribute)
                and descendant.value.func.attr == "tool"
                and _call_name(descendant.value.func.value) in audited_instances
                for descendant in ast.walk(node)
            )
            if returns_audited_tool and node.name not in audited_wrappers:
                audited_wrappers.add(node.name)
                changed = True

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            callee = _call_name(call.func if call else decorator)
            receiver = ""
            is_tool = False
            if (
                call
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "tool"
            ):
                receiver = _call_name(call.func.value)
                is_tool = True
            elif callee in tool_decorators or callee in audited_wrappers:
                is_tool = True
                receiver = callee
            elif call and _call_name(call.func) in {"getattr", "builtins.getattr"}:
                if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                    if call.args[1].value == "tool":
                        violations.append(
                            f"{relative}:{node.lineno}:dynamic MCP tool registrar is not auditable"
                        )
                continue
            if not is_tool:
                continue
            audited = (
                receiver in audited_instances
                or receiver in tool_decorators
                or receiver in audited_wrappers
            )
            if not audited:
                violations.append(
                    f"{relative}:{node.lineno}:{node.name} decorates "
                    f"{receiver or '<dynamic>'}.tool outside create_ticket_mcp"
                )
            name, name_error = _literal_tool_name(decorator, node.name)
            if name_error:
                violations.append(f"{relative}:{node.lineno}:{name_error}")
            registrations.add(
                Registration(
                    key=f"{relative}:{name}",
                    path=relative,
                    function=node.name,
                    line=node.lineno,
                    kind="mcp",
                )
            )
    # A dynamic `getattr(mcp, "tool")` / decorator factory defeats static
    # inventory.  It must be made explicit instead of silently ignored.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) in {
            "getattr",
            "builtins.getattr",
            "setattr",
            "builtins.setattr",
        } and any(
            isinstance(arg, ast.Constant) and arg.value == "tool"
            for arg in node.args[1:2]
        ):
            marker = (
                f"{relative}:{node.lineno}:dynamic MCP tool registrar is not auditable"
            )
            if marker not in violations:
                violations.append(marker)
    return registrations, violations


def _mcp_registrations() -> tuple[set[Registration], list[str]]:
    registrations: set[Registration] = set()
    violations: list[str] = []
    # Every importable module under ``agents`` is a capability surface.  A
    # service need not be named ``server.py`` (and registration wrappers are
    # often placed beside an agent), so a filename convention must never hide
    # a new decorator from the policy inventory.
    for path in sorted(SERVER_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        found, errors = _mcp_registrations_from_tree(
            relative,
            ast.parse(path.read_text(encoding="utf-8"), filename=relative),
        )
        registrations.update(found)
        violations.extend(errors)
    return registrations, violations


def _tool_definition_aliases(tree: ast.AST) -> set[str]:
    """Return import and simple assignment spellings of ``ToolDefinition``."""
    aliases = {"ToolDefinition"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for item in node.names:
                    if item.name == "ToolDefinition":
                        before = len(aliases)
                        aliases.add(item.asname or item.name)
                        changed |= len(aliases) != before
                    elif node.module and node.module.startswith("providers.llm"):
                        # ``from providers.llm import base as llm_base``.
                        before = len(aliases)
                        aliases.add(f"{item.asname or item.name}.ToolDefinition")
                        changed |= len(aliases) != before
            elif isinstance(node, ast.Import):
                for item in node.names:
                    if item.name.startswith("providers.llm"):
                        before = len(aliases)
                        local = item.asname or item.name.split(".")[0]
                        aliases.add(f"{local}.ToolDefinition")
                        aliases.add(f"{item.asname or item.name}.ToolDefinition")
                        changed |= len(aliases) != before
            elif isinstance(node, ast.Assign):
                if _call_name(node.value) in aliases:
                    before = len(aliases)
                    aliases.update(_assignment_names(node))
                    changed |= len(aliases) != before
    return aliases


def _native_registrations_from_tree(
    relative: str, module: ast.Module
) -> tuple[set[Registration], list[str]]:
    """Discover literal native tools and fail closed on dynamic registrations."""
    registrations: set[Registration] = set()
    violations: list[str] = []
    aliases = _tool_definition_aliases(module)
    parents = {
        child: parent
        for parent in ast.walk(module)
        for child in ast.iter_child_nodes(parent)
    }
    exceptions = {
        (exception.path, exception.symbol)
        for exception in REGISTRATION_DISCOVERY_EXCEPTIONS
    }
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in aliases:
            continue
        name_keyword = next(
            (item for item in node.keywords if item.arg == "name"), None
        )
        if name_keyword is None:
            # ToolDefinition is also used for dynamically relaying already
            # registered external MCP schemas.  That is not a native
            # registration unless it is inserted into a local handler map.
            continue
        if not (
            isinstance(name_keyword.value, ast.Constant)
            and isinstance(name_keyword.value.value, str)
        ):
            symbol = _enclosing_symbol(node, parents)
            if (relative, symbol) not in exceptions:
                violations.append(
                    f"{relative}:{node.lineno}:dynamic native tool name is not auditable"
                )
            continue
        registrations.add(
            Registration(
                key=f"{relative}:{name_keyword.value.value}",
                path=relative,
                function="ToolDefinition",
                line=node.lineno,
                kind="native",
            )
        )

    # Native maps need not be named local_handlers and may be filled with
    # ``update`` after construction.  Discover explicit literal keys in both
    # forms so an alternate map cannot evade policy classification.
    for node in ast.walk(module):
        dictionary: ast.Dict | None = None
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Dict
        ):
            line = node.lineno
            targets = _assignment_handler_targets(node)
            if any(
                name.endswith("handlers") or name.endswith("tool_map")
                for name in targets
            ):
                dictionary = node.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"update", "setdefault"}
            and _call_name(node.func.value).endswith(("handlers", "tool_map"))
        ):
            line = node.lineno
            if node.args and isinstance(node.args[0], ast.Dict):
                dictionary = node.args[0]
            elif node.func.attr == "setdefault" and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    registrations.add(
                        Registration(
                            key=f"{relative}:{key.value}",
                            path=relative,
                            function="native_handlers",
                            line=line,
                            kind="native",
                        )
                    )
                else:
                    violations.append(
                        f"{relative}:{line}:dynamic native handler name is not auditable"
                    )
        if dictionary is None:
            continue
        for key in dictionary.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                registrations.add(
                    Registration(
                        key=f"{relative}:{key.value}",
                        path=relative,
                        function="native_handlers",
                        line=line,
                        kind="native",
                    )
                )
            else:
                violations.append(
                    f"{relative}:{line}:dynamic native handler name is not auditable"
                )
    return registrations, violations


def _native_registrations() -> tuple[set[Registration], list[str]]:
    """Discover every native LLM surface in every production agent module.

    Do not limit this to ``*/agent.py``: native ``ToolDefinition`` collections
    are deliberately allowed beside servers and helpers, and a filename rule
    would let a new unclassified capability evade the CI contract.
    """
    registrations: set[Registration] = set()
    violations: list[str] = []
    for path in sorted(SERVER_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        found, errors = _native_registrations_from_tree(relative, module)
        registrations.update(found)
        violations.extend(errors)
    return registrations, violations


def _chat_registrations() -> set[Registration]:
    """Discover CHAT_TOOLS instead of treating the web chat as out of scope."""
    relative = "agents/chat/tools.py"
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
    registrations: set[Registration] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and _call_name(node.func).endswith("ToolDefinition")
        ):
            continue
        name = _tool_name(node, "")
        if name:
            registrations.add(
                Registration(
                    key=f"{relative}:{name}",
                    path=relative,
                    function="execute_tool",
                    line=node.lineno,
                    kind="chat",
                )
            )
    return registrations


def _chat_handler_name(tree: ast.AST, tool_name: str) -> str | None:
    """Find the concrete _dispatch_tool branch for one advertised chat name."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "tool_name"
            and len(test.ops) == len(test.comparators) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == tool_name
        ):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _call_name(child.func)
                if name.startswith("_") and name != "_dispatch_tool":
                    return name
    return None


def _schema_fixture(schema: dict[str, object]) -> object:
    """Build a schema-valid harmless value; never use empty dict shortcuts."""
    if "enum" in schema:
        return schema["enum"][0]  # type: ignore[index]
    kind = schema.get("type")
    if kind == "string":
        return "PERF-policy"
    if kind == "integer":
        return 1
    if kind == "number":
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        return [_schema_fixture(schema.get("items", {"type": "string"}))]  # type: ignore[arg-type]
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        return {
            name: _schema_fixture(properties[name])  # type: ignore[index]
            for name in required
            if name in properties  # type: ignore[operator]
        }
    return "PERF-policy"


def _chat_fixture(tool_name: str) -> dict[str, object]:
    """Return a per-registration schema-valid fixture with safe overrides."""
    from agents.chat.tools import CHAT_TOOLS

    definition = next(tool for tool in CHAT_TOOLS if tool.name == tool_name)
    fixture = _schema_fixture(definition.input_schema)
    assert isinstance(fixture, dict)
    # Stable non-secret values make any mocked state-store target inspectable.
    fixture.update(
        ticket_id="PERF-policy",
        username="policy-user",
        summary="policy fixture",
        description="safe audit policy fixture",
        message="safe audit policy fixture",
        fields={"policy_marker": "safe"},
    )
    return fixture


def _read_only_direct_mutations(registration: Registration) -> list[str]:
    """Return definite protected calls in an actual read-only entry handler.

    The checker resolves aliased imports (``from os import open as fd_open``
    and ``import os as operating_system``), not just their spelling at the
    call site.  Chat registrations resolve their real dispatch branch rather
    than inspecting the whole shared dispatcher.
    """
    if registration.kind not in {"mcp", "chat"}:
        return []
    tree = ast.parse(
        (ROOT / registration.path).read_text(encoding="utf-8"),
        filename=registration.path,
    )
    function = registration.function
    if registration.kind == "chat":
        function = _chat_handler_name(tree, registration.key.rsplit(":", 1)[1]) or ""
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        calls = []
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            call = _resolved_call_name(descendant.func, aliases)
            if (
                call in _PROTECTED_MUTATORS
                or call.rsplit(".", 1)[-1] in _PROTECTED_METHODS
            ):
                calls.append(f"{registration.path}:{descendant.lineno}:{call}")
        return calls
    raise AssertionError(f"could not resolve declared handler {registration!r}")


def test_tool_registration_inventory_is_complete_and_classified() -> None:
    """A new MCP/native action must add an explicit reviewed policy entry."""
    mcp, mcp_violations = _mcp_registrations()
    native, native_violations = _native_registrations()
    actual = mcp | native | _chat_registrations()
    assert not (mcp_violations + native_violations), "\n".join(
        mcp_violations + native_violations
    )
    expected = set(POLICY_BY_REGISTRATION)
    actual_keys = {registration.key for registration in actual}
    assert expected == actual_keys, (
        "tool audit inventory changed; add explicit classification, audited owner, "
        "and a bounded fixture exemption. "
        f"missing={sorted(actual_keys - expected)!r}; "
        f"stale={sorted(expected - actual_keys)!r}"
    )
    assert len(POLICY_BY_REGISTRATION) == len(TOOL_AUDIT_POLICY)


def test_dynamic_native_registration_exceptions_are_precise_and_consumed() -> None:
    """Schema relay exceptions cannot become a broad native registration waiver."""
    consumed: set[tuple[str, str]] = set()
    for exception in REGISTRATION_DISCOVERY_EXCEPTIONS:
        assert exception.owner and exception.reason
        assert exception.scope == f"{exception.path}:{exception.symbol}"
        assert date.fromisoformat(exception.expires_on) >= date.today()
        tree = ast.parse(
            (ROOT / exception.path).read_text(encoding="utf-8"),
            filename=exception.path,
        )
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        assert any(
            isinstance(node, ast.Call)
            and _call_name(node.func) in _tool_definition_aliases(tree)
            and any(keyword.arg == "name" for keyword in node.keywords)
            and not isinstance(
                next(
                    keyword for keyword in node.keywords if keyword.arg == "name"
                ).value,
                ast.Constant,
            )
            and _enclosing_symbol(node, parents) == exception.symbol
            for node in ast.walk(tree)
        ), f"stale dynamic native registration exception: {exception}"
        consumed.add((exception.path, exception.symbol))
    dynamic: set[tuple[str, str]] = set()
    for path in SERVER_ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        aliases = _tool_definition_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) not in aliases:
                continue
            name = next((item for item in node.keywords if item.arg == "name"), None)
            if name is not None and not isinstance(name.value, ast.Constant):
                dynamic.add((relative, _enclosing_symbol(node, parents)))
    assert consumed == dynamic, (
        "dynamic native ToolDefinition calls require a precise, consumed exception; "
        f"missing={sorted(dynamic - consumed)!r}; stale={sorted(consumed - dynamic)!r}"
    )


def test_registration_discovery_tracks_audited_mcp_aliases_and_wrappers() -> None:
    tree = ast.parse(
        """
from agents.mcp_audit import create_ticket_mcp as factory
mcp = factory("test")
alias = mcp
register = alias.tool

@register(name="alias_tool")
async def first(): pass

def audited_registration():
    return alias.tool()

@audited_registration()
async def second(): pass
"""
    )
    registrations, violations = _mcp_registrations_from_tree("agents/test.py", tree)
    assert not violations
    assert {registration.key for registration in registrations} == {
        "agents/test.py:alias_tool",
        "agents/test.py:second",
    }


def test_registration_discovery_fails_closed_for_dynamic_mcp_and_native_names() -> None:
    mcp_tree = ast.parse(
        """
from agents.mcp_audit import create_ticket_mcp
mcp = create_ticket_mcp("test")
name = external_name()
@mcp.tool(name=name)
async def dynamic_name(): pass
@getattr(mcp, "tool")()
async def dynamic_registrar(): pass
"""
    )
    _, mcp_violations = _mcp_registrations_from_tree("agents/test.py", mcp_tree)
    assert any("dynamic MCP advertised name" in item for item in mcp_violations)
    assert any("dynamic MCP tool registrar" in item for item in mcp_violations)

    native_tree = ast.parse(
        """
from providers.llm import base as llm_base
NativeDefinition = llm_base.ToolDefinition
name = external_name()
TOOLS = [NativeDefinition(name=name, description="x", input_schema={})]
self.alternate_tool_map = {"literal": handler}
self.alternate_tool_map.update({"also_literal": handler})
"""
    )
    registrations, native_violations = _native_registrations_from_tree(
        "agents/test.py", native_tree
    )
    assert {registration.key for registration in registrations} == {
        "agents/test.py:literal",
        "agents/test.py:also_literal",
    }
    assert native_violations == [
        "agents/test.py:5:dynamic native tool name is not auditable"
    ]


def test_checked_in_side_effect_inventory_has_zero_unexplained_boundaries() -> None:
    """CI rejects new direct or aliased sensitive capability use by default."""
    actual = _discover_side_effect_inventory()
    expected = set(INVENTORIED_SIDE_EFFECTS)
    assert actual == expected, (
        "sensitive capability inventory changed; classify the exact path/symbol "
        "as audited, system-only, unsupported, or a precise expiring exception. "
        f"unexplained={sorted(actual - expected)!r}; "
        f"stale={sorted(expected - actual)!r}"
    )
    # These compound inventory categories deliberately cover the requested
    # subdomains at the adapter level: container effects run through the
    # subprocess boundary, while cloud/resource provider effects share one
    # lifecycle boundary.  Keeping them explicit avoids a loophole caused by
    # provider-specific aliases.
    categories = {entry[2] for entry in expected}
    assert {
        "ssh",
        "subprocess",
        "mutating_http_state",
        "filesystem",
        "mcp",
        "github",
        "image",
        "cloud_resource",
    } <= categories
    assert set(INVENTORY_DISPOSITIONS) == expected, (
        "every sensitive boundary needs one exact reviewed disposition; "
        f"missing={sorted(expected - set(INVENTORY_DISPOSITIONS))!r}; "
        f"stale={sorted(set(INVENTORY_DISPOSITIONS) - expected)!r}"
    )
    for entry, (
        disposition,
        owner,
        scope,
        expires_on,
    ) in INVENTORY_DISPOSITIONS.items():
        assert disposition in {"audited", "system_only", "unsupported", "exception"}
        assert owner and scope == f"{entry[0]}:{entry[1]}"
        assert date.fromisoformat(expires_on) >= date.today()


def test_tool_audit_exemptions_and_side_effect_owners_are_reviewable() -> None:
    """Exemptions may be necessary, but cannot become silent permanent bypasses."""
    for policy in TOOL_AUDIT_POLICY:
        exemption = policy.fixture_exemption
        if exemption is not None:
            assert exemption.owner and exemption.reason
            assert date.fromisoformat(exemption.expires_on) >= date.today()
            assert policy.registration in exemption.reason, (
                "remote fixture exemptions must be per registration, not a broad "
                f"MCP/native surface waiver: {policy.registration}"
            )
        if policy.classification == "side_effecting":
            assert policy.operation_owner, policy.registration
            contract = OPERATION_OWNER_CONTRACTS.get(policy.operation_owner)
            assert contract, f"no concrete contract for {policy.operation_owner}"
            path, symbol = contract.split(":", 1)
            if path.endswith(".py"):
                source = (ROOT / path).read_text(encoding="utf-8")
            else:
                source = "\n".join(
                    candidate.read_text(encoding="utf-8")
                    for candidate in (ROOT / path).rglob("*.py")
                )
            assert symbol in source, (
                f"stale operation owner {policy.operation_owner}: {contract}"
            )
        else:
            assert policy.operation_owner is None, policy.registration

    used_owners = {
        policy.operation_owner
        for policy in TOOL_AUDIT_POLICY
        if policy.operation_owner is not None
    }
    assert used_owners == set(OPERATION_OWNER_CONTRACTS), (
        "operation owner contracts must not become stale documentation; "
        f"unused={set(OPERATION_OWNER_CONTRACTS) - used_owners!r}"
    )


def test_chat_mutation_owners_follow_real_dispatch_handlers() -> None:
    """A chat mutation cannot satisfy policy by naming its shared wrapper."""
    from agents.chat.tools import _CHAT_MUTATION_CONTRACTS

    tree = ast.parse(
        (ROOT / "agents/chat/tools.py").read_text(encoding="utf-8"),
        filename="agents/chat/tools.py",
    )
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for registration in _chat_registrations():
        policy = POLICY_BY_REGISTRATION[registration.key]
        if policy.classification != "side_effecting":
            continue
        name = registration.key.rsplit(":", 1)[1]
        owner, route = _CHAT_MUTATION_CONTRACTS[name]
        assert policy.operation_owner == owner
        handler = functions[owner.rsplit(".", 1)[1]]
        calls = [
            _call_name(call.func)
            for call in ast.walk(handler)
            if isinstance(call, ast.Call)
        ]
        assert any(
            call in {"client.post", "client.patch", "client.delete"} for call in calls
        ), f"{name} owner {owner} has no state-store mutation"
        assert route.startswith("/api/v1/")


def test_read_only_tool_declarations_do_not_call_protected_mutators() -> None:
    """Prevent a local write/subprocess from being labelled read-only."""
    mcp, _ = _mcp_registrations()
    checked = mcp | _chat_registrations()
    mismatches = []
    for registration in checked:
        policy = POLICY_BY_REGISTRATION[registration.key]
        if policy.classification != "read_only":
            continue
        mismatches.extend(_read_only_direct_mutations(registration))
    assert not mismatches, (
        "read-only tool calls a protected API; reclassify it and name its audited "
        f"operation/idempotency owner: {sorted(mismatches)!r}"
    )


def test_production_fastmcp_and_native_dispatch_have_one_audit_boundary() -> None:
    """Direct FastMCP construction or native handler dispatch is a CI failure."""
    allowlisted = {f"{entry.path}:{entry.symbol}" for entry in AUDIT_BYPASS_ALLOWLIST}
    for entry in AUDIT_BYPASS_ALLOWLIST:
        assert entry.owner and entry.scope and entry.reason
        assert date.fromisoformat(entry.expires_on) >= date.today()
    bypasses = []
    consumed_allowlist: set[str] = set()
    for path in sorted((ROOT / "agents").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "agents/mcp_audit.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        fastmcp_constructors = {"FastMCP"}
        for import_node in ast.walk(tree):
            if (
                isinstance(import_node, ast.ImportFrom)
                and import_node.module == "fastmcp"
            ):
                fastmcp_constructors.update(
                    alias.asname or alias.name
                    for alias in import_node.names
                    if alias.name == "FastMCP"
                )
            if isinstance(import_node, ast.Import):
                fastmcp_constructors.update(
                    f"{alias.asname or alias.name}.FastMCP"
                    for alias in import_node.names
                    if alias.name == "fastmcp"
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) in fastmcp_constructors
            ):
                key = f"{relative}:FastMCP"
                if key not in allowlisted:
                    bypasses.append(f"{relative}:{node.lineno}:FastMCP")
                else:
                    consumed_allowlist.add(key)
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "_tool_handlers"
                    for target in node.targets
                )
                and relative != "agents/base.py"
            ):
                key = f"{relative}:native handler assignment"
                if key not in allowlisted:
                    bypasses.append(
                        f"{relative}:{node.lineno}:native handler assignment"
                    )
                else:
                    consumed_allowlist.add(key)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"update", "setdefault"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_tool_handlers"
                and relative != "agents/base.py"
            ):
                key = f"{relative}:native handler {node.func.attr}"
                if key not in allowlisted:
                    bypasses.append(f"{relative}:{node.lineno}:native handler mutation")
                else:
                    consumed_allowlist.add(key)
    assert allowlisted == consumed_allowlist, (
        "audit bypass allowlist has stale or unmatched entries: "
        f"{sorted(allowlisted - consumed_allowlist)!r}"
    )
    assert not bypasses, (
        "production registrations must use create_ticket_mcp or "
        f"AgentBase._execute_tool: {bypasses!r}"
    )


@pytest.mark.asyncio
async def test_each_registered_mcp_name_gets_a_correlated_audit_pair() -> None:
    """Exercise each registration through its actual FastMCP middleware stack."""
    registrations, _ = _mcp_registrations()
    for registration in registrations:
        module_name = registration.path.removesuffix(".py").replace("/", ".")
        server = importlib.import_module(module_name)
        tool = await server.mcp.get_tool(registration.key.rsplit(":", 1)[1])
        assert tool is not None, registration.key
        arguments = _schema_fixture(tool.parameters)
        assert isinstance(arguments, dict), registration.key
        middleware = next(
            item
            for item in server.mcp.middleware
            if item.__class__.__name__ == "MCPAuditMiddleware"
        )
        events = []
        original_handler = tool.fn
        original_record = middleware._record

        async def harmless_handler(**_kwargs):
            # FastMCP validates declared output schemas after the handler.  A
            # mapping is accepted by the common wrapped-result contract and
            # keeps this fixture from invoking its remote implementation.
            return {"result": "policy fixture result"}

        middleware._record = events.append
        tool.fn = harmless_handler
        try:
            result = await server.mcp.call_tool(
                registration.key.rsplit(":", 1)[1], arguments
            )
        finally:
            tool.fn = original_handler
            middleware._record = original_record
        # Protected tools reject the harmless fixture before the handler when
        # it deliberately lacks a durable operation identity.  That is the
        # canonical real-boundary rejection path, not a fixture bypass.
        assert [event.lifecycle.state for event in events] in (
            [LifecycleState.REQUEST_RECEIVED, LifecycleState.RESPONSE_SENT],
            [LifecycleState.REQUEST_RECEIVED, LifecycleState.REJECTED],
        ), registration.key
        assert result.is_error == (
            events[-1].lifecycle.state == LifecycleState.REJECTED
        )
        assert {event.action.phase for event in events} == {
            registration.key.rsplit(":", 1)[1]
        }
        assert len({event.action_id for event in events}) == 1
        assert all(event.mcp.correlation_request_id for event in events)
        assert {event.attributes["policy_registration"] for event in events} == {
            registration.key
        }
        assert {event.attributes["policy_classification"] for event in events} == {
            POLICY_BY_REGISTRATION[registration.key].classification
        }
        assert {event.attributes["operation_owner"] for event in events} == {
            POLICY_BY_REGISTRATION[registration.key].operation_owner
        }


@pytest.mark.asyncio
async def test_each_registered_chat_name_gets_a_correlated_audit_pair() -> None:
    """Run every real CHAT_TOOLS dispatcher branch through ChatToolAudit."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatToolAudit, execute_tool

    registrations = _chat_registrations()
    for registration in registrations:
        events = []
        client = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": "PERF-policy", "approvals": []}
        client.get = AsyncMock(return_value=response)
        client.post = AsyncMock(return_value=response)
        client.patch = AsyncMock(return_value=response)
        tool_name = registration.key.rsplit(":", 1)[1]
        result = await execute_tool(
            tool_name,
            _chat_fixture(tool_name),
            client,
            "http://state-store.invalid",
            "policy-token",
            audit=ChatToolAudit(
                client,
                "http://state-store.invalid",
                "policy-token",
                record=events.append,
            ),
        )
        assert [event.lifecycle.state for event in events] == [
            LifecycleState.STARTED,
            LifecycleState.COMPLETED,
        ] or [event.lifecycle.state for event in events] == [
            LifecycleState.STARTED,
            LifecycleState.FAILED,
        ], registration.key
        assert len({event.action_id for event in events}) == 1
        assert {event.action.phase for event in events} == {tool_name}
        # The real dispatcher branch ran with a schema-valid input, rather
        # than only proving a shared wrapper around an artificial `{}` call.
        assert result
        if POLICY_BY_REGISTRATION[registration.key].classification == "side_effecting":
            assert client.post.await_count + client.patch.await_count >= 1, tool_name
            assert events[0].action.target and events[0].attributes[
                "operation_owner"
            ] == (POLICY_BY_REGISTRATION[registration.key].operation_owner)
            assert events[0].idempotency.key == events[0].attributes["operation_key"]
            assert events[0].idempotency.request_hash


@pytest.mark.asyncio
async def test_chat_audit_uses_the_service_credential_for_trace_ingestion() -> None:
    """Trace ingestion rejects user credentials, so this must be a service call."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatToolAudit, execute_tool

    client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)
    await execute_tool(
        "start_ticket",
        {},
        client,
        "http://state-store.invalid",
        "user-token",
        audit=ChatToolAudit(client, "http://state-store.invalid", "service-token"),
        tool_call_id="toolu-verified",
    )
    assert client.post.await_count == 2
    for call in client.post.await_args_list:
        assert call.args[0].endswith("/api/v1/traces/events")
        assert call.kwargs["headers"] == {"Authorization": "Bearer service-token"}
    assert [
        call.kwargs["json"]["lifecycle"]["state"]
        for call in client.post.await_args_list
    ] == [
        "started",
        "failed",
    ]
    assert {
        call.kwargs["json"]["tool_call_id"] for call in client.post.await_args_list
    } == {"toolu-verified"}


@pytest.mark.asyncio
async def test_chat_audit_fails_closed_before_handler_when_entry_is_unavailable() -> (
    None
):
    from unittest.mock import AsyncMock

    from agents.chat.tools import ChatAuditUnavailable, ChatToolAudit

    handler = AsyncMock(return_value='{"status": "unexpected"}')
    client = AsyncMock()
    client.post = AsyncMock(side_effect=OSError("trace service offline"))
    audit = ChatToolAudit(client, "http://state-store.invalid", "service-token")
    with pytest.raises(ChatAuditUnavailable):
        await audit.invoke("start_ticket", {"ticket_id": "PERF-policy"}, handler)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_audit_cancellation_emits_terminal_and_preserves_parent() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from agents.chat.tools import ChatToolAudit

    events = []
    parent = new_trace_context(ticket_id="PERF-policy", agent_id="web-request")
    token = bind_trace_context(parent)
    try:

        async def cancelled() -> str:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await ChatToolAudit(
                AsyncMock(),
                "http://state-store.invalid",
                "service-token",
                record=events.append,
            ).invoke("start_ticket", {"ticket_id": "PERF-policy"}, cancelled)
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.STARTED,
        LifecycleState.CANCELLED,
    ]
    assert {event.trace_id for event in events} == {parent.trace_id}
    assert {event.parent_action_id for event in events} == {parent.action_id}


@pytest.mark.asyncio
async def test_chat_terminal_delivery_failure_is_indeterminate_not_silent() -> None:
    """A completed mutation cannot quietly lose its terminal audit record."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatAuditUnavailable, ChatToolAudit

    response = MagicMock()
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[response, OSError("trace unavailable")])
    handler = AsyncMock(return_value='{"status": "ok"}')

    with pytest.raises(ChatAuditUnavailable, match="outcome is indeterminate"):
        await ChatToolAudit(
            client, "http://state-store.invalid", "service-token"
        ).invoke("start_ticket", {"ticket_id": "PERF-policy"}, handler)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_rejection_terminal_delivery_is_required() -> None:
    """Confirmation invalidation also has a durable started/rejected pair."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatAuditUnavailable, ChatToolAudit

    response = MagicMock()
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[response, OSError("trace unavailable")])
    with pytest.raises(ChatAuditUnavailable):
        await ChatToolAudit(
            client, "http://state-store.invalid", "service-token"
        ).reject("create_ticket", {"summary": "safe"})


def test_chat_tool_dispatch_has_no_production_audit_bypass() -> None:
    """Any direct *or aliased* production dispatcher needs an audit boundary."""
    calls: list[tuple[str, ast.Call]] = []
    for path in (ROOT / "agents").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "agents/chat/tools.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        aliases = _import_aliases(tree)
        calls.extend(
            (relative, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _resolved_call_name(node.func, aliases).endswith("tools.execute_tool")
        )
    assert calls, "CHAT_TOOLS has no production dispatcher"
    for relative, call in calls:
        audit = next(
            (item.value for item in call.keywords if item.arg == "audit"), None
        )
        assert (
            isinstance(audit, ast.Call) and _call_name(audit.func) == "ChatToolAudit"
        ), f"{relative}:{call.lineno} bypasses ChatToolAudit"
        assert any(item.arg == "tool_call_id" for item in call.keywords), (
            f"{relative}:{call.lineno} drops the LLM tool-call correlation id"
        )
        # The user's bearer must never become the trace producer credential.
        positional = audit.args[2:]
        token = positional[0] if positional else None
        assert not (isinstance(token, ast.Name) and token.id == "auth_token"), (
            f"{relative}:{call.lineno} supplies a user credential to ChatToolAudit"
        )

    store = ast.parse(
        (ROOT / "state_store/main.py").read_text(encoding="utf-8"),
        filename="state_store/main.py",
    )
    constructors = [
        node
        for node in ast.walk(store)
        if isinstance(node, ast.Call) and _call_name(node.func) == "ChatAgent"
    ]
    assert len(constructors) == 1
    assert any(item.arg == "audit_token" for item in constructors[0].keywords), (
        "embedded ChatAgent must use the service credential required by trace ingestion"
    )


def test_chat_agent_does_not_fallback_to_the_user_bearer_for_audit() -> None:
    """Audit must use the deployment credential, or fail closed before mutation."""
    tree = ast.parse(
        (ROOT / "agents/chat/agent.py").read_text(encoding="utf-8"),
        filename="agents/chat/agent.py",
    )
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) == "ChatToolAudit"
    ]
    assert constructors
    for constructor in constructors:
        assert len(constructor.args) >= 3
        rendered = ast.unparse(constructor.args[2])
        assert "auth_token" not in rendered


@pytest.mark.asyncio
async def test_chat_execute_tool_fails_closed_without_audit_boundary() -> None:
    """The public dispatcher must not retain a test-only raw execution path."""
    from unittest.mock import AsyncMock

    from agents.chat.tools import ChatAuditUnavailable, execute_tool

    with pytest.raises(ChatAuditUnavailable, match="requires an audit boundary"):
        await execute_tool(
            "search_tickets",
            {},
            AsyncMock(),
            "http://state-store.invalid",
            "user-token",
        )


def test_protected_call_aliases_resolve_to_the_canonical_api() -> None:
    """Regression coverage for import aliases that could otherwise evade CI."""
    tree = ast.parse(
        "from os import open as fd_open\nimport os as operating_system\n"
        "from pathlib import Path as LocalPath\n"
        "fd_open('x', operating_system.O_CREAT)\n"
        "operating_system.open('x', operating_system.O_WRONLY)\n"
        "LocalPath('x').open('w')\n"
    )
    aliases = _import_aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert [_resolved_call_name(call.func, aliases) for call in calls] == [
        "os.open",
        "os.open",
        "pathlib.Path.open",
        "pathlib.Path",
    ]
