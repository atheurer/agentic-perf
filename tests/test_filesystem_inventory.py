"""AST inventory preventing unaudited ticket filesystem regressions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[1]
PRODUCTION_TREES = ("agents", "orchestrator", "providers", "state_store")
MUTATING_ATTRIBUTES = {
    "chmod",
    "chown",
    "hardlink_to",
    "mkdir",
    "rename",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
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
    "shutil.move",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copytree",
    "shutil.rmtree",
    "tempfile.mkdtemp",
    "tempfile.mktemp",
    "tempfile.mkstemp",
    "tempfile.NamedTemporaryFile",
}


@dataclass(frozen=True)
class Mutation:
    path: str
    scope: str  # enclosing function/method name, or "<module>"
    call: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map locally-spelled imports back to their protected API names."""
    aliases: dict[str, str] = {}
    protected_modules = {"os", "pathlib", "shutil", "tempfile", "tarfile"}
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


def _mode(node: ast.Call, call: str) -> str | None:
    # Path.open(name, mode) has its mode as the first positional argument,
    # while built-in open/tarfile.open and os.fdopen place mode second.
    positional_mode = (
        0 if call.endswith(".open") and call not in {"tarfile.open", "open"} else 1
    )
    value: ast.expr | None = (
        node.args[positional_mode] if len(node.args) > positional_mode else None
    )
    for keyword in node.keywords:
        if keyword.arg == "mode":
            value = keyword.value
            break
    return (
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else None
    )


def _os_open_is_mutation(node: ast.Call) -> bool:
    """Recognize os.open flags which can create or modify a file.

    ``os.open`` is not mode-string based.  Treat an unknown flag expression as
    reviewed rather than silently missing a write boundary; read-only constants
    remain excluded to keep the manifest useful.
    """
    if len(node.args) < 2:
        return True

    def _has_mutating_flag(value: ast.expr) -> bool | None:
        if isinstance(value, ast.Attribute):
            return value.attr in {
                "O_APPEND",
                "O_CREAT",
                "O_RDWR",
                "O_TRUNC",
                "O_WRONLY",
            }
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
    if call in MUTATING_QUALIFIED:
        return True
    if call.rsplit(".", 1)[-1] in MUTATING_ATTRIBUTES:
        return True
    if call == "tarfile.open":
        mode = _mode(node, call) or "r"
        return any(flag in mode for flag in "wax+")
    if call == "os.fdopen":
        mode = _mode(node, call) or "r"
        return any(flag in mode for flag in "wax+")
    if call == "open" or call.endswith(".open"):
        mode = _mode(node, call) or "r"
        return any(flag in mode for flag in "wax+")
    return False


def _enclosing_scope(node: ast.AST, parents: dict[int, ast.AST]) -> str:
    """Walk up the AST to find the enclosing function/method name."""
    current = node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            return "<module>"
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        current = parent


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Map each node id to its parent for scope lookups."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _inventory() -> list[Mutation]:
    paths = [ROOT / "paths.py"]
    for tree_dir in PRODUCTION_TREES:
        paths.extend((ROOT / tree_dir).rglob("*.py"))
    found: list[Mutation] = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        aliases = _import_aliases(tree)
        parents = _build_parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _resolved_call_name(node.func, aliases)
            if _is_mutation(node, call):
                scope = _enclosing_scope(node, parents)
                found.append(Mutation(relative, scope, call))
    return sorted(found, key=lambda item: (item.path, item.scope, item.call))


