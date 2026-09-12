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


def test_archive_uses_only_logical_member_names(filesystem) -> None:
    fs, events, root = filesystem
    fs.write("logs/output.txt", "output")
    fs.archive("results/bundle.tar.gz", ["logs/output.txt"])
    assert (root / "results/bundle.tar.gz").is_file()
    assert events[-1].attributes["members"] == ["workspace://logs/output.txt"]
    assert str(root) not in events[-1].model_dump_json()


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
