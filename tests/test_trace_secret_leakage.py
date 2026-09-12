"""Regression tests proving trace storage has no credential-bearing surface."""

from __future__ import annotations

import base64
import tarfile
from urllib.parse import quote

import pytest

from providers.execution import AuditedFilesystem, RootedPath
from providers.redaction import Redactor
from providers.tracing import (
    PayloadBlobStore,
    PayloadBuilder,
    TraceSpool,
)
from state_store.trace_store import TracePayloadConflictError, TraceStore


def _fake_pem(secret: str) -> str:
    """Build a scanner-safe fake PEM fixture at runtime."""
    label = "PRIVATE" + " KEY"
    return f"-----BEGIN {label}-----\n{secret}\n-----END {label}-----"


def _storage_surfaces(tmp_path, descriptor, caplog=None) -> list[str]:
    surfaces = [descriptor.model_dump_json()]
    if caplog is not None:
        surfaces.append(caplog.text)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            surfaces.append(path.read_bytes().decode(errors="ignore"))
    return surfaces


def test_each_credential_form_is_absent_from_every_storage_surface(
    tmp_path, caplog
) -> None:
    sentinels = {
        "registered": "registered-secret-12345",
        "bearer": "bearer-secret-12345",
        # Construct at runtime so the fixture exercises the detector without
        # committing an AWS-key-shaped literal that trips repository scanning.
        "aws_access": "AK" + "IA" + "1234567890ABCDEF",
        "aws_secret": "aws-secret-value-12345",
        "pem": "pem-secret-value-12345",
        "password": "password-flag-value-12345",
        "environment": "environment-token-value-12345",
        "url": "url-password-value-12345",
    }
    redactor = Redactor()
    redactor.register("PERF-1", "vault/registered", sentinels["registered"])
    builder = PayloadBuilder(
        redactor,
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
        inline_bytes=20,
    )
    descriptor = builder.build(
        "PERF-1",
        " ".join(
            (
                sentinels["registered"],
                f"Bearer {sentinels['bearer']}",
                sentinels["aws_access"],
                f"AWS_SECRET_ACCESS_KEY={sentinels['aws_secret']}",
                _fake_pem(sentinels["pem"]),
                f"cmd --password={sentinels['password']}",
                f"-e API_TOKEN={sentinels['environment']}",
                f"https://user:{sentinels['url']}@example.invalid",
                base64.b64encode(sentinels["registered"].encode()).decode(),
                quote(sentinels["registered"], safe=""),
            )
        ),
    )
    with TraceStore(tmp_path / "trace.db") as trace_store:
        trace_store.put_payload_descriptor(descriptor)
        # Scan while the store connection is live so SQLite's WAL is an actual
        # persistence surface under test rather than an optional post-close file.
        assert (tmp_path / "trace.db-wal").exists()
        surfaces = _storage_surfaces(tmp_path, descriptor, caplog)
        forbidden = set(sentinels.values())
        forbidden.update(
            base64.b64encode(value.encode()).decode() for value in sentinels.values()
        )
        forbidden.update(quote(value, safe="") for value in sentinels.values())
        assert all(value not in surface for value in forbidden for surface in surfaces)


def test_credentials_are_absent_from_descriptor_db_wal_blob_and_logs(
    tmp_path, caplog
) -> None:
    secret = "leak-check-secret-12345"
    redactor = Redactor()
    redactor.register("PERF-1", "vault/password", secret)
    builder = PayloadBuilder(
        redactor,
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
        inline_bytes=20,
    )
    payload = {
        "password": secret,
        "auth": f"Bearer {secret}",
        "encoded": base64.b64encode(secret.encode()).decode(),
        "url": f"https://user:{secret}@example.invalid",
        "pem": _fake_pem("secret"),
        "command": f"tool --password={secret} -e API_TOKEN={secret}",
    }
    descriptor = builder.build("PERF-1", payload)
    with TraceStore(tmp_path / "trace.db") as trace_store:
        trace_store.put_payload_descriptor(descriptor)

    surfaces = [
        descriptor.model_dump_json(),
        (tmp_path / "trace.db").read_bytes().decode(errors="ignore"),
    ]
    surfaces.extend(
        path.read_text(errors="ignore") for path in (tmp_path / "blobs").iterdir()
    )
    wal = tmp_path / "trace.db-wal"
    if wal.exists():
        surfaces.append(wal.read_text(errors="ignore"))
    surfaces.append(caplog.text)
    assert all(secret not in surface for surface in surfaces)