# Fixed review manifest. Each entry is a deliberate audited-boundary primitive,
# an explicit no-ticket compatibility fallback, or a non-ticket/internal write.
# The equality assertion below makes source additions, removals, and line moves
# fail until a reviewer updates this list and the rationale document together.
_EXPECTED_MANIFEST = """
agents/benchmark/server.py|execute_benchmark|staging.unlink
agents/benchmark/server.py|execute_benchmark|tempfile.NamedTemporaryFile
agents/benchmark/server.py|execute_benchmark|unlink
agents/benchmark/server.py|execute_boot_time_test|artifact_filesystem.unlink
agents/benchmark/server.py|execute_boot_time_test|diag_file.write_text
agents/benchmark/server.py|execute_boot_time_test|merged_file.write_bytes
agents/benchmark/server.py|execute_boot_time_test|metadata_file.write_bytes
agents/benchmark/server.py|execute_boot_time_test|metadata_file.write_text
agents/benchmark/server.py|execute_boot_time_test|metadata_file.write_text
agents/benchmark/server.py|execute_boot_time_test|open
agents/benchmark/server.py|execute_boot_time_test|serial_log_path.unlink
agents/benchmark/server.py|validate_benchmark|staging.unlink
agents/benchmark/server.py|validate_benchmark|tempfile.NamedTemporaryFile
agents/benchmark/server.py|validate_benchmark|unlink
agents/infra/server.py|read_remote_dir|filesystem.mkdir
agents/infra/server.py|read_remote_dir|tempfile.mkdtemp
agents/infra/server.py|write_remote_file|staging.unlink
agents/infra/server.py|write_remote_file|tempfile.NamedTemporaryFile
agents/infra/server.py|write_remote_file|unlink
orchestrator/config.py|write_effective_config|destination.parent.mkdir
orchestrator/config.py|write_effective_config|os.fdopen
orchestrator/config.py|write_effective_config|os.replace
orchestrator/config.py|write_effective_config|os.unlink
orchestrator/config.py|write_effective_config|tempfile.mkstemp
orchestrator/main.py|_acquire_lock|LOCK_FILE.parent.mkdir
orchestrator/main.py|_acquire_lock|os.ftruncate
orchestrator/main.py|_acquire_lock|os.open
orchestrator/main.py|_acquire_lock|os.write
orchestrator/main.py|_release_lock|LOCK_FILE.unlink
paths.py|create_artifact_dir|mkdir
paths.py|create_artifact_dir|tempfile.mkdtemp
paths.py|get_ticket_workspace_dir|temp_dir.mkdir
paths.py|get_ticket_workspace_dir|ws_dir.mkdir
providers/events.py|__init__|self._log_dir.mkdir
providers/execution/filesystem.py|create_archive|os.replace
providers/execution/filesystem.py|create_archive|os.unlink
providers/execution/filesystem.py|create_archive|tarfile.open
providers/execution/filesystem.py|create_archive|target.parent.mkdir
providers/execution/filesystem.py|create_archive|tempfile.mkstemp
providers/execution/filesystem.py|mkdir|path.mkdir
providers/execution/filesystem.py|open_stream|open
providers/execution/filesystem.py|open_stream|os.chmod
providers/execution/filesystem.py|open_stream|path.parent.mkdir
providers/execution/filesystem.py|rename|source_path.rename
providers/execution/filesystem.py|unlink|path.unlink
providers/execution/filesystem.py|write_file|open
providers/execution/filesystem.py|write_file|os.chmod
providers/execution/filesystem.py|write_file|os.fdopen
providers/execution/filesystem.py|write_file|os.replace
providers/execution/filesystem.py|write_file|os.unlink
providers/execution/filesystem.py|write_file|path.parent.mkdir
providers/execution/filesystem.py|write_file|tempfile.mkstemp
providers/image_build/caib.py|build|tempfile.NamedTemporaryFile
providers/image_build/caib.py|build|unlink
providers/investigation/file.py|__init__|self._dir.mkdir
providers/investigation/file.py|_write|path.write_text
providers/quota.py|__init__|self._log_dir.mkdir
providers/quota.py|append|open
providers/resource/jumpstarter.py|from_secrets|user_config.parent.mkdir
providers/resource/jumpstarter.py|from_secrets|user_config.write_text
providers/resource/jumpstarter_provision.py|provision_jumpstarter|open
providers/resource/jumpstarter_provision.py|provision_jumpstarter|tempfile.mktemp
providers/secrets/bitwarden.py|secret_file|child.unlink
providers/secrets/bitwarden.py|secret_file|tempfile.mkdtemp
providers/secrets/bitwarden.py|secret_file|tmp_dir.chmod
providers/secrets/bitwarden.py|secret_file|tmp_dir.rmdir
providers/secrets/bitwarden.py|secret_file|tmp_file.chmod
providers/secrets/bitwarden.py|secret_file|tmp_file.write_text
providers/skills/arcaflow_plugins.py|__init__|self._cache_dir.mkdir
providers/skills/arcaflow_plugins.py|put|path.write_text
providers/skills/repo_cache.py|ensure_repo|repo_path.parent.mkdir
providers/tracing/fingerprints.py|_read_existing|os.chmod
providers/tracing/fingerprints.py|load_audit_key|key_path.parent.mkdir
providers/tracing/fingerprints.py|load_audit_key|os.chmod
providers/tracing/fingerprints.py|load_audit_key|os.fchmod
providers/tracing/fingerprints.py|load_audit_key|os.fdopen
providers/tracing/fingerprints.py|load_audit_key|os.link
providers/tracing/fingerprints.py|load_audit_key|os.unlink
providers/tracing/fingerprints.py|load_audit_key|tempfile.mkstemp
providers/tracing/payloads.py|put|os.chmod
providers/tracing/payloads.py|put|os.chmod
providers/tracing/payloads.py|put|os.chmod
providers/tracing/payloads.py|put|os.fchmod
providers/tracing/payloads.py|put|os.fdopen
providers/tracing/payloads.py|put|os.replace
providers/tracing/payloads.py|put|os.unlink
providers/tracing/payloads.py|put|self.directory.mkdir
providers/tracing/payloads.py|put|tempfile.mkstemp
providers/tracing/spool.py|__init__|os.chmod
providers/tracing/spool.py|__init__|os.chmod
providers/tracing/spool.py|__init__|os.fchmod
providers/tracing/spool.py|__init__|os.open
providers/tracing/spool.py|__init__|os.open
providers/tracing/spool.py|__init__|self.directory.mkdir
providers/tracing/spool.py|__init__|self.lock_path.unlink
providers/tracing/spool.py|_quarantine|os.replace
providers/tracing/spool.py|_quarantine|self.ack_path.unlink
providers/tracing/spool.py|_quarantine|self.path.touch
providers/tracing/spool.py|_write_ack|os.fchmod
providers/tracing/spool.py|_write_ack|os.fdopen
providers/tracing/spool.py|_write_ack|os.replace
providers/tracing/spool.py|_write_ack|os.unlink
providers/tracing/spool.py|_write_ack|tempfile.mkstemp
providers/tracing/spool.py|append|self.path.open
providers/tracing/spool.py|close|self.ack_path.unlink
providers/tracing/spool.py|close|self.lock_path.unlink
providers/tracing/spool.py|close|self.path.unlink
providers/tracing/spool.py|compact|os.fchmod
providers/tracing/spool.py|compact|os.fdopen
providers/tracing/spool.py|compact|os.replace
providers/tracing/spool.py|compact|os.unlink
providers/tracing/spool.py|compact|tempfile.mkstemp
providers/workspace/manager.py|__init__|self._filesystem.mkdir
state_store/api/artifacts.py|download_archive|filesystem.unlink
state_store/audit.py|__init__|self._path.parent.mkdir
state_store/auth.py|load_or_generate_token|SECRETS_DIR.mkdir
state_store/auth.py|load_or_generate_token|TOKEN_FILE.chmod
state_store/auth.py|load_or_generate_token|TOKEN_FILE.write_text
state_store/auth.py|load_or_generate_validator_token|SECRETS_DIR.mkdir
state_store/auth.py|load_or_generate_validator_token|VALIDATOR_TOKEN_FILE.chmod
state_store/auth.py|load_or_generate_validator_token|VALIDATOR_TOKEN_FILE.write_text
state_store/identity.py|_save|os.chmod
state_store/identity.py|_save|os.fdopen
state_store/identity.py|_save|os.replace
state_store/identity.py|_save|os.unlink
state_store/identity.py|_save|self._path.parent.mkdir
state_store/identity.py|_save|tempfile.mkstemp
state_store/process_lock.py|acquire|os.ftruncate
state_store/process_lock.py|acquire|os.open
state_store/process_lock.py|acquire|os.write
state_store/process_lock.py|acquire|self.root.mkdir
state_store/process_lock.py|ensure_store_id|os.open
state_store/process_lock.py|ensure_store_id|os.replace
state_store/process_lock.py|ensure_store_id|os.write
state_store/process_lock.py|ensure_store_id|path.parent.mkdir
state_store/process_lock.py|ensure_store_id|temporary.unlink
state_store/store.py|__init__|self._persist_dir.mkdir
state_store/store.py|_write_orchestrator_lease|os.replace
state_store/store.py|_write_orchestrator_lease|self._lease_path.unlink
state_store/store.py|_write_orchestrator_lease|temporary.open
state_store/store.py|archive_ticket|filesystem.mkdir
state_store/store.py|archive_ticket|filesystem.mkdir
state_store/store.py|archive_ticket|filesystem.rename
state_store/store.py|archive_ticket|log_filesystem.rename
state_store/trace_store.py|__init__|self.db_path.parent.mkdir
"""


