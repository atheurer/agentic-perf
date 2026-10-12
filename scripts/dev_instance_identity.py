#!/usr/bin/env python3
"""Identity and safe state-transfer helpers for ``dev-instance.sh``.

This module deliberately uses only the standard library so that validation is
available before the development instance's virtual environment is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = 1
MANIFEST = "identity.manifest.json"
MANIFEST_DIGEST = "identity.manifest.sha256"
DEFAULT_PORT = 8090
EXCLUDED_NAMES = {
    "config.json",
    "state-store.id",
    "state-store.lock",
    "state-store-launch.lock",
    "orchestrator.pid",
    "audit.jsonl",
    "events.jsonl",
}
EXCLUDED_FIELDS = {
    "claim",
    "claims",
    "lease",
    "leases",
    "fence",
    "fences",
    "approval",
    "approvals",
    "pending_approval",
    "pending_stop",
    "pending_interject",
    "execution_operation",
    "operation",
    "orchestrator_lease",
}


def _validate_ticket_id(ticket_id: str) -> None:
    if (
        not ticket_id
        or ticket_id in {".", ".."}
        or "/" in ticket_id
        or "\\" in ticket_id
        or Path(ticket_id).name != ticket_id
    ):
        raise ValueError(
            f"invalid ticket ID {ticket_id!r}; IDs must be a single path component"
        )


def _canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare_repository_imports() -> None:
    """Make the local stdlib-only facade available before an instance venv exists."""
    repository = str(Path(__file__).resolve().parents[1])
    if repository not in sys.path:
        sys.path.insert(0, repository)


def create_manifest(home: str, worktree: str, name: str, port: int) -> None:
    _prepare_repository_imports()
    from providers.execution.filesystem import AuditedFilesystem

    target = Path(home)
    filesystem = AuditedFilesystem.system(target)
    filesystem.mkdir(".", mode=0o777)
    path = target / MANIFEST
    if path.exists():
        raise ValueError(f"identity manifest already exists: {path}")
    url = f"http://localhost:{port}"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": str(uuid.uuid4()),
        "instance_name": name,
        "runtime_home": _canonical(home),
        "worktree": _canonical(worktree),
        "port": port,
        "url": url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    filesystem.write(path.name, encoded, mode=0o444)
    filesystem.write(MANIFEST_DIGEST, _digest(encoded) + "\n", mode=0o444)
    # Identity is not a mutable provider setting.  The digest detects edits
    # while keeping the files portable across filesystems.
    filesystem.chmod(path.name, 0o444)
    filesystem.chmod(MANIFEST_DIGEST, 0o444)


def _load_manifest(home: str) -> tuple[dict, Path]:
    path = Path(home) / MANIFEST
    digest_path = Path(home) / MANIFEST_DIGEST
    if not path.is_file() or not digest_path.is_file():
        raise ValueError(
            f"managed identity manifest missing under {home}; recreate the instance"
        )
    raw = path.read_bytes()
    expected_digest = digest_path.read_text().strip()
    if _digest(raw) != expected_digest:
        raise ValueError(
            f"identity manifest was modified: {path}; do not repair it in place"
        )
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"identity manifest is invalid JSON: {path}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported identity manifest schema")
    return manifest, path


def _config(home: str) -> dict:
    path = Path(home) / "config.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"config file is invalid JSON: {path}") from exc


def _endpoint_is_free(host: str, port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, port)) != 0
    except OSError:
        # Restricted test sandboxes may prohibit socket creation.  The
        # state-store process lock remains the authoritative collision check.
        return True


def _expected_store(home: str) -> str | None:
    path = Path(home) / "state-store.id"
    return path.read_text().strip() if path.is_file() else None


def _endpoint_store_id(url: str, home: str) -> str | None:
    token_path = Path(home) / "secrets/api-token"
    headers = {}
    if token_path.is_file():
        headers["Authorization"] = f"Bearer {token_path.read_text().strip()}"
    request = urllib.request.Request(
        url.rstrip("/") + "/api/v1/diagnostics", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return json.load(response).get("store_id")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def validate(
    home: str, worktree: str, name: str, dangerous_default: bool = False
) -> dict:
    manifest, _ = _load_manifest(home)
    actual_home = _canonical(home)
    actual_worktree = _canonical(worktree)
    checks = {
        "instance name": (manifest.get("instance_name"), name),
        "runtime home": (manifest.get("runtime_home"), actual_home),
        "worktree": (manifest.get("worktree"), actual_worktree),
    }
    problems = [
        f"{label}: expected {expected!r}, actual {actual!r}"
        for label, (expected, actual) in checks.items()
        if expected != actual
    ]
    config = _config(home)
    state = config.get("state_store", {})
    parsed = urlparse(str(state.get("url", "")))
    config_port = state.get("port")
    expected_port = manifest.get("port")
    expected_url = manifest.get("url")
    for label, expected, actual in (
        ("config instance_name", name, config.get("instance_name")),
        ("config state-store URL", expected_url, state.get("url")),
        ("config state-store port", expected_port, config_port),
        ("URL port", expected_port, parsed.port),
    ):
        if expected != actual:
            problems.append(f"{label}: expected {expected!r}, actual {actual!r}")
    if expected_port == DEFAULT_PORT and not dangerous_default:
        problems.append(
            "default state-store port 8090 is forbidden; pass --dangerous-default "
            "only for an intentional collision test"
        )
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        problems.append(f"state-store host must be local, actual {parsed.hostname!r}")
    if isinstance(config_port, int) and not _endpoint_is_free(
        parsed.hostname or "localhost", config_port
    ):
        expected_store = _expected_store(home)
        remote_store = _endpoint_store_id(str(state.get("url")), home)
        if not expected_store or remote_store != expected_store:
            problems.append(
                f"endpoint {state.get('url')!r} is occupied by an unexpected store "
                f"(expected store_id {expected_store or '<none>'}, actual {remote_store or '<unknown>'})"
            )
    if problems:
        raise ValueError(
            "managed instance identity validation failed:\n  " + "\n  ".join(problems)
        )
    return manifest


def _sanitize(value: object, field: str = "") -> object:
    if isinstance(value, dict):
        return {
            key: _sanitize(item, key)
            for key, item in value.items()
            if key.lower() not in EXCLUDED_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize(item, field) for item in value]
    return value


def import_state(
    source_home: str, destination_home: str, ids: list[str], apply: bool
) -> int:
    if not ids:
        raise ValueError("import-state requires at least one --ticket ID")
    source = Path(source_home)
    destination = Path(destination_home)
    source_manifest, _ = _load_manifest(source)
    destination_manifest, _ = _load_manifest(destination)
    if source_manifest["manifest_id"] == destination_manifest["manifest_id"]:
        raise ValueError("source and destination must be different managed instances")
    outputs: list[tuple[Path, dict]] = []
    for ticket_id in ids:
        _validate_ticket_id(ticket_id)
        src = source / "tickets" / f"{ticket_id}.json"
        dst = destination / "tickets" / f"{ticket_id}.json"
        if dst.exists():
            raise ValueError(f"destination ticket collision: {dst}")
        if not src.is_file():
            raise ValueError(f"source ticket not found: {src}")
        record = _sanitize(json.loads(src.read_text()))
        if not isinstance(record, dict):
            raise ValueError(f"ticket fixture is not an object: {src}")
        if record.get("id") != ticket_id:
            raise ValueError(
                f"source ticket ID mismatch: selected {ticket_id!r}, "
                f"record contains {record.get('id')!r}"
            )
        original_status = record.get("status")
        record["status"] = "awaiting_customer_guidance"
        record["previous_status"] = None
        fields = record.setdefault("custom_fields", {})
        if isinstance(fields, dict):
            fields["imported_fixture"] = True
            fields["import_provenance"] = {
                "source_instance": source_manifest["instance_name"],
                "source_store_id": _expected_store(str(source)) or "unavailable",
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "selected_objects": [ticket_id],
                "sanitized_fields": sorted(EXCLUDED_FIELDS),
                "destination_instance": destination_manifest["instance_name"],
                "original_status": original_status,
                "resume_requires_review": True,
            }
        outputs.append((dst, record))
    print(
        f"Import {'APPLY' if apply else 'DRY-RUN'}: {source_home} -> {destination_home}"
    )
    for path, record in outputs:
        print(f"  include {path.name} (status -> {record['status']})")
        print(
            "  exclude config.json, secrets, PID/lock files, logs, audit, claims, leases, approvals"
        )
    if apply:
        _prepare_repository_imports()
        from providers.execution.filesystem import AuditedFilesystem

        filesystem = AuditedFilesystem.system(destination)
        filesystem.mkdir("tickets", mode=0o777)
        for path, record in outputs:
            filesystem.write(
                path.relative_to(destination),
                json.dumps(record, indent=2, default=str) + "\n",
                mode=None,
                atomic=False,
            )
    return len(outputs)


def token_fingerprint(home: str) -> str | None:
    path = Path(home) / "secrets/api-token"
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes().strip()).hexdigest()[:12]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--home", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--port", required=True, type=int)
    check = sub.add_parser("validate")
    check.add_argument("--home", required=True)
    check.add_argument("--worktree", required=True)
    check.add_argument("--name", required=True)
    check.add_argument("--dangerous-default", action="store_true")
    transfer = sub.add_parser("import-state")
    transfer.add_argument("--source-home", required=True)
    transfer.add_argument("--destination-home", required=True)
    transfer.add_argument("--ticket", action="append", default=[])
    transfer.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "create":
            create_manifest(args.home, args.worktree, args.name, args.port)
        elif args.command == "validate":
            validate(args.home, args.worktree, args.name, args.dangerous_default)
        else:
            import_state(
                args.source_home, args.destination_home, args.ticket, args.apply
            )
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
