"""Fetch individual secrets from administrator-configured Git repositories."""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from providers.execution import AuditedFilesystem, AuditedSubprocessRunner
from providers.secrets.base import SecretsBackendError, SecretsProvider
from providers.skills.git_source import (
    GitSourceError,
    _git_argv,
    _git_environment,
    parse_git_source,
)

logger = logging.getLogger(__name__)

_SECRET_REF_SCHEMES = {"git-secret+http", "git-secret+https", "git-secret+ssh"}
_SECRET_PATH_PART = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_COMMIT = re.compile(r"^[a-f0-9]{40,64}$")
_MAX_SECRET_BYTES = 8 * 1024 * 1024
_GIT_TIMEOUT = 180


def _secret_ref_parts(reference: str) -> tuple[object, str] | None:
    """Parse a self-contained Git URI: transport, repo, branch, and file path."""
    candidate_scheme, separator, remainder = reference.partition(":")
    if not separator or not candidate_scheme.lower().startswith("git-secret+"):
        return None
    if not remainder.startswith("//"):
        raise SecretsBackendError("Invalid Git secret reference")
    try:
        parsed = urlsplit(reference)
    except ValueError:
        raise SecretsBackendError("Invalid Git secret reference") from None
    if parsed.scheme not in _SECRET_REF_SCHEMES:
        raise SecretsBackendError("Invalid Git secret reference")
    try:
        parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise SecretsBackendError("Invalid Git secret reference") from None
    if (
        not parsed.hostname
        or parsed.fragment
        or "\\" in parsed.path
        or set(query) - {"ref", "path", "auth_kind", "auth_ref", "username"}
        or any(len(values) != 1 for values in query.values())
    ):
        raise SecretsBackendError("Invalid Git secret reference")
    relative_path = query.get("path", [""])[0]
    if not _valid_secret_path(relative_path):
        raise SecretsBackendError("Invalid Git secret reference")
    auth_kind = query.get("auth_kind", ["default"])[0]
    if auth_kind == "default" and {"auth_ref", "username"} & query.keys():
        raise SecretsBackendError("Invalid Git secret reference")

    transport = parsed.scheme.removeprefix("git-secret+")
    source: dict[str, object] = {
        "kind": "git",
        "url": f"{transport}://{parsed.netloc}{parsed.path}",
        "ref": query.get("ref", ["main"])[0],
    }
    if auth_kind != "default":
        auth: dict[str, str] = {"kind": auth_kind}
        if "auth_ref" in query:
            auth["secret_ref"] = query["auth_ref"][0]
        if "username" in query:
            auth["username"] = query["username"][0]
        source["auth"] = auth
    try:
        return parse_git_source(source), relative_path
    except (GitSourceError, TypeError, ValueError):
        raise SecretsBackendError("Invalid Git secret reference") from None


