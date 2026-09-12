"""Tests for CLI submit --description-file flag.

Verifies that -f/--description-file reads from a file or stdin,
is mutually exclusive with -d, and handles missing/empty/unreadable
files correctly. Parser tests invoke the real cli.py to ensure
production parity.

Closes #653.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_CLI_PATH = str(Path(__file__).resolve().parents[1] / "cli.py")


class TestParserMutualExclusion:
    """Parser rejects -d and -f together (via real cli.py)."""

    def test_both_flags_rejected(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                _CLI_PATH,
                "submit",
                "summary",
                "-d",
                "inline",
                "-f",
                "file.txt",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "not allowed with argument" in result.stderr

    def test_d_alone_parsed(self, tmp_path: Path) -> None:
        """Real parser accepts -d and passes it through."""
        result = subprocess.run(
            [
                sys.executable,
                _CLI_PATH,
                "submit",
                "my summary",
                "-d",
                "inline desc",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Will fail connecting to state store, but parser succeeds
        # (returncode != 2 means argparse didn't reject it)
        assert result.returncode != 2

    def test_f_alone_parsed(self, tmp_path: Path) -> None:
        """Real parser accepts -f and passes it through."""
        desc_file = tmp_path / "desc.txt"
        desc_file.write_text("file content")
        result = subprocess.run(
            [
                sys.executable,
                _CLI_PATH,
                "submit",
                "my summary",
                "-f",
                str(desc_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 2

    def test_f_long_form_parsed(self, tmp_path: Path) -> None:
        """Real parser accepts --description-file long form."""
        desc_file = tmp_path / "desc.txt"
        desc_file.write_text("file content")
        result = subprocess.run(
            [
                sys.executable,
                _CLI_PATH,
                "submit",
                "my summary",
                "--description-file",
                str(desc_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 2


class TestResolveDescription:
    """Tests for the _resolve_description helper (production code)."""

    def test_from_file(self, tmp_path: Path) -> None:
        from cli import _resolve_description

        desc_file = tmp_path / "desc.md"
        desc_file.write_text("Multi-line\ndescription\n")
        args = argparse.Namespace(
            description=None,
            description_file=str(desc_file),
            summary="test summary",
        )
        assert _resolve_description(args) == "Multi-line\ndescription"

    def test_from_stdin(self) -> None:
        from cli import _resolve_description

        args = argparse.Namespace(
            description=None,
            description_file="-",
            summary="test summary",
        )
        with patch("sys.stdin", StringIO("stdin content\n")):
            assert _resolve_description(args) == "stdin content"

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        from cli import _resolve_description

        args = argparse.Namespace(
            description=None,
            description_file=str(tmp_path / "nonexistent.txt"),
            summary="test summary",
        )
        with pytest.raises(SystemExit):
            _resolve_description(args)

    def test_empty_file_exits(self, tmp_path: Path) -> None:
        from cli import _resolve_description

        desc_file = tmp_path / "empty.txt"
        desc_file.write_text("   \n\n  ")
        args = argparse.Namespace(
            description=None,
            description_file=str(desc_file),
            summary="test summary",
        )
        with pytest.raises(SystemExit):
            _resolve_description(args)

    def test_unreadable_file_exits(self, tmp_path: Path) -> None:
        from cli import _resolve_description

        args = argparse.Namespace(
            description=None,
            description_file=str(tmp_path / "exists.txt"),
            summary="test summary",
        )
        desc_file = tmp_path / "exists.txt"
        desc_file.write_text("content")
        with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
            with pytest.raises(SystemExit):
                _resolve_description(args)

    def test_inline_description_used(self) -> None:
        from cli import _resolve_description

        args = argparse.Namespace(
            description="inline desc",
            description_file=None,
            summary="test summary",
        )
        assert _resolve_description(args) == "inline desc"

    def test_summary_fallback(self) -> None:
        from cli import _resolve_description

        args = argparse.Namespace(
            description=None,
            description_file=None,
            summary="just the summary",
        )
        assert _resolve_description(args) == "just the summary"

    def test_file_whitespace_stripped(self, tmp_path: Path) -> None:
        from cli import _resolve_description

        desc_file = tmp_path / "padded.txt"
        desc_file.write_text("\n  real content  \n\n")
        args = argparse.Namespace(
            description=None,
            description_file=str(desc_file),
            summary="test summary",
        )
        assert _resolve_description(args) == "real content"
