"""The normal hook must isolate Git subprocesses launched by its tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_normal_hook_does_not_leak_worktree_git_identity_to_test_repositories(tmp_path):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"

    def git(*args, cwd=repository):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    repository.mkdir()
    git("init", "-b", "main")
    git("config", "user.name", "Outer Author")
    git("config", "user.email", "outer@example.invalid")
    git("config", "core.bare", "false")
    (repository / ".githooks").mkdir()
    hook = Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"
    (repository / ".githooks" / "pre-commit").write_text(hook.read_text())
    (repository / "example.py").write_text("value = 1\n")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_example.py").write_text(
        "def test_example():\n    pass\n"
    )
    git("add", ".")
    git("-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    git("worktree", "add", "-b", "review", str(worktree))
    (worktree / "example.py").write_text("value = 2\n")
    git("add", "example.py", cwd=worktree)
    git_dir = git("rev-parse", "--absolute-git-dir", cwd=worktree)
    index = Path(git_dir) / "index"
    original_head = git("rev-parse", "HEAD", cwd=worktree)
    original_index = index.read_bytes()
    original_config = {
        name: git("config", "--local", "--get", name, cwd=worktree)
        for name in ("core.bare", "user.name", "user.email")
    }
    stubs = tmp_path / "bin"
    stubs.mkdir()
    runner = stubs / "python3"
    runner.write_text("""#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == "-m" && "$2" == "pytest" ]]
git init --bare "$HOOK_NESTED/cache.git" >/dev/null
git init -b main "$HOOK_NESTED/project" >/dev/null
git -C "$HOOK_NESTED/project" config user.name "Nested Author"
git -C "$HOOK_NESTED/project" config user.email "nested@example.invalid"
printf 'nested\n' > "$HOOK_NESTED/project/nested.txt"
git -C "$HOOK_NESTED/project" add nested.txt
git -C "$HOOK_NESTED/project" -c core.hooksPath=/dev/null commit -m nested >/dev/null
printf 'runner completed\n'
""")
    runner.chmod(0o755)
    lint = stubs / "ruff"
    lint.write_text("#!/usr/bin/env bash\nexit 0\n")
    lint.chmod(0o755)
    nested = tmp_path / "nested"
    nested.mkdir()
    contaminated = environment | {
        "PATH": str(stubs) + os.pathsep + environment["PATH"],
        "GIT_DIR": git_dir,
        "GIT_COMMON_DIR": str(repository / ".git"),
        "GIT_WORK_TREE": str(worktree),
        "GIT_INDEX_FILE": str(index),
        "HOOK_NESTED": str(nested),
    }
    result = subprocess.run(
        ["bash", str(worktree / ".githooks" / "pre-commit")],
        cwd=worktree,
        env=contaminated,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Even a failed nested Git command must not have damaged the outer repository.
    assert git("rev-parse", "HEAD", cwd=worktree) == original_head
    assert index.read_bytes() == original_index
    assert {
        name: git("config", "--local", "--get", name, cwd=worktree)
        for name in original_config
    } == original_config
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runner completed" in result.stdout
    assert git("log", "-1", "--format=%an", cwd=nested / "project") == "Nested Author"
