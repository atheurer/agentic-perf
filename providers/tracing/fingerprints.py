"""Stable payload fingerprints that never expose a plain secret hash."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from providers.execution import AuditedFilesystem


def load_audit_key(key_path: Path) -> bytes:
    """Load or atomically create the deployment-local HMAC audit key."""
    filesystem = AuditedFilesystem.system(key_path.parent)
    filesystem.mkdir(".")
    filesystem.chmod(".", 0o700)

    def _read_existing() -> bytes:
        key = key_path.read_bytes()
        if len(key) < 32:
            raise ValueError("trace audit key is too short")
        filesystem.chmod(key_path.name, 0o600)
        return key

    try:
        key = _read_existing()
        _fsync_directory(key_path.parent)
        return key
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        temp_name = filesystem.temporary_file(prefix=".trace-audit-key-")
        try:
            filesystem.write(temp_name, key, mode=0o600, atomic=False)
            try:
                # link(2) publishes only if no concurrent creator won first.
                filesystem.hardlink(temp_name, key_path.name)
            except FileExistsError:
                _fsync_directory(key_path.parent)
                return _read_existing()
            _fsync_directory(key_path.parent)
            return key
        finally:
            try:
                filesystem.unlink(temp_name, missing_ok=True)
            except FileNotFoundError:
                pass


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fingerprint(
    data: bytes, *, sensitive: bool, audit_key: bytes | None = None
) -> tuple[str, str]:
    """Return a digest and its algorithm label for correlation only."""
    if sensitive:
        if audit_key is None:
            raise ValueError("sensitive payload fingerprints require an audit key")
        return hmac.new(audit_key, data, hashlib.sha256).hexdigest(), "hmac-sha256"
    return hashlib.sha256(data).hexdigest(), "sha256"