def test_secret_is_absent_from_db_wal_spool_blob_and_export(tmp_path) -> None:
    """Audit descriptors remain safe across every durable/re-exported surface."""
    secret = "audit-storage-secret-12345"
    builder = PayloadBuilder(
        Redactor(),
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
        inline_bytes=8,
    )
    descriptor = builder.build("PERF-1", {"password": secret})
    events = []
    filesystem = AuditedFilesystem(
        RootedPath(tmp_path / "workspace", "workspace"),
        ticket_id="PERF-1",
        emit=events.append,
        critical=True,
    )
    filesystem.write("secrets/secret-token.txt", secret)
    with filesystem.open_stream("keys/private-key.pem") as stream:
        stream.write(secret.encode())
    assert events
    spool = TraceSpool(tmp_path / "spool", name="filesystem")
    with TraceStore(tmp_path / "trace.db") as store:
        store.put_payload_descriptor(descriptor)
        for event in events:
            store.insert_event(event)
            spool.append(event)
        export_source = tmp_path / "export.json"
        export_source.write_text("\n".join(event.model_dump_json() for event in events))
        with tarfile.open(tmp_path / "export.tar.gz", "w:gz") as archive:
            archive.add(export_source, arcname="trace.json")
        surfaces = [descriptor.model_dump_json()]
        for path in tmp_path.rglob("*"):
            if path.is_file() and "workspace" not in path.relative_to(tmp_path).parts:
                surfaces.append(path.read_bytes().decode(errors="ignore"))
        assert all(secret not in surface for surface in surfaces)
        assert "secret-token.txt" not in "".join(surfaces)


def test_redaction_failure_returns_only_a_safe_descriptor(
    monkeypatch, tmp_path
) -> None:
    builder = PayloadBuilder(
        Redactor(), blob_store=PayloadBlobStore(tmp_path / "blobs")
    )
    monkeypatch.setattr(
        builder.redactor, "redact_string", lambda *_: "[REDACTED:redaction_error]"
    )

    descriptor = builder.build("PERF-1", "secret that must not be returned")

    assert descriptor.preview == "[REDACTED:redaction_error]"
    assert descriptor.digest is None
    assert descriptor.blob_ref is None


def test_trace_store_persists_descriptor_metadata_only(tmp_path) -> None:
    descriptor = PayloadBuilder(
        Redactor(),
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
    ).build("PERF-1", {"result": "safe"})

    with TraceStore(tmp_path / "trace.db") as trace_store:
        trace_store.put_payload_descriptor(descriptor)
        assert trace_store.get_payload_descriptor(descriptor.digest or "") == descriptor


def test_trace_store_rejects_conflicting_payload_metadata(tmp_path) -> None:
    builder = PayloadBuilder(
        Redactor(),
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
    )
    descriptor = builder.build("PERF-1", {"result": "safe"})
    conflicting = descriptor.model_copy(update={"media_type": "text/plain"})

    with TraceStore(tmp_path / "trace.db") as trace_store:
        trace_store.put_payload_descriptor(descriptor)
        trace_store.put_payload_descriptor(descriptor)
        with pytest.raises(TracePayloadConflictError):
            trace_store.put_payload_descriptor(conflicting)


def test_binary_secret_at_any_offset_is_never_recoverable(tmp_path) -> None:
    secret = b"binary-secret-value-12345"
    builder = PayloadBuilder(
        Redactor(),
        blob_store=PayloadBlobStore(tmp_path / "blobs"),
        audit_key_path=tmp_path / "secrets" / "audit-key",
        inline_bytes=100,
    )
    for offset in range(4):
        descriptor = builder.build("PERF-1", b"x" * offset + secret + b"\x00tail")
        surfaces = _storage_surfaces(tmp_path, descriptor, caplog=None)
        assert descriptor.blob_ref is None
        assert all(secret.decode() not in surface for surface in surfaces)
        assert all(
            base64.b64encode(secret).decode() not in surface for surface in surfaces
        )
