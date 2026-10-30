"""Tests for read_artifact_file_impl path scoping and containment."""

from __future__ import annotations

import json
from pathlib import Path

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
