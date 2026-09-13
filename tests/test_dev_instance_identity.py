from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "dev_instance_identity",
    Path(__file__).parents[1] / "scripts/dev_instance_identity.py",
)
assert _SPEC and _SPEC.loader
identity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(identity)


def _instance(root: Path, name: str, port: int) -> tuple[Path, Path]:
    home = root / name
    worktree = root / f"{name}-repo"
    worktree.mkdir()
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "instance_name": name,
                "state_store": {
                    "url": f"http://localhost:{port}",
                    "port": port,
                },
            }
        )
    )
    identity.create_manifest(str(home), str(worktree), name, port)
    return home, worktree


def test_manifest_and_config_identity_are_checked(tmp_path: Path) -> None:
    home, worktree = _instance(tmp_path, "one", 18101)

    manifest = identity.validate(str(home), str(worktree), "one")

    assert manifest["schema_version"] == 1
    assert manifest["runtime_home"] == str(home.resolve())
    assert manifest["worktree"] == str(worktree.resolve())

    config = json.loads((home / "config.json").read_text())
    config["instance_name"] = "copied-default"
    (home / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="config instance_name"):
        identity.validate(str(home), str(worktree), "one")


def test_manifest_digest_rejects_identity_edits(tmp_path: Path) -> None:
    home, worktree = _instance(tmp_path, "one", 18102)
    manifest_path = home / identity.MANIFEST
    manifest_path.chmod(0o644)
    manifest_path.write_text(manifest_path.read_text().replace('"one"', '"other"'))
    with pytest.raises(ValueError, match="modified"):
        identity.validate(str(home), str(worktree), "one")


def test_import_is_dry_run_by_default_and_sanitizes_fixture(tmp_path: Path) -> None:
    source, _ = _instance(tmp_path, "source", 18103)
    destination, _ = _instance(tmp_path, "destination", 18104)
    tickets = source / "tickets"
    tickets.mkdir()
    (tickets / "PERF-1.json").write_text(
        json.dumps(
            {
                "id": "PERF-1",
                "status": "executing_benchmark",
                "custom_fields": {
                    "claim": {"owner": "source"},
                    "pending_approval": True,
                },
            }
        )
    )

    assert identity.import_state(str(source), str(destination), ["PERF-1"], False) == 1
    assert not (destination / "tickets/PERF-1.json").exists()
    identity.import_state(str(source), str(destination), ["PERF-1"], True)
    imported = json.loads((destination / "tickets/PERF-1.json").read_text())
    assert imported["status"] == "awaiting_customer_guidance"
    assert "claim" not in imported["custom_fields"]
    assert "pending_approval" not in imported["custom_fields"]
    assert imported["custom_fields"]["imported_fixture"] is True

    with pytest.raises(ValueError, match="destination ticket collision"):
        identity.import_state(str(source), str(destination), ["PERF-1"], False)
