#!/usr/bin/env python3
"""Scrub known sensitive patterns from JSONL event logs.

This is a pattern-only scrubber — it replaces text that looks like
secrets (bearer tokens, AWS keys, passwords on command lines, PEM
blocks, pexpect sendline payloads) with [SCRUBBED].  It does NOT
know actual secret values; that requires the full redaction
infrastructure (see #456 / PR 4a).

Usage:
    # Preview what would be scrubbed (default):
    python3 scripts/scrub-event-logs.py

    # Scrub a specific directory:
    python3 scripts/scrub-event-logs.py --log-dir /path/to/logs

    # Apply changes (atomic rename per file):
    python3 scripts/scrub-event-logs.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

SCRUB_MARKER = "[SCRUBBED]"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "bearer_token",
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    ),
    (
        "aws_access_key",
        re.compile(r"(AKIA)[A-Z0-9]{16}"),
    ),
    (
        "aws_secret_key",
        re.compile(
            r"(?<=AWS_SECRET_ACCESS_KEY[=:])\s*[A-Za-z0-9/+=]{20,}",
        ),
    ),
    (
        "password_cli_flag",
        re.compile(r"--password[= ]\S+"),
    ),
    (
        "env_kubeadmin_password",
        re.compile(r"-e\s+KUBEADMIN_PASSWORD[= ]\S+"),
    ),
    (
        "pem_block",
        re.compile(
            r"-----BEGIN [A-Z ]+-----[^-]+-----END [A-Z ]+-----",
            re.DOTALL,
        ),
    ),
    (
        "pexpect_sendline",
        re.compile(r"(child\.sendline\()(['\"])[^'\"]+\2(\))"),
    ),
]


def _apply_pattern(
    name: str,
    pattern: re.Pattern[str],
    line: str,
) -> tuple[str, int]:
    """Apply one pattern to a line, return (new_line, match_count)."""
    if name == "bearer_token":
        new, n = pattern.subn(rf"\1{SCRUB_MARKER}", line)
    elif name == "env_kubeadmin_password":
        new, n = pattern.subn(
            f"-e KUBEADMIN_PASSWORD={SCRUB_MARKER}",
            line,
        )
    elif name == "pexpect_sendline":
        new, n = pattern.subn(rf"\1'{SCRUB_MARKER}'\3", line)
    else:
        new, n = pattern.subn(SCRUB_MARKER, line)
    return new, n


def scrub_line(line: str) -> tuple[str, dict[str, int]]:
    """Scrub all patterns from a single line.

    Returns (scrubbed_line, {pattern_name: match_count}).
    """
    hits: dict[str, int] = {}
    for name, pattern in PATTERNS:
        line, count = _apply_pattern(name, pattern, line)
        if count:
            hits[name] = hits.get(name, 0) + count
    return line, hits


def scrub_file(
    path: Path,
    apply: bool = False,
) -> tuple[int, dict[str, int]]:
    """Scrub a single JSONL file.

    Returns (lines_changed, {pattern_name: total_matches}).
    """
    lines_changed = 0
    totals: dict[str, int] = {}
    scrubbed_lines: list[str] = []

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            new_line, hits = scrub_line(line)
            if hits:
                lines_changed += 1
                for name, count in hits.items():
                    totals[name] = totals.get(name, 0) + count
            scrubbed_lines.append(new_line)

    if apply and lines_changed > 0:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(scrubbed_lines)
            os.replace(tmp, str(path))
        except BaseException:
            os.unlink(tmp)
            raise

    return lines_changed, totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrub sensitive patterns from JSONL event logs.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path.home() / ".agentic-perf" / "logs",
        help="Directory containing JSONL event logs",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write scrubbed files (default is dry-run)",
    )
    args = parser.parse_args(argv)

    log_dir: Path = args.log_dir
    if not log_dir.is_dir():
        print(f"Log directory not found: {log_dir}", file=sys.stderr)
        return 1

    jsonl_files = sorted(log_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"No .jsonl files in {log_dir}")
        return 0

    mode = "APPLYING" if args.apply else "DRY RUN"
    print(f"[{mode}] Scanning {len(jsonl_files)} file(s) in {log_dir}\n")

    files_modified = 0
    grand_totals: dict[str, int] = {}

    for path in jsonl_files:
        lines_changed, totals = scrub_file(path, apply=args.apply)
        if lines_changed:
            files_modified += 1
            action = "scrubbed" if args.apply else "would scrub"
            print(f"  {path.name}: {action} {lines_changed} line(s)")
            for name, count in sorted(totals.items()):
                print(f"    {name}: {count}")
                grand_totals[name] = grand_totals.get(name, 0) + count

    print(f"\nFiles scanned:  {len(jsonl_files)}")
    print(f"Files modified: {files_modified}")
    if grand_totals:
        print("Patterns matched:")
        for name, count in sorted(grand_totals.items()):
            print(f"  {name}: {count}")
    else:
        print("No sensitive patterns found.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
