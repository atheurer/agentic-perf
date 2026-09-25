"""AST inventory preventing unaudited ticket filesystem regressions."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).parents[1]
WRAPPER_PATH = "providers/execution/filesystem.py"
EXCLUDED_SOURCE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "tests",
    "test",
    "vendor",
    "build",
    "dist",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "coverage",
}
MUTATING_ATTRIBUTES = {
    "chmod",
    "chown",
    "extract",
    "extractall",
    "hardlink_to",
    "mkdir",
    "rename",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
    "writestr",
    "write_bytes",
    "write_text",
}
MUTATING_QUALIFIED = {
    "os.chmod",
    "os.chown",
    "os.fchmod",
    "os.fchown",
    "os.ftruncate",
    "os.open",
    "os.link",
    "os.makedirs",
    "os.mkdir",
    "os.replace",
    "os.rename",
    "os.unlink",
    "os.remove",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.utime",
    "os.write",
    "pathlib.Path.replace",
    "shutil.move",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copytree",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.make_archive",
    "shutil.rmtree",
    "shutil.unpack_archive",
    "tempfile.mkdtemp",
    "tempfile.mktemp",
    "tempfile.mkstemp",
    "tempfile.NamedTemporaryFile",
    "tempfile.TemporaryDirectory",
    "tempfile.TemporaryFile",
    "tempfile.SpooledTemporaryFile",
    "zipfile.ZipFile",
}
PATH_TYPES = {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}
ZIPFILE_TYPES = {"zipfile.ZipFile"}
DATETIME_TYPES = {"datetime.date", "datetime.datetime", "datetime.time"}
DATETIME_CONSTRUCTOR_CALLS = DATETIME_TYPES | {
    f"{type_name}.{method}"
    for type_name in DATETIME_TYPES
    for method in {
        "combine",
        "fromisoformat",
        "fromordinal",
        "fromtimestamp",
        "fromisocalendar",
        "now",
        "strptime",
        "today",
        "utcnow",
    }
}
DATETIME_RETURNING_METHODS = {
    "astimezone",
    "date",
    "replace",
    "time",
    "timetz",
}
PATH_RETURNING_ATTRIBUTES = {
    "absolute",
    "joinpath",
    "parent",
    "resolve",
    "with_name",
    "with_stem",
    "with_suffix",
}
FACADE_METHODS = {
    "append",
    "archive",
    "chmod",
    "hardlink",
    "mkdir",
    "open_descriptor",
    "open_stream",
    "rename",
    "rmdir",
    "set_descriptor_mode",
    "temporary_directory",
    "temporary_file",
    "touch",
    "unlink",
    "write",
    "write_descriptor",
    "write_stream",
}


@dataclass(frozen=True)
class Mutation:
    path: str
    line: int
    end_line: int
    call: str
    scope: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Resolve protected names bound by imports."""
    aliases: dict[str, str] = {}
    protected_modules = {
        "builtins",
        "datetime",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
        "tarfile",
        "zipfile",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in protected_modules:
                    aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in protected_modules:
            for imported in node.names:
                aliases[imported.asname or imported.name] = (
                    f"{node.module}.{imported.name}"
                )
    return aliases


def _resolved_call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
        constructor = _resolved_call_name(node.value.func, aliases)
        if constructor == "pathlib.Path" and node.attr == "open":
            return f"{constructor}.{node.attr}"
    raw = _call_name(node)
    root, dot, rest = raw.partition(".")
    return f"{aliases.get(root, root)}{dot}{rest}" if raw else raw


def _target_names(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _target_names(item)]
    return []


def _is_mutator_reference(name: str) -> bool:
    return (
        name in MUTATING_QUALIFIED
        or name.rsplit(".", 1)[-1] in MUTATING_ATTRIBUTES
        or name in {"open", "pathlib.Path.open"}
        or name.endswith(".open")
    )


def _expr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _scope_chain(scope: str) -> list[str]:
    if scope == "<module>":
        return [scope]
    result = ["<module>"]
    parts = scope.split(".")
    result.extend(".".join(parts[:index]) for index in range(1, len(parts) + 1))
    return result


def _assigned_receivers_by_scope(
    tree: ast.AST,
    scopes: dict[int, str],
    class_scopes: dict[int, str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Record assignments that can shadow an inferred receiver binding."""
    names: dict[str, set[str]] = {scope: set() for scope in set(scopes.values())}
    fields: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.setdefault(scopes[id(node)], set()).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.Import):
            names.setdefault(scopes[id(node)], set()).update(
                item.asname or item.name.split(".", 1)[0] for item in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            names.setdefault(scopes[id(node)], set()).update(
                item.asname or item.name for item in node.names
            )
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.setdefault(scopes[id(node)], set()).add(node.rest)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
        ):
            fields.setdefault(class_scopes[id(node)], set()).add(_expr_name(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent_scope = scopes[id(node)]
            scope = (
                f"{parent_scope}.{node.name}"
                if parent_scope != "<module>"
                else node.name
            )
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                names.setdefault(scope, set()).add(arg.arg)
    return names, fields


def _unclassified_receiver_stores(
    tree: ast.AST,
    scopes: dict[int, str],
    class_scopes: dict[int, str],
    recognized_stores: set[int] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Find stores that must discard inferred receiver types."""
    recognized_stores = recognized_stores or set()
    assigned_targets: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            assigned_targets.update(
                id(candidate)
                for candidate in ast.walk(target)
                if isinstance(candidate, (ast.Name, ast.Attribute))
                and isinstance(candidate.ctx, ast.Store)
            )

    names: dict[str, set[str]] = {}
    fields: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if id(node) in recognized_stores:
            continue
        if id(node) in assigned_targets:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.setdefault(scopes[id(node)], set()).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.Import):
            names.setdefault(scopes[id(node)], set()).update(
                item.asname or item.name.split(".", 1)[0] for item in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            names.setdefault(scopes[id(node)], set()).update(
                item.asname or item.name for item in node.names
            )
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.setdefault(scopes[id(node)], set()).add(node.rest)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
        ):
            fields.setdefault(class_scopes[id(node)], set()).add(_expr_name(node))

    # A global/nonlocal assignment updates the declared binding, not a local
    # variable with the same spelling. Invalidate both scopes conservatively.
    local_bindings, _ = _assigned_receivers_by_scope(tree, scopes, class_scopes)
    function_scopes = {
        (
            f"{scopes[id(node)]}.{node.name}"
            if scopes[id(node)] != "<module>"
            else node.name
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Global, ast.Nonlocal)):
            continue
        scope = scopes[id(node)]
        written = set(node.names) & local_bindings.get(scope, set())
        names.setdefault(scope, set()).update(written)
        if isinstance(node, ast.Global):
            names.setdefault("<module>", set()).update(written)
            continue
        ancestors = list(reversed(_scope_chain(scope)[:-1]))
        for name in written:
            target_scope = next(
                (
                    ancestor
                    for ancestor in ancestors
                    if ancestor in function_scopes
                    and name in local_bindings.get(ancestor, set())
                ),
                None,
            )
            if target_scope is not None:
                names.setdefault(target_scope, set()).add(name)
    return names, fields


def _assignment_values(
    target: ast.expr, value: ast.expr | None
) -> list[tuple[str, ast.expr | None]]:
    """Pair assigned names/fields with their values, failing closed on unpacking."""
    if isinstance(target, (ast.Name, ast.Attribute)):
        return [(_expr_name(target), value)]
    if isinstance(target, (ast.Tuple, ast.List)):
        if isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(
            value.elts
        ):
            return [
                pair
                for target_item, value_item in zip(target.elts, value.elts)
                for pair in _assignment_values(target_item, value_item)
            ]
        return [
            pair
            for target_item in target.elts
            for pair in _assignment_values(target_item, None)
        ]
    return []


def _binding_is_available(
    name: str,
    scope: str,
    bindings: dict[str, set[str]],
    assigned_names: dict[str, set[str]],
) -> bool:
    """Use the nearest scope's inferred binding, respecting local shadowing."""
    for ancestor in reversed(_scope_chain(scope)):
        if name in bindings.get(ancestor, set()):
            return True
        if name in assigned_names.get(ancestor, set()):
            return False
    return False


def _scope_imports(
    tree: ast.AST, scopes: dict[int, str]
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    mutation_imports: dict[str, dict[str, str]] = {}
    facade_imports: dict[str, set[str]] = {}
    protected = {
        "builtins",
        "datetime",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
        "tarfile",
        "zipfile",
    }
    for node in ast.walk(tree):
        scope = scopes[id(node)]
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".", 1)[0]
                if root in protected:
                    name = item.asname or root
                    mutation_imports.setdefault(scope, {})[name] = root
                if item.name in {
                    "providers.execution",
                    "providers.execution.filesystem",
                }:
                    alias = item.asname or item.name
                    facade_imports.setdefault(scope, set()).add(
                        f"{alias}.AuditedFilesystem"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module in protected:
                for item in node.names:
                    mutation_imports.setdefault(scope, {})[item.asname or item.name] = (
                        f"{node.module}.{item.name}"
                    )
            if node.module in {
                "providers.execution",
                "providers.execution.filesystem",
            }:
                for item in node.names:
                    if item.name == "AuditedFilesystem":
                        facade_imports.setdefault(scope, set()).add(
                            item.asname or item.name
                        )
    return mutation_imports, facade_imports


def _aliases_by_scope(
    tree: ast.AST, scopes: dict[int, str]
) -> dict[str, dict[str, str]]:
    """Resolve imported mutators and simple assignments without crossing scopes."""
    imports, _ = _scope_imports(tree, scopes)
    nodes = list(ast.walk(tree))
    aliases_by_scope: dict[str, dict[str, str]] = {}
    for scope in set(scopes.values()):
        aliases: dict[str, str] = {}
        for ancestor in _scope_chain(scope):
            aliases.update(imports.get(ancestor, {}))
            changed = True
            while changed:
                changed = False
                for node in nodes:
                    if scopes[id(node)] != ancestor:
                        continue
                    if isinstance(node, ast.Assign):
                        targets, value = node.targets, node.value
                    elif isinstance(node, ast.AnnAssign) and node.value is not None:
                        targets, value = [node.target], node.value
                    else:
                        continue
                    if not isinstance(value, (ast.Name, ast.Attribute)):
                        continue
                    resolved = _resolved_call_name(value, aliases)
                    if not _is_mutator_reference(resolved):
                        continue
                    for target in targets:
                        for name in _target_names(target):
                            if aliases.get(name) != resolved:
                                aliases[name] = resolved
                                changed = True
        aliases_by_scope[scope] = aliases
    return aliases_by_scope


def _is_path_expression(
    expr: ast.expr,
    scope: str,
    class_scope: str,
    aliases_by_scope: dict[str, dict[str, str]],
    bindings: dict[str, set[str]],
    fields_by_class: dict[str, set[str]],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> bool:
    aliases = aliases_by_scope.get(scope, {})
    if isinstance(expr, ast.NamedExpr):
        return _is_path_expression(
            expr.value,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        )
    if isinstance(expr, ast.Call):
        return _resolved_call_name(expr.func, aliases) in PATH_TYPES
    if isinstance(expr, ast.IfExp):
        return _is_path_expression(
            expr.body,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        ) and _is_path_expression(
            expr.orelse,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return _is_path_expression(
            expr.left,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        ) or _is_path_expression(
            expr.right,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        )
    if isinstance(expr, ast.Attribute):
        name = _expr_name(expr)
        if name in fields_by_class.get(class_scope, set()):
            return True
        if expr.attr in PATH_RETURNING_ATTRIBUTES:
            return _is_path_expression(
                expr.value,
                scope,
                class_scope,
                aliases_by_scope,
                bindings,
                fields_by_class,
                assigned_names,
                assigned_fields,
            )
    name = _expr_name(expr)
    if name in fields_by_class.get(class_scope, set()):
        return True
    if name in assigned_fields.get(class_scope, set()):
        return False
    variable = name.split(".", 1)[0]
    return _binding_is_available(variable, scope, bindings, assigned_names)


def _path_bindings_by_scope(
    tree: ast.AST,
    scopes: dict[int, str],
    class_scopes: dict[int, str],
    aliases_by_scope: dict[str, dict[str, str]],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Track simple pathlib.Path bindings so string ``replace`` stays allowed."""
    nodes = list(ast.walk(tree))
    bindings: dict[str, set[str]] = {scope: set() for scope in set(scopes.values())}
    fields_by_class: dict[str, set[str]] = {}

    def path_annotation(annotation: ast.expr | None, scope: str) -> bool:
        if annotation is None:
            return False
        resolved = _resolved_call_name(annotation, aliases_by_scope.get(scope, {}))
        if isinstance(annotation, ast.Subscript):
            return path_annotation(annotation.value, scope)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return path_annotation(annotation.left, scope) or path_annotation(
                annotation.right, scope
            )
        return resolved in PATH_TYPES

    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent_scope = scopes[id(node)]
        scope = (
            f"{parent_scope}.{node.name}" if parent_scope != "<module>" else node.name
        )
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if path_annotation(arg.annotation, scope):
                bindings.setdefault(scope, set()).add(arg.arg)

    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                annotation_is_path = False
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
                annotation_is_path = path_annotation(node.annotation, scopes[id(node)])
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if not annotation_is_path and (
                value is None
                or not _is_path_expression(
                    value,
                    scope,
                    class_scope,
                    aliases_by_scope,
                    bindings,
                    fields_by_class,
                    assigned_names,
                    assigned_fields,
                )
            ):
                continue
            for target in targets:
                names = (
                    [_expr_name(item) for item in target.elts]
                    if isinstance(target, (ast.Tuple, ast.List))
                    else [_expr_name(target)]
                )
                for name in names:
                    if not name:
                        continue
                    if name.startswith(("self.", "cls.")):
                        fields_by_class.setdefault(class_scope, set()).add(name)
                    elif name not in bindings.setdefault(scope, set()):
                        bindings[scope].add(name)
                        changed = True

    recognized_stores: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.NamedExpr):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            values = [node.value]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if (
                not isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
                or not node.iter.elts
            ):
                continue
            values = node.iter.elts
        else:
            continue
        if not isinstance(target, (ast.Name, ast.Attribute)) or not all(
            _is_path_expression(
                value,
                scope,
                class_scope,
                aliases_by_scope,
                bindings,
                fields_by_class,
                assigned_names,
                assigned_fields,
            )
            for value in values
        ):
            continue
        name = _expr_name(target)
        if not name:
            continue
        recognized_stores.add(id(target))
        if name.startswith(("self.", "cls.")):
            fields_by_class.setdefault(class_scope, set()).add(name)
        else:
            bindings.setdefault(scope, set()).add(name)

    unclassified_names, unclassified_fields = _unclassified_receiver_stores(
        tree, scopes, class_scopes, recognized_stores
    )
    for scope, names in unclassified_names.items():
        bindings.get(scope, set()).difference_update(names)
    for class_scope, names in unclassified_fields.items():
        fields_by_class.get(class_scope, set()).difference_update(names)

    changed = True
    while changed:
        changed = False
        invalid_bindings: set[tuple[str, str]] = set()
        invalid_fields: set[tuple[str, str]] = set()
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                annotated = False
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
                annotated = path_annotation(node.annotation, scopes[id(node)])
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            for target in targets:
                for name, assigned_value in _assignment_values(target, value):
                    if not name:
                        continue
                    if name in bindings.get(scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_bindings.add((scope, name))
                        elif not _is_path_expression(
                            assigned_value,
                            scope,
                            class_scope,
                            aliases_by_scope,
                            bindings,
                            fields_by_class,
                            assigned_names,
                            assigned_fields,
                        ):
                            invalid_bindings.add((scope, name))
                    if name in fields_by_class.get(class_scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_fields.add((class_scope, name))
                        elif not _is_path_expression(
                            assigned_value,
                            scope,
                            class_scope,
                            aliases_by_scope,
                            bindings,
                            fields_by_class,
                            assigned_names,
                            assigned_fields,
                        ):
                            invalid_fields.add((class_scope, name))
        for scope, name in invalid_bindings:
            bindings[scope].discard(name)
        for class_scope, name in invalid_fields:
            fields_by_class.get(class_scope, set()).discard(name)
        changed = bool(invalid_bindings or invalid_fields)
    return bindings, fields_by_class


def _path_replace_signature(call: ast.Call) -> bool:
    """Match pathlib.Path.replace's one-target signature without matching str.replace."""
    return (len(call.args) == 1 and not call.keywords) or (
        not call.args and len(call.keywords) == 1 and call.keywords[0].arg == "target"
    )


def _numeric_path_replace_target(call: ast.Call) -> bool:
    target: ast.expr | None = None
    if len(call.args) == 1 and not call.keywords:
        target = call.args[0]
    elif not call.args and len(call.keywords) == 1 and call.keywords[0].arg == "target":
        target = call.keywords[0].value
    return isinstance(target, ast.Constant) and isinstance(target.value, (int, float))


def _known_nonpath_replace_receiver(
    node: ast.Attribute,
    aliases: dict[str, str],
) -> bool:
    """Return true only for obvious string replace method references."""
    receiver = node.value
    if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
        return True
    if isinstance(receiver, ast.Call):
        name = _resolved_call_name(receiver.func, aliases)
        if name == "str":
            return True
        if isinstance(receiver.func, ast.Attribute) and receiver.func.attr in {
            "lower",
            "lstrip",
            "replace",
            "rstrip",
            "strip",
            "upper",
        }:
            return _known_nonpath_replace_receiver(receiver.func, aliases)
    return False


def _is_zipfile_expression(
    expr: ast.expr,
    scope: str,
    class_scope: str,
    bindings: dict[str, set[str]],
    fields_by_class: dict[str, set[str]],
    aliases: dict[str, str],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> bool:
    if isinstance(expr, ast.NamedExpr):
        return _is_zipfile_expression(
            expr.value,
            scope,
            class_scope,
            bindings,
            fields_by_class,
            aliases,
            assigned_names,
            assigned_fields,
        )
    if isinstance(expr, ast.Call):
        return _resolved_call_name(expr.func, aliases) in ZIPFILE_TYPES
    name = _expr_name(expr)
    if name in fields_by_class.get(class_scope, set()):
        return True
    if name in assigned_fields.get(class_scope, set()):
        return False
    return _binding_is_available(name, scope, bindings, assigned_names)


def _zipfile_bindings_by_scope(
    tree: ast.AST,
    scopes: dict[int, str],
    class_scopes: dict[int, str],
    aliases_by_scope: dict[str, dict[str, str]],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Track local ZipFile objects so ``ZipFile.open`` uses its actual mode slot."""
    nodes = list(ast.walk(tree))
    bindings: dict[str, set[str]] = {scope: set() for scope in set(scopes.values())}
    fields_by_class: dict[str, set[str]] = {}

    def is_zipfile(expr: ast.expr, scope: str, class_scope: str) -> bool:
        aliases = aliases_by_scope.get(scope, {})
        if isinstance(expr, ast.Call):
            return _resolved_call_name(expr.func, aliases) in ZIPFILE_TYPES
        return _is_zipfile_expression(
            expr,
            scope,
            class_scope,
            bindings,
            fields_by_class,
            aliases,
            assigned_names,
            assigned_fields,
        )

    def zipfile_annotation(annotation: ast.expr | None, scope: str) -> bool:
        if annotation is None:
            return False
        aliases = aliases_by_scope.get(scope, {})
        if _resolved_call_name(annotation, aliases) in ZIPFILE_TYPES:
            return True
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return zipfile_annotation(annotation.left, scope) or zipfile_annotation(
                annotation.right, scope
            )
        if isinstance(annotation, ast.Subscript) and _expr_name(annotation.value).split(
            "."
        )[-1] in {"Optional", "Union"}:
            values = (
                annotation.slice.elts
                if isinstance(annotation.slice, ast.Tuple)
                else [annotation.slice]
            )
            return any(zipfile_annotation(value, scope) for value in values)
        return False

    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent_scope = scopes[id(node)]
        scope = (
            f"{parent_scope}.{node.name}" if parent_scope != "<module>" else node.name
        )
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if zipfile_annotation(arg.annotation, scope):
                bindings.setdefault(scope, set()).add(arg.arg)

    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if not zipfile_annotation(
                node.annotation if isinstance(node, ast.AnnAssign) else None, scope
            ) and (value is None or not is_zipfile(value, scope, class_scope)):
                continue
            for target in targets:
                names = (
                    [_expr_name(item) for item in target.elts]
                    if isinstance(target, (ast.Tuple, ast.List))
                    else [_expr_name(target)]
                )
                for name in names:
                    if not name:
                        continue
                    if name.startswith(("self.", "cls.")):
                        fields_by_class.setdefault(class_scope, set()).add(name)
                    elif name not in bindings.setdefault(scope, set()):
                        bindings[scope].add(name)
                        changed = True

    recognized_stores: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.NamedExpr):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            values = [node.value]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if (
                not isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
                or not node.iter.elts
            ):
                continue
            values = node.iter.elts
        else:
            continue
        if not isinstance(target, (ast.Name, ast.Attribute)) or not all(
            is_zipfile(value, scope, class_scope) for value in values
        ):
            continue
        name = _expr_name(target)
        if not name:
            continue
        recognized_stores.add(id(target))
        if name.startswith(("self.", "cls.")):
            fields_by_class.setdefault(class_scope, set()).add(name)
        else:
            bindings.setdefault(scope, set()).add(name)

    unclassified_names, unclassified_fields = _unclassified_receiver_stores(
        tree, scopes, class_scopes, recognized_stores
    )
    for scope, names in unclassified_names.items():
        bindings.get(scope, set()).difference_update(names)
    for class_scope, names in unclassified_fields.items():
        fields_by_class.get(class_scope, set()).difference_update(names)

    changed = True
    while changed:
        changed = False
        invalid_bindings: set[tuple[str, str]] = set()
        invalid_fields: set[tuple[str, str]] = set()
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                annotated = False
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
                annotated = zipfile_annotation(node.annotation, scopes[id(node)])
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            for target in targets:
                for name, assigned_value in _assignment_values(target, value):
                    if not name:
                        continue
                    if name in bindings.get(scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_bindings.add((scope, name))
                        elif not is_zipfile(assigned_value, scope, class_scope):
                            invalid_bindings.add((scope, name))
                    if name in fields_by_class.get(class_scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_fields.add((class_scope, name))
                        elif not is_zipfile(assigned_value, scope, class_scope):
                            invalid_fields.add((class_scope, name))
        for scope, name in invalid_bindings:
            bindings[scope].discard(name)
        for class_scope, name in invalid_fields:
            fields_by_class.get(class_scope, set()).discard(name)
        changed = bool(invalid_bindings or invalid_fields)
    return bindings, fields_by_class


def _datetime_bindings_by_scope(
    tree: ast.AST,
    scopes: dict[int, str],
    class_scopes: dict[int, str],
    aliases_by_scope: dict[str, dict[str, str]],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Track ordinary date/time values so their replace method is not Path.replace."""
    nodes = list(ast.walk(tree))
    bindings: dict[str, set[str]] = {scope: set() for scope in set(scopes.values())}
    fields_by_class: dict[str, set[str]] = {}

    def is_datetime_annotation(annotation: ast.expr | None, scope: str) -> bool:
        if annotation is None:
            return False
        aliases = aliases_by_scope.get(scope, {})
        if _resolved_call_name(annotation, aliases) in DATETIME_TYPES:
            return True
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return is_datetime_annotation(
                annotation.left, scope
            ) or is_datetime_annotation(annotation.right, scope)
        if isinstance(annotation, ast.Subscript) and _expr_name(annotation.value).split(
            "."
        )[-1] in {"Optional", "Union"}:
            values = (
                annotation.slice.elts
                if isinstance(annotation.slice, ast.Tuple)
                else [annotation.slice]
            )
            return any(is_datetime_annotation(value, scope) for value in values)
        return False

    def is_datetime(expr: ast.expr, scope: str, class_scope: str) -> bool:
        aliases = aliases_by_scope.get(scope, {})
        if isinstance(expr, ast.NamedExpr):
            return is_datetime(expr.value, scope, class_scope)
        if isinstance(expr, ast.Call):
            if _resolved_call_name(expr.func, aliases) in DATETIME_CONSTRUCTOR_CALLS:
                return True
            return (
                isinstance(expr.func, ast.Attribute)
                and expr.func.attr in DATETIME_RETURNING_METHODS
                and is_datetime(expr.func.value, scope, class_scope)
            )
        name = _expr_name(expr)
        if name in fields_by_class.get(class_scope, set()):
            return True
        if name in assigned_fields.get(class_scope, set()):
            return False
        return _binding_is_available(name, scope, bindings, assigned_names)

    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent_scope = scopes[id(node)]
        scope = (
            f"{parent_scope}.{node.name}" if parent_scope != "<module>" else node.name
        )
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if is_datetime_annotation(arg.annotation, scope):
                bindings.setdefault(scope, set()).add(arg.arg)

    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                annotation_is_datetime = False
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
                annotation_is_datetime = is_datetime_annotation(
                    node.annotation, scopes[id(node)]
                )
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if not annotation_is_datetime and (
                value is None or not is_datetime(value, scope, class_scope)
            ):
                continue
            for target in targets:
                names = (
                    [_expr_name(item) for item in target.elts]
                    if isinstance(target, (ast.Tuple, ast.List))
                    else [_expr_name(target)]
                )
                for name in names:
                    if not name:
                        continue
                    if name.startswith(("self.", "cls.")):
                        fields_by_class.setdefault(class_scope, set()).add(name)
                    elif name not in bindings.setdefault(scope, set()):
                        bindings[scope].add(name)
                        changed = True

    recognized_stores: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.NamedExpr):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            values = [node.value]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if (
                not isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
                or not node.iter.elts
            ):
                continue
            values = node.iter.elts
        else:
            continue
        if not isinstance(target, (ast.Name, ast.Attribute)) or not all(
            is_datetime(value, scope, class_scope) for value in values
        ):
            continue
        name = _expr_name(target)
        if not name:
            continue
        recognized_stores.add(id(target))
        if name.startswith(("self.", "cls.")):
            fields_by_class.setdefault(class_scope, set()).add(name)
        else:
            bindings.setdefault(scope, set()).add(name)

    unclassified_names, unclassified_fields = _unclassified_receiver_stores(
        tree, scopes, class_scopes, recognized_stores
    )
    for scope, names in unclassified_names.items():
        bindings.get(scope, set()).difference_update(names)
    for class_scope, names in unclassified_fields.items():
        fields_by_class.get(class_scope, set()).difference_update(names)

    changed = True
    while changed:
        changed = False
        invalid_bindings: set[tuple[str, str]] = set()
        invalid_fields: set[tuple[str, str]] = set()
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                annotated = False
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
                annotated = is_datetime_annotation(node.annotation, scopes[id(node)])
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            for target in targets:
                for name, assigned_value in _assignment_values(target, value):
                    if not name:
                        continue
                    if name in bindings.get(scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_bindings.add((scope, name))
                        elif not is_datetime(assigned_value, scope, class_scope):
                            invalid_bindings.add((scope, name))
                    if name in fields_by_class.get(class_scope, set()):
                        if assigned_value is None:
                            if not annotated:
                                invalid_fields.add((class_scope, name))
                        elif not is_datetime(assigned_value, scope, class_scope):
                            invalid_fields.add((class_scope, name))
        for scope, name in invalid_bindings:
            bindings[scope].discard(name)
        for class_scope, name in invalid_fields:
            fields_by_class.get(class_scope, set()).discard(name)
        changed = bool(invalid_bindings or invalid_fields)
    return bindings, fields_by_class


def _is_datetime_expression(
    expr: ast.expr,
    scope: str,
    class_scope: str,
    aliases_by_scope: dict[str, dict[str, str]],
    bindings: dict[str, set[str]],
    fields_by_class: dict[str, set[str]],
    assigned_names: dict[str, set[str]],
    assigned_fields: dict[str, set[str]],
) -> bool:
    aliases = aliases_by_scope.get(scope, {})
    if isinstance(expr, ast.NamedExpr):
        return _is_datetime_expression(
            expr.value,
            scope,
            class_scope,
            aliases_by_scope,
            bindings,
            fields_by_class,
            assigned_names,
            assigned_fields,
        )
    if isinstance(expr, ast.Call):
        if _resolved_call_name(expr.func, aliases) in DATETIME_CONSTRUCTOR_CALLS:
            return True
        return (
            isinstance(expr.func, ast.Attribute)
            and expr.func.attr in DATETIME_RETURNING_METHODS
            and _is_datetime_expression(
                expr.func.value,
                scope,
                class_scope,
                aliases_by_scope,
                bindings,
                fields_by_class,
                assigned_names,
                assigned_fields,
            )
        )
    name = _expr_name(expr)
    if name in fields_by_class.get(class_scope, set()):
        return True
    if name in assigned_fields.get(class_scope, set()):
        return False
    return _binding_is_available(name, scope, bindings, assigned_names)


def _facade_state_by_scope(
    tree: ast.AST, scopes: dict[int, str], class_scopes: dict[int, str]
) -> tuple[
    dict[str, dict[str, bool]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    """Resolve imported facade instances and local mutation aliases by scope."""
    _, imported_facades = _scope_imports(tree, scopes)
    nodes = list(ast.walk(tree))
    scope_names = set(scopes.values())
    bindings: dict[str, dict[str, bool]] = {scope: {} for scope in scope_names}
    locals_by_scope: dict[str, set[str]] = {scope: set() for scope in scope_names}
    parameters_by_scope: dict[str, set[str]] = {scope: set() for scope in scope_names}
    fields_by_class: dict[str, set[str]] = {}
    returns: dict[str, set[int | None]] = {}

    class_body_scopes = {
        (
            f"{scopes[id(node)]}.{node.name}"
            if scopes[id(node)] != "<module>"
            else node.name
        )
        for node in nodes
        if isinstance(node, ast.ClassDef)
    }

    # Collect lexical locals first so an assigned or parameter-shadowed
    # ``AuditedFilesystem`` name cannot masquerade as its imported binding.
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent_scope = scopes[id(node)]
            scope = (
                f"{parent_scope}.{node.name}"
                if parent_scope != "<module>"
                else node.name
            )
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                locals_by_scope.setdefault(scope, set()).add(arg.arg)
                parameters_by_scope.setdefault(scope, set()).add(arg.arg)
        elif isinstance(node, ast.Lambda):
            parent_scope = scopes[id(node)]
            scope = (
                f"{parent_scope}.<lambda>" if parent_scope != "<module>" else "<lambda>"
            )
            args = getattr(node, "args", None)
            if args:
                for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                    locals_by_scope.setdefault(scope, set()).add(arg.arg)
                    parameters_by_scope.setdefault(scope, set()).add(arg.arg)
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for name in _target_names(target):
                locals_by_scope.setdefault(scopes[id(node)], set()).add(name)

    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            locals_by_scope.setdefault(scopes[id(node)], set()).add(node.rest)

    def class_aliases(scope: str) -> set[str]:
        chain = _scope_chain(scope)
        found: set[str] = set()
        for index, ancestor in enumerate(chain):
            if ancestor in class_body_scopes and ancestor != scope:
                continue
            for alias in imported_facades.get(ancestor, set()):
                root_name = alias.split(".", 1)[0]
                if any(
                    root_name in locals_by_scope.get(descendant, set())
                    for descendant in chain[index:]
                ):
                    continue
                found.add(alias)
        return found

    def is_facade_annotation(annotation: ast.expr, scope: str) -> bool:
        name = _expr_name(annotation)
        return name in class_aliases(scope)

    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent_scope = scopes[id(node)]
            def_scope = (
                f"{parent_scope}.{node.name}"
                if parent_scope != "<module>"
                else node.name
            )
            annotation = node.returns
            indices: set[int | None] = set()
            if is_facade_annotation(annotation, parent_scope):
                indices.add(None)
            elif isinstance(annotation, ast.Subscript):
                values = (
                    annotation.slice.elts
                    if isinstance(annotation.slice, ast.Tuple)
                    else [annotation.slice]
                )
                indices.update(
                    index
                    for index, value in enumerate(values)
                    if is_facade_annotation(value, parent_scope)
                )
            if indices:
                returns[def_scope] = indices

    def is_constructor(expr: ast.expr, scope: str) -> bool:
        if not isinstance(expr, ast.Call):
            return False
        name = _expr_name(expr.func)
        return any(
            name == alias or name == f"{alias}.system" for alias in class_aliases(scope)
        )

    def returned_indices(
        expr: ast.expr, scope: str, class_scope: str
    ) -> set[int | None]:
        if not isinstance(expr, ast.Call):
            return set()
        name = _expr_name(expr.func)
        candidates = []
        if class_scope and name.startswith(("self.", "cls.")):
            candidates.append(f"{class_scope}.{name.rsplit('.', 1)[-1]}")
        elif "." not in name:
            candidates.extend(
                f"{ancestor}.{name}" if ancestor != "<module>" else name
                for ancestor in reversed(_scope_chain(scope))
                if ancestor != class_scope or scope == class_scope
            )
        return next((returns[key] for key in candidates if key in returns), set())

    def is_bound_facade(name: str, scope: str, class_scope: str) -> bool:
        if name in fields_by_class.get(class_scope, set()):
            return True
        variable = name.split(".", 1)[0]
        available = False
        for ancestor in _scope_chain(scope):
            if ancestor == class_scope and scope != class_scope:
                continue
            if name in bindings.get(ancestor, {}):
                available = True
            elif variable in locals_by_scope.get(ancestor, set()):
                available = False
        return available

    def target_facade_indices(
        expr: ast.expr, scope: str, class_scope: str
    ) -> set[int | None]:
        if is_constructor(expr, scope):
            return {None}
        if isinstance(expr, ast.IfExp):
            left = target_facade_indices(expr.body, scope, class_scope)
            right = target_facade_indices(expr.orelse, scope, class_scope)
            return left & right
        if isinstance(expr, (ast.Name, ast.Attribute)):
            return (
                {None}
                if is_bound_facade(_expr_name(expr), scope, class_scope)
                else set()
            )
        return returned_indices(expr, scope, class_scope)

    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            indices = target_facade_indices(value, scope, class_scope)
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)) and None not in indices:
                    names = [
                        _expr_name(item)
                        for index, item in enumerate(target.elts)
                        if index in indices
                    ]
                else:
                    names = [_expr_name(target)] if indices else []
                for name in names:
                    if not name:
                        continue
                    if bindings.setdefault(scope, {}).get(name) is not True:
                        bindings[scope][name] = True
                        changed = True
                    if name.startswith(("self.", "cls.")):
                        fields_by_class.setdefault(class_scope, set()).add(name)

    recognized_stores: set[int] = set()
    recognized_facade_assignment_lines: dict[tuple[str, str], int] = {}
    for node in nodes:
        if isinstance(node, ast.NamedExpr):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            values = [node.value]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target = node.target
            scope = scopes[id(node)]
            class_scope = class_scopes[id(node)]
            if (
                not isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
                or not node.iter.elts
            ):
                continue
            values = node.iter.elts
        else:
            continue
        if not isinstance(target, (ast.Name, ast.Attribute)) or not all(
            target_facade_indices(value, scope, class_scope) == {None}
            for value in values
        ):
            continue
        name = _expr_name(target)
        if not name:
            continue
        recognized_stores.add(id(target))
        bindings.setdefault(scope, {})[name] = True
        if name.startswith(("self.", "cls.")):
            fields_by_class.setdefault(class_scope, set()).add(name)
        recognized_facade_assignment_lines[(scope, name)] = node.lineno

    # A facade binding is safe only if every assignment in its lexical scope
    # preserves that identity. Rebinding it to an unknown object must not turn
    # a later raw ``unlink``/``write`` into an apparent facade call.
    facade_assignment_lines: dict[tuple[str, str], int] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        scope = scopes[id(node)]
        indices = target_facade_indices(value, scope, class_scopes[id(node)])
        for target in targets:
            names = (
                [
                    _expr_name(item)
                    for index, item in enumerate(target.elts)
                    if index in indices
                ]
                if isinstance(target, (ast.Tuple, ast.List)) and None not in indices
                else [_expr_name(target)]
                if indices
                else []
            )
            for name in names:
                if name:
                    facade_assignment_lines[(scope, name)] = node.lineno
    facade_assignment_lines.update(recognized_facade_assignment_lines)

    invalid_bindings: set[tuple[str, str]] = set()
    invalid_fields: set[tuple[str, str]] = set()
    invalid_bindings.update(
        (scope, name) for scope, names in parameters_by_scope.items() for name in names
    )
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        scope = scopes[id(node)]
        class_scope = class_scopes[id(node)]
        indices = target_facade_indices(value, scope, class_scope)
        for target in targets:
            names = (
                [_expr_name(target)]
                if isinstance(target, (ast.Name, ast.Attribute))
                else _target_names(target)
            )
            if isinstance(target, (ast.Tuple, ast.List)) and None not in indices:
                names = [
                    _expr_name(item)
                    for index, item in enumerate(target.elts)
                    if index in indices
                ]
            for name in names:
                binding_line = facade_assignment_lines.get((scope, name))
                if (
                    name in bindings.get(scope, {})
                    and not indices
                    and binding_line is not None
                    and node.lineno >= binding_line
                ):
                    invalid_bindings.add((scope, name))
                if name.startswith(("self.", "cls.")) and not indices:
                    invalid_fields.add((class_scope, name))
    unclassified_names, unclassified_fields = _unclassified_receiver_stores(
        tree, scopes, class_scopes, recognized_stores
    )
    invalid_bindings.update(
        (scope, name) for scope, names in unclassified_names.items() for name in names
    )
    invalid_fields.update(
        (class_scope, name)
        for class_scope, names in unclassified_fields.items()
        for name in names
    )
    for scope, name in invalid_bindings:
        bindings[scope].pop(name, None)
    for class_scope, name in invalid_fields:
        fields_by_class.get(class_scope, set()).discard(name)

    return bindings, locals_by_scope, fields_by_class


def _is_facade_receiver(
    receiver: ast.expr,
    scope: str,
    bindings: dict[str, dict[str, bool]],
    locals_by_scope: dict[str, set[str]],
    fields_by_class: dict[str, set[str]],
    facade_imports: dict[str, set[str]],
    class_scopes: dict[int, str],
) -> bool:
    if isinstance(receiver, ast.NamedExpr):
        return _is_facade_receiver(
            receiver.value,
            scope,
            bindings,
            locals_by_scope,
            fields_by_class,
            facade_imports,
            class_scopes,
        )
    receiver_name = _expr_name(receiver)
    if isinstance(receiver, ast.Call):
        constructor = _expr_name(receiver.func)
        chain = _scope_chain(scope)
        class_scope = class_scopes[id(receiver)]
        imported: set[str] = set()
        for index, ancestor in enumerate(chain):
            if ancestor == class_scope and scope != class_scope:
                continue
            for alias in facade_imports.get(ancestor, set()):
                root_name = alias.split(".", 1)[0]
                if any(
                    root_name in locals_by_scope.get(descendant, set())
                    for descendant in chain[index:]
                ):
                    continue
                imported.add(alias)
        if any(
            constructor == alias or constructor == f"{alias}.system"
            for alias in imported
        ):
            return True
    variable = receiver_name.split(".", 1)[0]
    class_scope = class_scopes[id(receiver)]
    available = False
    for ancestor in _scope_chain(scope):
        if ancestor == class_scope and scope != class_scope:
            continue
        local_binding = bindings.get(ancestor, {}).get(receiver_name)
        if local_binding is not None:
            available = local_binding
        elif variable in locals_by_scope.get(ancestor, set()):
            available = False
    return available or receiver_name in fields_by_class.get(class_scope, set())


def _is_facade_call(
    node: ast.Call,
    scope: str,
    bindings: dict[str, dict[str, bool]],
    locals_by_scope: dict[str, set[str]],
    fields_by_class: dict[str, set[str]],
    facade_imports: dict[str, set[str]],
    class_scopes: dict[int, str],
) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in FACADE_METHODS
        and _is_facade_receiver(
            node.func.value,
            scope,
            bindings,
            locals_by_scope,
            fields_by_class,
            facade_imports,
            class_scopes,
        )
    )


def _valid_mode(value: str) -> bool:
    allowed = set("rwaxbt+")
    primaries = [char for char in value if char in "rwax"]
    return (
        bool(value)
        and all(char in allowed for char in value)
        and len(primaries) == 1
        and len(set(value)) == len(value)
        and not ("b" in value and "t" in value)
    )


def _mode(node: ast.Call, call: str) -> str | None:
    """Return a mode without mistaking a filename for an open mode."""
    keyword_mode = next(
        (keyword.value for keyword in node.keywords if keyword.arg == "mode"),
        None,
    )
    if keyword_mode is not None:
        return (
            keyword_mode.value
            if isinstance(keyword_mode, ast.Constant)
            and isinstance(keyword_mode.value, str)
            else None
        )
    if call in {
        "tarfile.open",
        "builtins.open",
        "open",
        "os.fdopen",
        "zipfile.ZipFile",
        "zipfile.ZipFile.open",
    }:
        mode_index = 1
        if len(node.args) <= mode_index:
            return "r"
        value = node.args[mode_index]
        return (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else None
        )
    if call == "pathlib.Path.open":
        mode_index = 0
        if len(node.args) <= mode_index:
            return "r"
        value = node.args[mode_index]
        return (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else None
        )
    if call.endswith(".open"):
        if not node.args:
            return "r"
        first = node.args[0]
        if len(node.args) == 1:
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value if _valid_mode(first.value) else "r"
            return None
        second = node.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            return second.value if _valid_mode(second.value) else None
        if isinstance(second, ast.Constant):
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if _valid_mode(first.value):
                    return first.value
            return None
        if not isinstance(second, ast.Constant):
            return None
    if len(node.args) <= 1:
        return "r"
    value = node.args[1]
    return (
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else None
    )


def _os_open_is_mutation(node: ast.Call) -> bool:
    """Recognize os.open flags which can create or modify a file.

    ``os.open`` is not mode-string based. Treat an unknown flag expression as
    reviewed rather than silently missing a write boundary; read-only constants
    remain excluded to keep the inventory useful.
    """
    if len(node.args) < 2:
        return True

    def _has_mutating_flag(value: ast.expr) -> bool | None:
        if isinstance(value, ast.Attribute):
            if value.attr in {
                "O_APPEND",
                "O_CREAT",
                "O_RDWR",
                "O_TRUNC",
                "O_WRONLY",
                "O_TMPFILE",
            }:
                return True
            if value.attr in {
                "O_RDONLY",
                "O_CLOEXEC",
                "O_DIRECTORY",
                "O_NOFOLLOW",
                "O_NONBLOCK",
                "O_SYNC",
                "O_DSYNC",
                "O_RSYNC",
                "O_DIRECT",
                "O_LARGEFILE",
                "O_PATH",
                "O_ASYNC",
                "O_NOCTTY",
            }:
                return False
            return None
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
            left = _has_mutating_flag(value.left)
            right = _has_mutating_flag(value.right)
            if left is None or right is None:
                return None
            return left or right
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return value.value != 0
        return None

    return _has_mutating_flag(node.args[1]) is not False


def _is_mutation(node: ast.Call, call: str) -> bool:
    if call == "os.open":
        return _os_open_is_mutation(node)
    if call == "zipfile.ZipFile":
        mode = _mode(node, call)
        return mode is None or any(flag in mode for flag in "wax+")
    if call == "zipfile.ZipFile.open":
        mode = _mode(node, call)
        return mode is None or "w" in mode
    if call in MUTATING_QUALIFIED:
        return True
    if call.rsplit(".", 1)[-1] in MUTATING_ATTRIBUTES:
        return True
    if call == "tarfile.open":
        mode = _mode(node, call)
        if mode is None:
            return True
        return any(flag in mode for flag in "wax+")
    if call == "os.fdopen":
        mode = _mode(node, call)
        if mode is None:
            return True
        return any(flag in mode for flag in "wax+")
    if call in {"open", "builtins.open"} or call.endswith(".open"):
        mode = _mode(node, call)
        if mode is None:
            return True
        return any(flag in mode for flag in "wax+")
    return False


def _inventory_from_sources(sources: Mapping[str, str]) -> list[Mutation]:
    found: list[Mutation] = []
    for relative, source in sources.items():
        tree = ast.parse(source, filename=relative)
        call_scopes, class_scopes = _call_scopes(tree)
        aliases_by_scope = _aliases_by_scope(tree, call_scopes)
        assigned_receivers, assigned_fields = _assigned_receivers_by_scope(
            tree, call_scopes, class_scopes
        )
        invalidated_names, invalidated_fields = _unclassified_receiver_stores(
            tree, call_scopes, class_scopes
        )
        for scope, names in invalidated_names.items():
            assigned_receivers.setdefault(scope, set()).update(names)
        for class_scope, names in invalidated_fields.items():
            assigned_fields.setdefault(class_scope, set()).update(names)
        path_bindings, path_fields = _path_bindings_by_scope(
            tree,
            call_scopes,
            class_scopes,
            aliases_by_scope,
            assigned_receivers,
            assigned_fields,
        )
        zipfile_bindings, zipfile_fields = _zipfile_bindings_by_scope(
            tree,
            call_scopes,
            class_scopes,
            aliases_by_scope,
            assigned_receivers,
            assigned_fields,
        )
        datetime_bindings, datetime_fields = _datetime_bindings_by_scope(
            tree,
            call_scopes,
            class_scopes,
            aliases_by_scope,
            assigned_receivers,
            assigned_fields,
        )
        facade_bindings, local_names, facade_fields = _facade_state_by_scope(
            tree, call_scopes, class_scopes
        )
        _, facade_imports = _scope_imports(tree, call_scopes)
        parents: dict[int, ast.AST] = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            scope = call_scopes[id(node)]
            aliases = aliases_by_scope[scope]
            call = ""
            is_mutation = False
            if isinstance(node, ast.Call):
                call = _resolved_call_name(node.func, aliases)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open"
                    and _is_zipfile_expression(
                        node.func.value,
                        scope,
                        class_scopes[id(node)],
                        zipfile_bindings,
                        zipfile_fields,
                        aliases,
                        assigned_receivers,
                        assigned_fields,
                    )
                ):
                    call = "zipfile.ZipFile.open"
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open"
                    and _is_path_expression(
                        node.func.value,
                        scope,
                        class_scopes[id(node)],
                        aliases_by_scope,
                        path_bindings,
                        path_fields,
                        assigned_receivers,
                        assigned_fields,
                    )
                ):
                    call = "pathlib.Path.open"
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "replace"
                    and (
                        _is_path_expression(
                            node.func.value,
                            scope,
                            class_scopes[id(node)],
                            aliases_by_scope,
                            path_bindings,
                            path_fields,
                            assigned_receivers,
                            assigned_fields,
                        )
                        or (
                            _path_replace_signature(node)
                            and not _numeric_path_replace_target(node)
                            and not _is_datetime_expression(
                                node.func.value,
                                scope,
                                class_scopes[id(node)],
                                aliases_by_scope,
                                datetime_bindings,
                                datetime_fields,
                                assigned_receivers,
                                assigned_fields,
                            )
                        )
                    )
                ):
                    call = "pathlib.Path.replace"
                is_mutation = _is_mutation(node, call) and not _is_facade_call(
                    node,
                    scope,
                    facade_bindings,
                    local_names,
                    facade_fields,
                    facade_imports,
                    class_scopes,
                )
            elif isinstance(node, (ast.Name, ast.Attribute)):
                parent = parents.get(id(node))
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in FACADE_METHODS
                    and _is_facade_receiver(
                        node.value,
                        scope,
                        facade_bindings,
                        local_names,
                        facade_fields,
                        facade_imports,
                        class_scopes,
                    )
                ):
                    continue
                referenced = _resolved_call_name(node, aliases)
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "replace"
                    and (
                        _is_path_expression(
                            node.value,
                            scope,
                            class_scopes[id(node)],
                            aliases_by_scope,
                            path_bindings,
                            path_fields,
                            assigned_receivers,
                            assigned_fields,
                        )
                        or (
                            not _is_datetime_expression(
                                node.value,
                                scope,
                                class_scopes[id(node)],
                                aliases_by_scope,
                                datetime_bindings,
                                datetime_fields,
                                assigned_receivers,
                                assigned_fields,
                            )
                            and not _known_nonpath_replace_receiver(node, aliases)
                        )
                    )
                ):
                    referenced = "pathlib.Path.replace"
                if _is_mutator_reference(referenced):
                    call = f"raw function reference {referenced}"
                    is_mutation = True
            elif isinstance(node, ast.alias):
                imported = aliases.get(node.asname or node.name, "")
                if _is_mutator_reference(imported):
                    call = f"raw function reference {imported}"
                    is_mutation = True
            if is_mutation:
                found.append(
                    Mutation(
                        relative,
                        node.lineno,
                        node.end_lineno or node.lineno,
                        call,
                        scope,
                    )
                )
    return sorted(
        found,
        key=lambda item: (
            item.path,
            item.line,
            item.end_line,
            item.call,
            item.scope,
        ),
    )


def _call_scopes(tree: ast.AST) -> tuple[dict[int, str], dict[int, str]]:
    """Map calls to their fully qualified lexical class/function scope."""

    class ScopeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.class_stack: list[str] = []
            self.scopes: dict[int, str] = {}
            self.class_scopes: dict[int, str] = {}

        def _visit_definition_header(self, node: ast.AST) -> None:
            for field in (
                "decorator_list",
                "bases",
                "keywords",
                "args",
                "returns",
                "type_params",
            ):
                value = getattr(node, field, None)
                if isinstance(value, list):
                    for child in value:
                        if isinstance(child, ast.AST):
                            self.visit(child)
                elif isinstance(value, ast.AST):
                    self.visit(value)

        def _visit_body(self, node: ast.AST, name: str) -> None:
            self._visit_definition_header(node)
            self.stack.append(name)
            for statement in getattr(node, "body", ()):
                self.visit(statement)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_definition_header(node)
            class_scope = ".".join((*self.stack, node.name))
            self.stack.append(node.name)
            self.class_stack.append(class_scope)
            for statement in node.body:
                self.visit(statement)
            self.class_stack.pop()
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_body(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_body(node, node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.visit(node.args)
            self.stack.append("<lambda>")
            self.visit(node.body)
            self.stack.pop()

        def visit(self, node: ast.AST) -> Any:
            self.scopes[id(node)] = ".".join(self.stack) or "<module>"
            self.class_scopes[id(node)] = (
                self.class_stack[-1] if self.class_stack else ""
            )
            return super().visit(node)

    visitor = ScopeVisitor()
    visitor.visit(tree)
    return visitor.scopes, visitor.class_scopes


def _production_sources(root: Path) -> dict[str, str]:
    paths = [
        path
        for path in root.rglob("*.py")
        if not any(
            part in EXCLUDED_SOURCE_DIRS for part in path.relative_to(root).parts
        )
        and path.name != "conftest.py"
        and not path.name.startswith("test_")
        and not path.name.endswith("_test.py")
    ]
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
    }


def _inventory(root: Path = ROOT) -> list[Mutation]:
    return _inventory_from_sources(_production_sources(root))


def _format_locations(mutations: Counter[Mutation]) -> str:
    if not mutations:
        return "<none>"
    values = []
    for mutation, count in sorted(
        mutations.items(),
        key=lambda pair: (
            pair[0].path,
            pair[0].line,
            pair[0].end_line,
            pair[0].call,
            pair[0].scope,
        ),
    ):
        location = (
            f"{mutation.path}:{mutation.line}:{mutation.call} [scope={mutation.scope}]"
        )
        values.append(f"{location} (x{count})" if count > 1 else location)
    return ", ".join(values)


def _raw_mutations_outside_wrapper() -> Counter[Mutation]:
    return Counter(item for item in _inventory() if item.path != WRAPPER_PATH)


def test_aliases_cannot_hide_os_open_mutations() -> None:
    tree = ast.parse(
        "from os import open as fd_open\nimport os as operating_system\n"
        "from pathlib import Path as LocalPath\n"
        "fd_open('created', operating_system.O_CREAT)\n"
        "operating_system.open('written', operating_system.O_WRONLY)\n"
        "LocalPath('written').open('w')\n"
    )
    aliases = _import_aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    resolved = [_resolved_call_name(node.func, aliases) for node in calls]
    assert resolved == ["os.open", "os.open", "pathlib.Path.open", "pathlib.Path"]
    assert all(_is_mutation(node, name) for node, name in zip(calls[:3], resolved[:3]))


def test_no_raw_mutations_outside_audited_filesystem() -> None:
    """Every recognized raw mutation must stay inside the central wrapper."""
    raw = _raw_mutations_outside_wrapper()
    assert not raw, (
        "Raw filesystem mutation calls must use AuditedFilesystem; migrate the "
        "operation and preserve ticket audit or explicit system_context:\n"
        f"{_format_locations(raw)}"
    )


def test_os_open_and_bound_path_open_writes_are_inventoried() -> None:
    """Regression coverage for the Wave 6 audit-inventory blind spots."""
    write_open = (
        ast.parse("os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)").body[0].value
    )
    read_open = ast.parse("os.open(path, os.O_RDONLY)").body[0].value
    bound_open = ast.parse('self.path.open("ab")').body[0].value
    assert isinstance(write_open, ast.Call)
    assert isinstance(read_open, ast.Call)
    assert isinstance(bound_open, ast.Call)
    assert _is_mutation(write_open, "os.open")
    assert not _is_mutation(read_open, "os.open")
    assert _is_mutation(bound_open, "self.path.open")


def test_leader_lease_write_uses_audited_filesystem() -> None:
    """Lease persistence must use the wrapper instead of a direct file write."""
    source = _production_sources(ROOT)["state_store/store.py"]
    assert "self._system_filesystem.write(self._lease_path.name, payload" in source
    mutations = _inventory_from_sources({"state_store/store.py": source})
    assert not any(
        mutation.scope.endswith("._write_orchestrator_lease") for mutation in mutations
    )