class GitSecretsProvider(SecretsProvider):
    """Resolve direct Git URL references through short-lived Git fetches.

    Each reference contains the repository, branch, and repository path, so
    deployments need no separate Git source-location configuration. Secret
    values are exposed only through the short-lived ``secret_file`` context
    manager.
    """

    def __init__(
        self,
        fallback: SecretsProvider,
        *,
        authentication_provider: SecretsProvider | None = None,
    ) -> None:
        self._fallback = fallback
        self._authentication_provider = authentication_provider or fallback

    def _resolve(self, reference: str) -> tuple[object, str] | None:
        return _secret_ref_parts(reference)

    async def get_secret(self, path: str) -> str | None:
        resolved = self._resolve(path)
        if resolved is None:
            return await self._fallback.get_secret(path)
        async with self.secret_file(path) as local_path:
            if local_path is None:
                return None
            try:
                return local_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None

    async def get_secret_file(self, path: str) -> Path | None:
        if self._resolve(path) is not None:
            # A Git-backed file exists only for the duration of secret_file().
            return None
        return await self._fallback.get_secret_file(path)

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        resolved = self._resolve(path)
        if resolved is None:
            async with self._fallback.secret_file(path) as fallback_path:
                yield fallback_path
            return

        source, relative_path = resolved
        temp_root = Path(tempfile.gettempdir()).resolve()
        filesystem = AuditedFilesystem.system(temp_root, scheme="git-secret")
        temp_dir = filesystem.temporary_directory(prefix="agentic-perf-secret-")
        relative_dir = temp_dir.relative_to(temp_root)
        try:
            repo = temp_dir / "source.git"
            runner = AuditedSubprocessRunner(output_limit=_MAX_SECRET_BYTES + 1)
            try:
                async with _git_environment(
                    source, self._authentication_provider
                ) as env:
                    await self._run_git(
                        runner,
                        _git_argv("init", "--bare", "--quiet", str(repo)),
                        env,
                    )
                    await self._run_git(
                        runner,
                        _git_argv(
                            "--git-dir",
                            str(repo),
                            "fetch",
                            "--quiet",
                            "--depth=1",
                            "--no-tags",
                            source.url,
                            f"+refs/heads/{source.ref}:refs/agentic-perf/secret-source",
                        ),
                        env,
                    )
                env = _offline_git_environment()
                commit = await self._run_git(
                    runner,
                    _git_argv(
                        "--git-dir",
                        str(repo),
                        "rev-parse",
                        "--verify",
                        "refs/agentic-perf/secret-source^{commit}",
                    ),
                    env,
                )
                if not _COMMIT.fullmatch(commit):
                    raise SecretsBackendError(
                        "Configured Git secret source returned an invalid revision"
                    )
                tree_entry = await self._run_git_bytes(
                    runner,
                    _git_argv(
                        "--git-dir",
                        str(repo),
                        "ls-tree",
                        "-z",
                        "--full-tree",
                        commit,
                        "--",
                        relative_path,
                    ),
                    env,
                    output_limit=16 * 1024,
                )
                if not tree_entry:
                    yield None
                    return
                entries = tree_entry.split(b"\0")
                if len(entries) != 2 or not entries[0]:
                    raise SecretsBackendError("Invalid Git secret file entry")
                metadata, entry_path = entries[0].split(b"\t", 1)
                mode, object_type, _object_id = metadata.decode("ascii").split(" ")
                if (
                    mode != "100644"
                    or object_type != "blob"
                    or entry_path.decode("utf-8") != relative_path
                ):
                    raise SecretsBackendError("Git secret must be a regular file")
                content = await self._run_git_bytes(
                    runner,
                    _git_argv(
                        "--git-dir",
                        str(repo),
                        "cat-file",
                        "blob",
                        f"{commit}:{relative_path}",
                    ),
                    env,
                    output_limit=_MAX_SECRET_BYTES + 1,
                )
                if len(content) > _MAX_SECRET_BYTES:
                    raise SecretsBackendError("Git secret exceeds the supported size")
                secret_file = temp_dir / "secret"
                filesystem.write(
                    secret_file.relative_to(temp_root), content, mode=0o600
                )
                yield secret_file
            except (GitSourceError, OSError, ValueError, UnicodeError):
                raise SecretsBackendError(
                    "Unable to retrieve configured Git secret"
                ) from None
        finally:
            try:
                _remove_tree(relative_dir, temp_root)
            except OSError:
                logger.error("Unable to clean up temporary Git secret workspace")
                raise SecretsBackendError(
                    "Unable to clean up temporary Git secret workspace"
                ) from None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        scheme, separator, _ = prefix.partition(":")
        if separator and scheme.lower().startswith("git-secret+"):
            async with self.secret_file(prefix) as secret_path:
                return [prefix] if secret_path is not None else []
        return await self._fallback.list_secrets(prefix)

    async def _run_git(self, runner, argv: list[str], env: dict[str, str]) -> str:
        result = await runner.run(
            argv,
            env=env,
            timeout=_GIT_TIMEOUT,
            system_context=True,
        )
        if result.returncode != 0 or result.timed_out:
            raise GitSourceError(
                "secret_source_unavailable",
                "Configured Git secret source is unavailable",
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    async def _run_git_bytes(
        self,
        runner,
        argv: list[str],
        env: dict[str, str],
        *,
        output_limit: int,
    ) -> bytes:
        result = await runner.run(
            argv,
            env=env,
            timeout=_GIT_TIMEOUT,
            system_context=True,
        )
        if result.returncode != 0 or result.timed_out:
            raise GitSourceError(
                "secret_source_unavailable",
                "Configured Git secret source is unavailable",
            )
        if len(result.stdout) > output_limit:
            raise SecretsBackendError("Git secret response exceeds the supported size")
        return result.stdout


def _valid_secret_path(path: str) -> bool:
    parts = path.split("/")
    return bool(parts) and all(
        part not in {"", ".", ".."} and _SECRET_PATH_PART.fullmatch(part)
        for part in parts
    )


def _offline_git_environment() -> dict[str, str]:
    return {
        **{
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if key in os.environ
        },
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _remove_tree(relative: Path, root: Path) -> None:
    filesystem = AuditedFilesystem.system(root, scheme="git-secret")
    path = root / relative
    if not path.exists() and not path.is_symlink():
        return
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        mode = stat.S_IMODE(current_path.stat().st_mode)
        filesystem.chmod(current_path.relative_to(root), mode | 0o700)
        for name in files:
            child = current_path / name
            filesystem.unlink(child.relative_to(root), missing_ok=True)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                filesystem.unlink(child.relative_to(root), missing_ok=True)
            else:
                filesystem.rmdir(child.relative_to(root))
    filesystem.rmdir(relative)