def _expected_manifest() -> list[Mutation]:
    entries = []
    for raw in _EXPECTED_MANIFEST.strip().splitlines():
        path, scope, call = raw.split("|", 2)
        entries.append(Mutation(path, scope, call))
    return sorted(entries, key=lambda m: (m.path, m.scope, m.call))


EXPECTED_MUTATIONS = _expected_manifest()


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


def test_full_production_mutation_inventory_has_reviewed_exclusions() -> None:
    """Every production mutation exactly matches the reviewed manifest."""
    document = (ROOT / "docs/filesystem-audit-inventory.md").read_text()
    assert "fixed `file:" in document
    actual = sorted(_inventory(), key=lambda m: (m.path, m.scope, m.call))
    expected = EXPECTED_MUTATIONS  # already sorted
    # Compare as sorted lists to handle duplicate scope+call pairs
    if actual != expected:
        actual_set = set((m.path, m.scope, m.call) for m in actual)
        expected_set = set((m.path, m.scope, m.call) for m in expected)
        missing = sorted(expected_set - actual_set)
        added = sorted(actual_set - expected_set)
        raise AssertionError(
            "filesystem mutation inventory changed; review each delta "
            "and update the fixed manifest. "
            f"missing={[Mutation(*m) for m in missing]!r}, "
            f"added={[Mutation(*m) for m in added]!r}"
        )


def test_leader_lease_temporary_write_is_inventoried() -> None:
    """Path.open write modes must remain visible to the mutation scanner."""
    assert any(
        mutation.path == "state_store/store.py" and mutation.call == "temporary.open"
        for mutation in _inventory()
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
