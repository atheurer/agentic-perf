"""Bounded, redacted payload descriptors and private content-addressed blobs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from paths import TRACE_AUDIT_KEY_PATH, TRACE_PAYLOAD_DIR
from providers.redaction import Redactor

from .fingerprints import fingerprint, load_audit_key
from .models import PayloadDescriptor

DEFAULT_INLINE_BYTES = 4096
_REDACTION_ERROR = "[REDACTED:redaction_error]"
_BINARY_OMITTED = "[REDACTED:binary_omitted]"


class PayloadStorageError(RuntimeError):
    """A payload could not be safely redacted or durably stored."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonicalize_payload(
    payload: Any, media_type: str | None = None
) -> tuple[bytes, Any | None, str]:
    """Return canonical bytes, parsed structured content, and its media type."""
    if isinstance(payload, bytes):
        return payload, None, media_type or "application/octet-stream"
    if isinstance(payload, bytearray):
        return bytes(payload), None, media_type or "application/octet-stream"
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload.encode("utf-8"), None, media_type or "text/plain"
        return _canonical_json(parsed), parsed, media_type or "application/json"
    return _canonical_json(payload), payload, media_type or "application/json"


class PayloadBlobStore:
    """Private redacted blob files, addressed by their safe content hash."""

    def __init__(
        self, directory: Path = TRACE_PAYLOAD_DIR, *, ticket_id: str | None = None
    ) -> None:
        root = Path(directory)
        if ticket_id is not None:
            # Ticket IDs are an immutable scope, never a caller-controlled path.
            if (
                not ticket_id
                or Path(ticket_id).name != ticket_id
                or ticket_id in {".", ".."}
            ):
                raise PayloadStorageError("invalid ticket payload scope")
            root = root / ticket_id
        self.directory = root

    def put(self, content: bytes, *, max_bytes: int | None = None) -> str:
        if max_bytes is not None and len(content) > max_bytes:
            raise PayloadStorageError("payload exceeds quota")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        digest = hashlib.sha256(content).hexdigest()
        target = self.directory / digest
        try:
            if (
                target.exists()
                and stat.S_ISREG(target.lstat().st_mode)
                and target.read_bytes() == content
            ):
                os.chmod(target, 0o600)
                self._fsync_directory()
                return f"sha256:{digest}"
        except OSError as exc:
            raise PayloadStorageError(
                "could not verify an existing redacted payload"
            ) from exc
        fd, temp_name = tempfile.mkstemp(prefix=".payload-", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Replacing identical content is atomic and makes concurrent writes
            # idempotent.  A rename error is deliberately propagated.
            os.replace(temp_name, target)
            self._fsync_directory()
        except OSError as exc:
            raise PayloadStorageError(
                "could not atomically store redacted payload"
            ) from exc
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        os.chmod(target, 0o600)
        return f"sha256:{digest}"

    def get(self, ref: str, *, max_bytes: int) -> bytes:
        """Read a private content-addressed payload after strict verification."""
        if not ref.startswith("sha256:") or len(ref) != 71:
            raise PayloadStorageError("invalid payload reference")
        target = self.directory / ref.removeprefix("sha256:")
        try:
            if (
                target.parent != self.directory
                or target.is_symlink()
                or not target.is_file()
            ):
                raise PayloadStorageError("unsafe payload reference")
            directory_mode = self.directory.stat().st_mode
            if directory_mode & 0o077 or target.stat().st_mode & 0o077:
                raise PayloadStorageError(
                    "payload exceeds policy or has unsafe permissions"
                )
            content = target.read_bytes()
            if (
                len(content) > max_bytes
                or hashlib.sha256(content).hexdigest() != ref[7:]
            ):
                raise PayloadStorageError("payload integrity check failed")
            return content
        except OSError as exc:
            raise PayloadStorageError("could not read payload") from exc

    def _fsync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class PayloadBuilder:
    """Create one safe, bounded descriptor for every later trace adapter."""

    def __init__(
        self,
        redactor: Redactor,
        *,
        blob_store: PayloadBlobStore | None = None,
        audit_key_path: Path = TRACE_AUDIT_KEY_PATH,
        inline_bytes: int = DEFAULT_INLINE_BYTES,
    ) -> None:
        if inline_bytes < 1:
            raise ValueError("inline_bytes must be positive")
        self.redactor = redactor
        self.blob_store = blob_store or PayloadBlobStore()
        self.audit_key_path = Path(audit_key_path)
        self.inline_bytes = inline_bytes

    def build(
        self, ticket_id: str, payload: Any, *, media_type: str | None = None
    ) -> PayloadDescriptor:
        """Redact before storage, fail closed, and retain only safe correlation."""
        original, structured, resolved_media_type = canonicalize_payload(
            payload, media_type
        )
        try:
            if structured is not None:
                redacted_value = self.redactor.redact(ticket_id, structured)
                if (
                    isinstance(redacted_value, dict)
                    and "redaction_error" in redacted_value
                ):
                    raise PayloadStorageError("redaction failed")
                redacted = _canonical_json(redacted_value)
            elif isinstance(payload, (bytes, bytearray)):
                # Do not persist reversible opaque bytes: a registered secret can
                # cross base64 block boundaries and evade textual redaction.
                redacted = _BINARY_OMITTED.encode("utf-8")
            else:
                redacted = self.redactor.redact_string(
                    ticket_id, original.decode("utf-8")
                ).encode("utf-8")
            if _REDACTION_ERROR.encode() in redacted:
                raise PayloadStorageError("redaction failed")
        except Exception:
            return PayloadDescriptor(
                preview=_REDACTION_ERROR,
                redaction_applied=True,
                truncated=True,
            )

        sensitive = redacted != original or isinstance(payload, (bytes, bytearray))
        try:
            audit_key = load_audit_key(self.audit_key_path) if sensitive else None
            digest, digest_kind = fingerprint(
                original,
                sensitive=sensitive,
                audit_key=audit_key,
            )
            binary_omitted = isinstance(payload, (bytes, bytearray))
            truncated = len(redacted) > self.inline_bytes or binary_omitted
            # Ignore an incomplete trailing code point so the persisted UTF-8
            # preview can never exceed the byte budget.
            preview = redacted[: self.inline_bytes].decode("utf-8", errors="ignore")
            blob_ref = (
                self.blob_store.put(redacted, max_bytes=1_048_576)
                if truncated and not binary_omitted
                else None
            )
        except (OSError, PayloadStorageError):
            # A caller must not mark its action audit-ready from this descriptor.
            raise
        return PayloadDescriptor(
            size_bytes=len(original),
            original_size_bytes=len(original),
            redacted_size_bytes=len(redacted),
            media_type=resolved_media_type,
            digest=digest,
            digest_kind=digest_kind,
            preview=preview,
            blob_ref=blob_ref,
            truncated=truncated,
            redaction_applied=sensitive,
        )
