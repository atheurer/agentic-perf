"""Resolve one secret file from an anonymous HTTPS Git source."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit

from providers.execution import AuditedFilesystem, AuditedSubprocessRunner

from .base import SecretsBackendError, SecretsProvider

_MAX_REFERENCE_LENGTH = 2048
_MAX_SECRET_BYTES = 64 * 1024
_MAX_GIT_TIMEOUT = 90
_REF_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class GitSecretReferenceError(SecretsBackendError):
    """A configured HTTPS Git secret pointer is invalid or unavailable."""


@dataclass(frozen=True)
class GitSecretReference:
    """Validated identity and requested file for one Git secret pointer."""

    transport_url: str
    repository: str
    ref: str
    path: str


def parse_git_secret_reference(value: object) -> GitSecretReference | None:
    """Parse a ``git-secret+https://`` pointer without accessing the network."""
    if not isinstance(value, str) or not value.lower().startswith("git-secret+"):
        return None
    if len(value) > _MAX_REFERENCE_LENGTH:
        raise GitSecretReferenceError("Git secret reference is too long")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GitSecretReferenceError("Invalid Git secret reference URI") from None

    if parsed.scheme != "git-secret+https":
        raise GitSecretReferenceError(
            "Unsupported Git secret reference scheme; use "
            "git-secret+https://. SSH fetching is disabled."
        )
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.strip("/")
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127
            for char in parsed.netloc
        )
        or (port is not None and not 1 <= port <= 65535)
        or _BAD_PERCENT_ESCAPE.search(parsed.path)
    ):
        raise GitSecretReferenceError("Invalid Git secret repository URI")

    repo_parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if any(
        part in {"", ".", ".."}
        or "/" in part
        or "\\" in part
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in repo_parts
    ):
        raise GitSecretReferenceError("Invalid Git secret repository URI")

    query: dict[str, str] = {}
    if not parsed.query or _BAD_PERCENT_ESCAPE.search(parsed.query):
        raise GitSecretReferenceError(
            "Git secret reference requires exactly one ref and one path"
        )
    for item in parsed.query.split("&"):
        key, separator, raw_value = item.partition("=")
        key, raw_value = unquote(key), unquote(raw_value)
        if not separator or key not in {"ref", "path"} or key in query:
            raise GitSecretReferenceError(
                "Git secret reference requires exactly one ref and one path"
            )
        query[key] = raw_value

    ref = query.get("ref", "")
    path = query.get("path", "")
    ref_parts = ref.split("/")
    if (
        not ref
        or len(ref) > 255
        or any(
            not _REF_PART.fullmatch(part)
            or part.endswith(".")
            or part.endswith(".lock")
            for part in ref_parts
        )
        or ".." in ref
        or "@{" in ref
    ):
        raise GitSecretReferenceError("Invalid Git secret branch reference")
    if (
        not path
        or len(path) > 1024
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or PurePosixPath(path).as_posix() != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise GitSecretReferenceError("Invalid Git secret file path")

    netloc = parsed.netloc
    transport_url = urlunsplit(("https", netloc, parsed.path, "", ""))
    display_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    repository = f"{display_host}{f':{port}' if port is not None else ''}/"
    repository += "/".join(repo_parts)
    return GitSecretReference(transport_url, repository, ref, path)


def _git_argv(*args: str) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise GitSecretReferenceError("Git is unavailable for HTTPS secret lookup")
    return [
        git,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "init.templateDir=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "credential.interactive=never",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.followRedirects=false",
        *args,
    ]


def _git_environment() -> dict[str, str]:
    """Pass only transport settings; ignore ambient Git credentials/config."""
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
        if key in os.environ
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return env


class _SecretOutputRunner(AuditedSubprocessRunner):
    """Audit process output sizes without persisting secret digests."""

    def _output_descriptors(self, stdout: bytes, stderr: bytes) -> dict[str, object]:
        return {
            "stdout_size": len(stdout),
            "stdout_sensitive": bool(stdout),
            "stderr_size": len(stderr),
            "stderr_sensitive": bool(stderr),
            "stdout_truncated": len(stdout) > self._output_limit,
            "stderr_truncated": len(stderr) > self._output_limit,
        }


async def _run_git(
    runner: AuditedSubprocessRunner,
    argv: list[str],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
) -> tuple[bytes, bytes]:
    try:
        result = await runner.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=_MAX_GIT_TIMEOUT,
            system_context=True,
        )
    except Exception:
        raise GitSecretReferenceError("HTTPS Git secret retrieval failed") from None
    if result.returncode != 0 or result.timed_out:
        raise GitSecretReferenceError("HTTPS Git secret retrieval failed")
    return result.stdout, result.stderr


