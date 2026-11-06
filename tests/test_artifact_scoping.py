"""Tests for read_artifact_file_impl path scoping and containment."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def artifact_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set ARTIFACT_DIR to a temporary directory."""
    import paths

    monkeypatch.setattr(paths, "ARTIFACT_DIR", tmp_path)
    return tmp_path


def test_read_artifact_file_rejects_cross_ticket_absolute_path(
    artifact_dir: Path,
) -> None:
    """Absolute path into another ticket's directory must be rejected."""
    from agents.server_utils import read_artifact_file_impl

    other_dir = artifact_dir / "PERF-B"
    other_dir.mkdir()
    secret = other_dir / "result.txt"
    secret.write_text("secret data")

    result = json.loads(read_artifact_file_impl(str(secret), ticket_id="PERF-A"))
    assert "error" in result
    assert "File not found" in result["error"]


def test_read_artifact_file_rejects_out_of_scope_path(
    artifact_dir: Path,
) -> None:
    """Absolute path outside ARTIFACT_DIR must be rejected."""
    from agents.server_utils import read_artifact_file_impl

    result = json.loads(read_artifact_file_impl("/etc/passwd", ticket_id="PERF-A"))
    assert "error" in result
    assert "File not found" in result["error"]


def test_out_of_scope_existing_and_missing_return_same_error(
    artifact_dir: Path,
) -> None:
    """An existing out-of-scope file and a missing one must return the
    same error message so callers cannot distinguish them (no info leak)."""
    from agents.server_utils import read_artifact_file_impl

    # Create a file outside the scoped ticket dir but probe with same name
    other_dir = artifact_dir / "PERF-B"
    other_dir.mkdir()
    existing = other_dir / "data.txt"
    existing.write_text("sensitive")

    # Both paths are out of scope for PERF-A; one exists, one doesn't.
    # They must both return "File not found" (same error key/format).
    existing_result = json.loads(
        read_artifact_file_impl(str(existing), ticket_id="PERF-A")
    )
    missing_result = json.loads(
        read_artifact_file_impl(str(other_dir / "nonexistent.txt"), ticket_id="PERF-A")
    )
    assert "error" in existing_result
    assert "File not found" in existing_result["error"]
    assert "error" in missing_result
    assert "File not found" in missing_result["error"]


def test_read_artifact_file_within_scope_succeeds(
    artifact_dir: Path,
) -> None:
    """A file inside the ticket's artifact directory should be readable."""
    from agents.server_utils import read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    target = ticket_dir / "output.log"
    target.write_text("line 1\nline 2\nline 3\n")

    result = json.loads(read_artifact_file_impl("output.log", ticket_id="PERF-A"))
    assert "error" not in result
    assert "line 1" in result["content"]


@pytest.mark.parametrize("absolute", [True, False])
@pytest.mark.parametrize("ticket_kwargs", [{}, {"ticket_id": ""}])
def test_read_artifact_file_requires_ticket_context(
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
    ticket_kwargs: dict[str, str],
) -> None:
    from agents.server_utils import read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-B"
    ticket_dir.mkdir()
    target = ticket_dir / "result.txt"
    target.write_text("private ticket data")
    monkeypatch.chdir(artifact_dir)
    path = str(target if absolute else target.relative_to(artifact_dir))

    existing_result = json.loads(read_artifact_file_impl(path, **ticket_kwargs))
    target.unlink()
    missing_result = json.loads(read_artifact_file_impl(path, **ticket_kwargs))

    assert existing_result == missing_result == {"error": f"File not found: {path}"}


@pytest.mark.parametrize("agent_type", ["benchmark", "platform"])
async def test_read_artifact_tool_initializes_ticket_once(
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_type: str,
) -> None:
    server = import_module(f"agents.{agent_type}.server")
    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    (ticket_dir / "output.log").write_text("first\nsecond\nthird\n")
    monkeypatch.setattr(server, "_ticket", None)

    async def initialize() -> None:
        monkeypatch.setattr(server, "_ticket", {"id": "PERF-A"})

    ensure_init = AsyncMock(side_effect=initialize)
    monkeypatch.setattr(server, "_ensure_init", ensure_init)

    result = json.loads(
        await server.read_artifact_file("output.log", offset=1, limit=1)
    )

    ensure_init.assert_awaited_once()
    assert result["content"] == "second\n"
    assert result["has_more"] is True


def test_read_artifact_file_truncates_oversized_line(artifact_dir: Path) -> None:
    """A single oversized line is byte-bounded and reported as truncated."""
    from agents.server_utils import _ARTIFACT_READ_MAX_BYTES, read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    (ticket_dir / "huge.log").write_text("A" * (_ARTIFACT_READ_MAX_BYTES * 2))

    result = json.loads(read_artifact_file_impl("huge.log", ticket_id="PERF-A"))

    assert result["truncated"] is True
    assert result["has_more"] is True
    assert len(result["content"].encode("utf-8")) <= _ARTIFACT_READ_MAX_BYTES


def test_read_artifact_file_normal_read_not_truncated(artifact_dir: Path) -> None:
    from agents.server_utils import read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    (ticket_dir / "small.log").write_text("a\nb\n")

    result = json.loads(read_artifact_file_impl("small.log", ticket_id="PERF-A"))

    assert result["truncated"] is False
    assert result["has_more"] is False
    assert result["content"] == "a\nb\n"


def test_read_artifact_file_clamps_negative_offset_and_limit(
    artifact_dir: Path,
) -> None:
    from agents.server_utils import read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    (ticket_dir / "data.log").write_text("one\ntwo\n")

    result = json.loads(
        read_artifact_file_impl("data.log", offset=-3, limit=-5, ticket_id="PERF-A")
    )

    assert result["offset"] == 0
    assert result["limit"] == 0
    assert result["lines_returned"] == 0


def test_read_artifact_file_offset_skips_oversized_first_line(
    artifact_dir: Path,
) -> None:
    from agents.server_utils import _ARTIFACT_READ_MAX_BYTES, read_artifact_file_impl

    ticket_dir = artifact_dir / "PERF-A"
    ticket_dir.mkdir()
    (ticket_dir / "long.log").write_text("A" * (_ARTIFACT_READ_MAX_BYTES * 3) + "\nb\n")

    result = json.loads(
        read_artifact_file_impl("long.log", offset=1, ticket_id="PERF-A")
    )

    assert result["truncated"] is False
    assert result["content"] == "b\n"
