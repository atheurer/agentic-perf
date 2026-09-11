"""Tests for canonical, bounded, private trace payload descriptors."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from providers.redaction import Redactor
from providers.tracing import PayloadBlobStore, PayloadBuilder, PayloadStorageError
from providers.tracing.fingerprints import load_audit_key


def _builder(tmp_path, *, inline_bytes: int = 32) -> PayloadBuilder:
    return PayloadBuilder(
        Redactor(),
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "trace-audit-key",
        inline_bytes=inline_bytes,
    )


def test_equivalent_json_has_a_canonical_digest(tmp_path) -> None:
    builder = _builder(tmp_path)

    first = builder.build("PERF-1", '{"b": 2, "a": [1, 2]}')
    second = builder.build("PERF-1", {"a": [1, 2], "b": 2})
    changed = builder.build("PERF-1", {"a": [1, 3], "b": 2})

    assert first.digest == second.digest
    assert first.digest != changed.digest
    assert first.digest_kind == "sha256"
    assert not (tmp_path / "secrets" / "trace-audit-key").exists()


def test_sensitive_payload_uses_stable_hmac_and_never_plain_digest(tmp_path) -> None:
    secret = "secret-value-should-not-leak"
    builder = _builder(tmp_path)
    builder.redactor.register("PERF-1", "vault/token", secret)

    one = builder.build("PERF-1", {"token": secret})
    two = builder.build("PERF-1", {"token": secret})
    other = _builder(tmp_path / "other")
    other.redactor.register("PERF-1", "vault/token", secret)
    three = other.build("PERF-1", {"token": secret})

    assert one.digest == two.digest
    assert one.digest != three.digest
    assert one.digest_kind == "hmac-sha256"
    assert secret not in one.model_dump_json()


def test_inline_threshold_unicode_binary_and_malformed_json(tmp_path) -> None:
    builder = _builder(tmp_path, inline_bytes=8)

    unicode_descriptor = builder.build("PERF-1", {"message": "héllo"})
    binary_descriptor = builder.build("PERF-1", b"\xff\x00bytes")
    malformed = builder.build("PERF-1", "{not json")

    assert unicode_descriptor.original_size_bytes == len('{"message":"héllo"}'.encode())
    assert unicode_descriptor.size_bytes == unicode_descriptor.original_size_bytes
    assert unicode_descriptor.truncated is True
    assert unicode_descriptor.blob_ref
    assert len(unicode_descriptor.preview.encode()) <= 8
    assert binary_descriptor.media_type == "application/octet-stream"
    assert binary_descriptor.original_size_bytes == 7
    assert binary_descriptor.preview == "[REDACTE"
    assert binary_descriptor.blob_ref is None
    assert binary_descriptor.digest_kind == "hmac-sha256"
    assert malformed.media_type == "text/plain"
    assert malformed.preview == "{not jso"


def test_large_redacted_output_is_private_and_mode_restricted(tmp_path) -> None:
    builder = _builder(tmp_path, inline_bytes=12)
    descriptor = builder.build("PERF-1", "x" * 500)
    blob = builder.blob_store.directory / descriptor.blob_ref.split(":", 1)[1]

    assert descriptor.truncated is True
    assert len(descriptor.preview.encode()) <= 12
    assert blob.read_bytes() == b"x" * 500
    assert os.stat(builder.blob_store.directory).st_mode & 0o777 == 0o700
    assert os.stat(blob).st_mode & 0o777 == 0o600


def test_concurrent_identical_blob_creation_is_idempotent(tmp_path) -> None:
    store = PayloadBlobStore(tmp_path / "blobs")
    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(store.put, [b"same-safe-content"] * 16))

    assert len(set(refs)) == 1
    assert len(list(store.directory.iterdir())) == 1


def test_failed_atomic_rename_raises(monkeypatch, tmp_path) -> None:
    store = PayloadBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(
        "providers.tracing.payloads.os.replace",
        lambda *_: (_ for _ in ()).throw(OSError("nope")),
    )

    with pytest.raises(PayloadStorageError):
        store.put(b"safe")


def test_failed_directory_sync_raises(monkeypatch, tmp_path) -> None:
    store = PayloadBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(
        store,
        "_fsync_directory",
        lambda: (_ for _ in ()).throw(OSError("directory sync failed")),
    )

    with pytest.raises(PayloadStorageError):
        store.put(b"safe")


def test_existing_blob_directory_sync_failure_is_wrapped(monkeypatch, tmp_path) -> None:
    store = PayloadBlobStore(tmp_path / "blobs")
    store.put(b"safe")
    monkeypatch.setattr(
        store,
        "_fsync_directory",
        lambda: (_ for _ in ()).throw(OSError("directory sync failed")),
    )

    with pytest.raises(PayloadStorageError, match="verify an existing"):
        store.put(b"safe")


def test_corrupt_blob_is_replaced_and_permissions_are_repaired(tmp_path) -> None:
    store = PayloadBlobStore(tmp_path / "blobs")
    ref = store.put(b"safe")
    target = store.directory / ref.split(":", 1)[1]
    target.write_bytes(b"corrupt")
    target.chmod(0o644)

    assert store.put(b"safe") == ref
    assert target.read_bytes() == b"safe"
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_concurrent_first_key_creation_returns_one_stable_key(tmp_path) -> None:
    key_path = tmp_path / "secrets" / "trace-audit-key"
    with ThreadPoolExecutor(max_workers=8) as executor:
        keys = list(executor.map(load_audit_key, [key_path] * 16))

    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
