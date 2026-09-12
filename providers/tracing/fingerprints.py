"""Stable payload fingerprints that never expose a plain secret hash."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path


def load_audit_key(key_path: Path) -> bytes:
    """Load or atomically create the deployment-local HMAC audit key."""
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_path.parent, 0o700)

    def _read_existing() -> bytes:
        key = key_path.read_bytes()
        if len(key) < 32:
            raise ValueError("trace audit key is too short")
        os.chmod(key_path, 0o600)
        return key

    try:
        key = _read_existing()
        _fsync_directory(key_path.parent)
        return key
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd, temp_name = tempfile.mkstemp(
            prefix=".trace-audit-key-", dir=key_path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # link(2) publishes only if no concurrent creator won first.
                os.link(temp_name, key_path)
            except FileExistsError:
                _fsync_directory(key_path.parent)
                return _read_existing()
            _fsync_directory(key_path.parent)
            return key
        finally:
            try:
                os.unlink(temp_name)
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
