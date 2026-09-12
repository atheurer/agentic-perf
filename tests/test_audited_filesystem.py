"""Regression coverage for ticket-rooted filesystem audit mutations."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from providers.execution import AuditedFilesystem, FilesystemAuditError, RootedPath
from state_store.models import CreateTicketRequest
from state_store.store import TicketStore


@pytest.fixture
def filesystem(tmp_path: Path):
    events = []
    fs = AuditedFilesystem(
        RootedPath(tmp_path, "workspace"), ticket_id="PERF-FILES", emit=events.append
    )
    return fs, events, tmp_path


def test_create_is_audited_with_logical_digest_and_size(filesystem) -> None:
    fs, events, root = filesystem
    fs.write("results/result.json", b"hello")
    assert (root / "results/result.json").read_bytes() == b"hello"
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "completed",
    ]
    complete = events[-1]
    assert complete.attributes["size_bytes"] == 5
    assert complete.attributes["digest"] == hashlib.sha256(b"hello").hexdigest()
    assert complete.action.target == "workspace://results/result.json"
    assert str(root) not in complete.model_dump_json()


def test_lifecycle_uses_one_child_action_and_never_records_error_text(
    filesystem,
) -> None:
    fs, events, root = filesystem
    secret = "secret-token-not-for-audit"
    with pytest.raises(FileNotFoundError):
        fs.rename("missing", secret)
    assert [event.lifecycle.state.value for event in events] == ["requested", "failed"]
    assert events[0].action_id == events[1].action_id
    assert events[0].trace_id == events[1].trace_id
    serialized = events[-1].model_dump_json()
    assert secret not in serialized
    assert str(root) not in serialized
    assert events[-1].error is not None
    assert events[-1].error.message is None
    assert len(events[-1].attributes["error_digest"]) == 64


def test_atomic_replace_rename_and_unlink(filesystem) -> None:
    fs, events, root = filesystem
    fs.write("one", "old")
    fs.write("one", "new")
    fs.rename("one", "two")
    fs.unlink("two")
    assert not (root / "two").exists()
    assert [event.attributes["operation"] for event in events[::2]] == [
        "create",
        "replace",
        "rename",
        "unlink",
    ]
    assert events[3].attributes["atomic"] is True


def test_missing_target_and_audit_delivery_failure_are_not_success(filesystem) -> None:
    fs, events, _ = filesystem
    with pytest.raises(FileNotFoundError):
        fs.unlink("missing")
    assert events[-1].lifecycle.state.value == "failed"

    blocked = AuditedFilesystem(
        RootedPath(Path.cwd(), "workspace"),
        ticket_id="PERF-FILES",
        emit=lambda _: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    with pytest.raises(FilesystemAuditError):
        blocked.write("never-created", "x")
    assert not (Path.cwd() / "never-created").exists()


def test_critical_mutation_requires_recorder_before_touching_disk(
    tmp_path: Path,
) -> None:
    with pytest.raises(FilesystemAuditError):
        AuditedFilesystem(
            RootedPath(tmp_path, "ticket"), ticket_id="PERF-FILES", critical=True
        )
    assert not (tmp_path / "never-created").exists()


def test_short_write_is_failed_and_never_completed(
    filesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, events, _ = filesystem

    class ShortWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def write(self, value):
            self._handle.write(value[:1])
            return 1

    import providers.execution.filesystem as module

    original = module.os.fdopen
    monkeypatch.setattr(module.os, "fdopen", lambda *args: ShortWriter(original(*args)))
    with pytest.raises(OSError, match="short filesystem write"):
        fs.write("partial", b"abcdef")
    assert [event.lifecycle.state.value for event in events] == ["requested", "failed"]


def test_concurrent_same_target_is_atomic(filesystem) -> None:
    import concurrent.futures

    fs, _, root = filesystem
    values = [f"value-{number}".encode() for number in range(12)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: fs.write("same", value), values))
    assert (root / "same").read_bytes() in values


def test_stream_is_requested_before_open_and_finalized_on_close(filesystem) -> None:
    fs, events, root = filesystem
    stream = fs.open_stream("logs/serial.log")
    stream.write(b"serial output")
    stream.close()
    assert (root / "logs/serial.log").read_bytes() == b"serial output"
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "completed",
    ]
    assert events[0].action_id == events[1].action_id
    assert (
        events[-1].attributes["digest"] == hashlib.sha256(b"serial output").hexdigest()
    )


def test_archive_uses_only_logical_member_names(filesystem) -> None:
    fs, events, root = filesystem
    fs.write("logs/output.txt", "output")
    fs.archive("results/bundle.tar.gz", ["logs/output.txt"])
    assert (root / "results/bundle.tar.gz").is_file()
    assert events[-1].attributes["members"] == ["workspace://logs/output.txt"]
    assert str(root) not in events[-1].model_dump_json()


def test_permission_failure_has_one_failed_terminal(filesystem, monkeypatch) -> None:
    fs, events, _ = filesystem
    import providers.execution.filesystem as module

    monkeypatch.setattr(
        module.os,
        "fdopen",
        lambda *_: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    with pytest.raises(PermissionError):
        fs.write("denied", b"x")
    assert [event.lifecycle.state.value for event in events] == ["requested", "failed"]
    assert events[-1].outcome.value == "failure"
    assert events[-1].error.message is None


def test_temporary_cleanup_failure_has_a_failed_cleanup_lifecycle(
    filesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, events, _ = filesystem
    import providers.execution.filesystem as module

    original_replace = module.os.replace
    monkeypatch.setattr(
        module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace"))
    )
    monkeypatch.setattr(
        module.os, "unlink", lambda *_: (_ for _ in ()).throw(OSError("cleanup"))
    )
    with pytest.raises(OSError):
        fs.write("cleanup", b"data")
    assert [event.attributes["operation"] for event in events] == [
        "create",
        "cleanup",
        "create",
    ]
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "failed",
        "failed",
    ]
    monkeypatch.setattr(module.os, "replace", original_replace)


def test_ticket_archive_records_each_move_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    log_dir = tmp_path.parent / "logs"
    monkeypatch.setattr(paths, "LOG_DIR", log_dir)
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="archive", description="x")
    )
    store.force_close(ticket.id)
    log_dir.mkdir()
    (log_dir / f"{ticket.id}.jsonl").write_text("event\n")

    result = store.archive_ticket(ticket.id)
    assert result["archived_files"] == [
        f"ticket://archive/tickets/{ticket.id}.json",
        f"ticket://archive/logs/{ticket.id}.jsonl",
    ]
    assert not TicketStore(persist_dir=tmp_path).list_tickets()
