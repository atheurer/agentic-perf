"""Answer Git HTTPS prompts from a secret file supplied by the parent."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

_MAX_SECRET_BYTES = 64 * 1024


def main() -> int:
    prompt = sys.argv[1].casefold() if len(sys.argv) > 1 else ""
    if "username" in prompt:
        value = os.environ.get("AGENTIC_PERF_GIT_USERNAME", "")
    elif "password" in prompt or "token" in prompt:
        secret_path = os.environ.get("AGENTIC_PERF_GIT_TOKEN_FILE", "")
        try:
            path = Path(secret_path)
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size == 0
                or metadata.st_size > _MAX_SECRET_BYTES
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                return 1
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            return 1
    else:
        return 1
    if not value or "\n" in value or "\r" in value:
        return 1
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
