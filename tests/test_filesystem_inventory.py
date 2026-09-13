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
    "os.link",
    "os.makedirs",
    "os.mkdir",
    "os.replace",
    "os.rename",
    "os.unlink",
    "os.remove",
    "os.rmdir",
    "os.symlink",
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
    line: int
    call: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _mode(node: ast.Call) -> str | None:
    value: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            value = keyword.value
            break
    return (
        value.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
        else None
    )


def _is_mutation(node: ast.Call, call: str) -> bool:
    if call in MUTATING_QUALIFIED:
        return True
    if call.rsplit(".", 1)[-1] in MUTATING_ATTRIBUTES:
        return True
    if call == "tarfile.open":
        mode = _mode(node) or "r"
        return any(flag in mode for flag in "wax+")
    if call == "os.fdopen":
        mode = _mode(node) or "r"
        return any(flag in mode for flag in "wax+")
    if call == "open" or call.endswith(".open"):
        mode = _mode(node) or "r"
        return any(flag in mode for flag in "wax+")
    return False


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
            if _is_mutation(node, call):
                found.append(Mutation(relative, node.lineno, call))
    return sorted(found, key=lambda item: (item.path, item.line, item.call))


# Fixed review manifest. Each entry is a deliberate audited-boundary primitive,
# an explicit no-ticket compatibility fallback, or a non-ticket/internal write.
# The equality assertion below makes source additions, removals, and line moves
# fail until a reviewer updates this list and the rationale document together.
_EXPECTED_MANIFEST = """
agents/benchmark/server.py|3061|tempfile.NamedTemporaryFile
agents/benchmark/server.py|3072|staging.unlink
agents/benchmark/server.py|3074|unlink
agents/benchmark/server.py|3256|tempfile.NamedTemporaryFile
agents/benchmark/server.py|3361|staging.unlink
agents/benchmark/server.py|3363|unlink
agents/benchmark/server.py|3681|open
agents/benchmark/server.py|3762|artifact_filesystem.unlink
agents/benchmark/server.py|3764|serial_log_path.unlink
agents/benchmark/server.py|3795|metadata_file.write_bytes
agents/benchmark/server.py|3802|metadata_file.write_text
agents/benchmark/server.py|3808|metadata_file.write_text
agents/benchmark/server.py|3863|merged_file.write_bytes
agents/infra/server.py|232|tempfile.NamedTemporaryFile
agents/infra/server.py|240|staging.unlink
agents/infra/server.py|242|unlink
agents/infra/server.py|307|tempfile.mkdtemp
orchestrator/main.py|1857|LOCK_FILE.parent.mkdir
orchestrator/main.py|1889|LOCK_FILE.unlink
paths.py|145|mkdir
paths.py|152|tempfile.mkdtemp
paths.py|170|ws_dir.mkdir
paths.py|174|temp_dir.mkdir
providers/events.py|149|self._log_dir.mkdir
providers/execution/filesystem.py|359|path.mkdir
providers/execution/filesystem.py|385|path.parent.mkdir
providers/execution/filesystem.py|386|open
providers/execution/filesystem.py|387|os.chmod
providers/execution/filesystem.py|431|path.parent.mkdir
providers/execution/filesystem.py|435|tempfile.mkstemp
providers/execution/filesystem.py|436|os.fdopen
providers/execution/filesystem.py|438|open
providers/execution/filesystem.py|440|os.chmod
providers/execution/filesystem.py|446|os.replace
providers/execution/filesystem.py|454|os.unlink
providers/execution/filesystem.py|477|source_path.rename
providers/execution/filesystem.py|486|path.unlink
providers/execution/filesystem.py|499|target.parent.mkdir
providers/execution/filesystem.py|500|tempfile.mkstemp
providers/execution/filesystem.py|505|tarfile.open
providers/execution/filesystem.py|508|os.replace
providers/execution/filesystem.py|515|os.unlink
providers/image_build/caib.py|193|tempfile.NamedTemporaryFile
providers/image_build/caib.py|339|unlink
providers/investigation/file.py|42|self._dir.mkdir
providers/investigation/file.py|52|path.write_text
providers/quota.py|113|self._log_dir.mkdir
providers/quota.py|131|open
providers/resource/jumpstarter.py|138|user_config.parent.mkdir
providers/resource/jumpstarter.py|139|user_config.write_text
providers/resource/jumpstarter_provision.py|121|tempfile.mktemp
providers/resource/jumpstarter_provision.py|126|open
providers/secrets/bitwarden.py|217|tempfile.mkdtemp
providers/secrets/bitwarden.py|219|tmp_dir.chmod
providers/secrets/bitwarden.py|221|tmp_file.write_text
providers/secrets/bitwarden.py|222|tmp_file.chmod
providers/secrets/bitwarden.py|227|child.unlink
providers/secrets/bitwarden.py|228|tmp_dir.rmdir
providers/skills/arcaflow_plugins.py|37|self._cache_dir.mkdir
providers/skills/arcaflow_plugins.py|70|path.write_text
providers/skills/repo_cache.py|34|repo_path.parent.mkdir
providers/tracing/fingerprints.py|15|key_path.parent.mkdir
providers/tracing/fingerprints.py|16|os.chmod
providers/tracing/fingerprints.py|22|os.chmod
providers/tracing/fingerprints.py|31|tempfile.mkstemp
providers/tracing/fingerprints.py|36|os.fdopen
providers/tracing/fingerprints.py|42|os.link
providers/tracing/fingerprints.py|50|os.unlink
providers/tracing/payloads.py|76|self.directory.mkdir
providers/tracing/payloads.py|77|os.chmod
providers/tracing/payloads.py|86|os.chmod
providers/tracing/payloads.py|93|tempfile.mkstemp
providers/tracing/payloads.py|96|os.fdopen
providers/tracing/payloads.py|102|os.replace
providers/tracing/payloads.py|110|os.unlink
providers/tracing/payloads.py|113|os.chmod
providers/tracing/spool.py|59|self.directory.mkdir
providers/tracing/spool.py|62|os.chmod
providers/tracing/spool.py|89|self.lock_path.unlink
providers/tracing/spool.py|103|os.chmod
providers/tracing/spool.py|138|tempfile.mkstemp
providers/tracing/spool.py|141|os.fdopen
providers/tracing/spool.py|145|os.replace
providers/tracing/spool.py|149|os.unlink
providers/tracing/spool.py|195|tempfile.mkstemp
providers/tracing/spool.py|198|os.fdopen
providers/tracing/spool.py|208|os.replace
providers/tracing/spool.py|212|os.unlink
providers/tracing/spool.py|223|self.path.unlink
providers/tracing/spool.py|224|self.ack_path.unlink
providers/tracing/spool.py|225|self.lock_path.unlink
providers/tracing/spool.py|234|os.replace
providers/tracing/spool.py|235|self.path.touch
providers/tracing/spool.py|236|self.ack_path.unlink
providers/workspace/manager.py|72|self._filesystem.mkdir
state_store/api/artifacts.py|136|filesystem.unlink
state_store/audit.py|42|self._path.parent.mkdir
state_store/auth.py|66|SECRETS_DIR.mkdir
state_store/auth.py|68|TOKEN_FILE.write_text
state_store/auth.py|69|TOKEN_FILE.chmod
state_store/auth.py|81|SECRETS_DIR.mkdir
state_store/auth.py|83|VALIDATOR_TOKEN_FILE.write_text
state_store/auth.py|84|VALIDATOR_TOKEN_FILE.chmod
state_store/identity.py|387|self._path.parent.mkdir
state_store/identity.py|388|tempfile.mkstemp
state_store/identity.py|393|os.fdopen
state_store/identity.py|395|os.chmod
state_store/identity.py|396|os.replace
state_store/identity.py|399|os.unlink
state_store/store.py|65|self._persist_dir.mkdir
state_store/process_lock.py|56|path.parent.mkdir
state_store/process_lock.py|72|os.replace
state_store/process_lock.py|80|temporary.unlink
state_store/process_lock.py|107|self.root.mkdir
state_store/store.py|799|filesystem.mkdir
state_store/store.py|806|filesystem.mkdir
state_store/store.py|819|log_filesystem.rename
state_store/store.py|828|filesystem.rename
state_store/trace_store.py|77|self.db_path.parent.mkdir
"""


def _expected_manifest() -> frozenset[Mutation]:
    entries = []
    for raw in _EXPECTED_MANIFEST.strip().splitlines():
        path, line, call = raw.split("|", 2)
        entries.append(Mutation(path, int(line), call))
    return frozenset(entries)


EXPECTED_MUTATIONS = _expected_manifest()


def test_full_production_mutation_inventory_has_reviewed_exclusions() -> None:
    """Every production mutation exactly matches the reviewed manifest."""
    document = (ROOT / "docs/filesystem-audit-inventory.md").read_text()
    assert "fixed `file:line:call` manifest" in document
    actual = frozenset(_inventory())
    missing = sorted(
        EXPECTED_MUTATIONS - actual, key=lambda item: (item.path, item.line, item.call)
    )
    added = sorted(
        actual - EXPECTED_MUTATIONS, key=lambda item: (item.path, item.line, item.call)
    )
    assert not missing and not added, (
        "filesystem mutation inventory changed; review each delta and update the "
        f"fixed manifest. missing={missing!r}, added={added!r}"
    )