def _remove_tree(path: Path, root: Path, filesystem: AuditedFilesystem) -> None:
    """Remove a temporary object database through the system filesystem facade."""
    if not path.exists() and not path.is_symlink():
        return
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            filesystem.unlink((current_path / name).relative_to(root), missing_ok=True)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                filesystem.unlink(child.relative_to(root), missing_ok=True)
            else:
                filesystem.rmdir(child.relative_to(root))
    filesystem.rmdir(path.relative_to(root))


class GitSecretReferenceProvider(SecretsProvider):
    """Interpret HTTPS Git pointers, delegating ordinary names unchanged."""

    def __init__(
        self,
        inner: SecretsProvider,
        *,
        temp_root: str | Path | None = None,
        runner_factory: Callable[..., AuditedSubprocessRunner] = _SecretOutputRunner,
    ) -> None:
        self._inner = inner
        self._temp_root = Path(temp_root or tempfile.gettempdir())
        self._runner_factory = runner_factory

    def _parse(self, path: str) -> GitSecretReference | None:
        try:
            return parse_git_secret_reference(path)
        except GitSecretReferenceError:
            raise
        except Exception:
            raise GitSecretReferenceError("Invalid Git secret reference URI") from None

    def _display_source(self, reference: GitSecretReference) -> str:
        return f"{reference.repository}@{reference.ref}"

    def _fetch_error(self, reference: GitSecretReference) -> GitSecretReferenceError:
        return GitSecretReferenceError(
            f"Git secret source '{self._display_source(reference)}' could not be "
            "resolved. Check anonymous HTTPS read access, TLS trust, network "
            "access, the configured ref/path, and server support for Git blob "
            "filtering."
        )

    async def _fetch(self, reference: GitSecretReference) -> bytes:
        filesystem = AuditedFilesystem.system(self._temp_root)
        temp_dir: Path | None = None
        try:
            temp_dir = filesystem.temporary_directory(prefix="git-secret-")
            filesystem.chmod(temp_dir.relative_to(self._temp_root), 0o700)
            repository = temp_dir / "source.git"
            env = _git_environment()
            runner = self._runner_factory(output_limit=_MAX_SECRET_BYTES + 1)
            await _run_git(
                runner,
                _git_argv("init", "--bare", "--quiet", str(repository)),
                env,
            )
            git_dir = ["--git-dir", str(repository)]
            await _run_git(
                runner,
                _git_argv(*git_dir, "remote", "add", "origin", reference.transport_url),
                env,
            )
            await _run_git(
                runner,
                _git_argv(*git_dir, "config", "remote.origin.promisor", "true"),
                env,
            )
            await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "config",
                    "remote.origin.partialclonefilter",
                    "blob:none",
                ),
                env,
            )
            _, fetch_stderr = await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--filter=blob:none",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{reference.ref}:refs/heads/secret-source",
                ),
                env,
            )
            fetch_warning = fetch_stderr.decode(errors="replace").lower()
            if any(
                warning in fetch_warning
                for warning in (
                    "filtering not recognized",
                    "does not support filter",
                    "filtering is not supported",
                    "ignoring filter",
                )
            ):
                raise GitSecretReferenceError(
                    "HTTPS Git server does not support filtered secret retrieval"
                )
            object_types, _ = await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "cat-file",
                    "--batch-all-objects",
                    "--batch-check=%(objecttype)",
                ),
                env,
            )
            if len(object_types) >= _MAX_SECRET_BYTES + 1:
                raise GitSecretReferenceError(
                    "Git secret repository metadata exceeds the size limit"
                )
            if b"blob\n" in object_types:
                raise GitSecretReferenceError(
                    "HTTPS Git server did not honor filtered secret retrieval"
                )
            commit_raw, _ = await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "rev-parse",
                    "--verify",
                    "refs/heads/secret-source^{commit}",
                ),
                env,
            )
            commit = commit_raw.decode("ascii", errors="ignore").strip()
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise GitSecretReferenceError("Git secret branch resolved invalidly")
            secret_raw, _ = await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "cat-file",
                    "blob",
                    f"{commit}:{reference.path}",
                ),
                env,
            )
            if len(secret_raw) > _MAX_SECRET_BYTES:
                raise GitSecretReferenceError("Git secret file exceeds the size limit")
            object_types, _ = await _run_git(
                runner,
                _git_argv(
                    *git_dir,
                    "cat-file",
                    "--batch-all-objects",
                    "--batch-check=%(objecttype)",
                ),
                env,
            )
            if (
                len(object_types) >= _MAX_SECRET_BYTES + 1
                or object_types.splitlines().count(b"blob") != 1
            ):
                raise GitSecretReferenceError(
                    "HTTPS Git server returned more than the requested secret blob"
                )
            return secret_raw
        except GitSecretReferenceError:
            raise self._fetch_error(reference) from None
        except Exception:
            raise self._fetch_error(reference) from None
        finally:
            if temp_dir is not None:
                try:
                    _remove_tree(temp_dir, self._temp_root, filesystem)
                except Exception:
                    raise self._fetch_error(reference) from None

    async def get_secret(self, path: str) -> str | None:
        reference = self._parse(path)
        if reference is None:
            return await self._inner.get_secret(path)
        secret = await self._fetch(reference)
        try:
            return secret.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise self._fetch_error(reference) from None

    async def get_secret_file(self, path: str) -> Path | None:
        reference = self._parse(path)
        if reference is not None:
            # A pointer has no stable file path. Use secret_file() so its
            # materialized file is removed as soon as the operation completes.
            return None
        return await self._inner.get_secret_file(path)

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        reference = self._parse(path)
        if reference is None:
            async with self._inner.secret_file(path) as secret_file:
                yield secret_file
            return

        content = await self._fetch(reference)
        filesystem = AuditedFilesystem.system(self._temp_root)
        temp_dir: Path | None = None
        try:
            temp_dir = filesystem.temporary_directory(prefix="git-secret-file-")
            filesystem.chmod(temp_dir.relative_to(self._temp_root), 0o700)
            secret_file = temp_dir / "secret"
            filesystem.write(
                secret_file.relative_to(self._temp_root),
                content,
                mode=0o600,
            )
        except Exception:
            if temp_dir is not None:
                try:
                    _remove_tree(temp_dir, self._temp_root, filesystem)
                except Exception:
                    pass
            raise self._fetch_error(reference) from None
        try:
            yield secret_file
        finally:
            try:
                _remove_tree(temp_dir, self._temp_root, filesystem)
            except Exception:
                raise self._fetch_error(reference) from None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return await self._inner.list_secrets(prefix)


def wrap_git_secret_references(provider: SecretsProvider) -> SecretsProvider:
    """Add pointer resolution once while preserving provider composition."""
    if isinstance(provider, GitSecretReferenceProvider):
        return provider
    return GitSecretReferenceProvider(provider)
