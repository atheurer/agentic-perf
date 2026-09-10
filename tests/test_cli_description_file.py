"""Tests for CLI submit --description-file flag.

Verifies that -f/--description-file reads from a file or stdin,
is mutually exclusive with -d, and handles missing/empty/unreadable
files correctly.

Closes #653.
"""

from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


def _get_parser() -> argparse.ArgumentParser:
    """Build the submit argument parser matching cli.py."""
    parser = argparse.ArgumentParser()
    parser.add_argument("summary")
    desc_group = parser.add_mutually_exclusive_group()
    desc_group.add_argument("-d", "--description")
    desc_group.add_argument("-f", "--description-file")
    return parser


class TestParserMutualExclusion:
    """Parser rejects -d and -f together."""

    def test_both_flags_rejected(self) -> None:
        parser = _get_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["summary", "-d", "inline", "-f", "file.txt"])

    def test_d_alone_works(self) -> None:
        parser = _get_parser()
        args = parser.parse_args(["summary", "-d", "inline desc"])
        assert args.description == "inline desc"
        assert args.description_file is None

    def test_f_alone_works(self) -> None:
        parser = _get_parser()
        args = parser.parse_args(["summary", "-f", "file.txt"])
        assert args.description_file == "file.txt"
        assert args.description is None


class TestResolveDescription:
    """Tests for the _resolve_description helper."""

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
