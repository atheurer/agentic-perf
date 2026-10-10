"""Resolve one secret file through GitLab's anonymous HTTPS API."""

from __future__ import annotations

import asyncio
import re
import ssl
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import httpx

from providers.execution import AuditedFilesystem

from .base import SecretsBackendError, SecretsProvider

_MAX_REFERENCE_LENGTH = 2048
_MAX_SECRET_BYTES = 64 * 1024
_MAX_SOCKET_TIMEOUT = 10
_MAX_TOTAL_TIMEOUT = 30
_SUPPORTED_GITLAB_HOST = "gitlab.cee.redhat.com"
_REF_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class GitSecretReferenceError(SecretsBackendError):
    """A configured HTTPS Git secret pointer is invalid or unavailable."""


@dataclass(frozen=True)
class GitSecretReference:
    """Validated identity and requested file for one Git secret pointer."""

    file_url: str
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
    if parsed.hostname.lower() != _SUPPORTED_GITLAB_HOST or (
        port is not None and port != 443
    ):
        raise GitSecretReferenceError(
            "Unsupported Git secret host; only GitLab HTTPS repository URLs on "
            f"{_SUPPORTED_GITLAB_HOST} are supported"
        )

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
    project_path = "/".join(repo_parts)
    if project_path.lower().endswith(".git"):
        project_path = project_path[:-4]
    if not project_path:
        raise GitSecretReferenceError("Invalid Git secret repository URI")
    file_url = urlunsplit(
        (
            "https",
            netloc,
            "/api/v4/projects/"
            + quote(project_path, safe="")
            + "/repository/files/"
            + quote(path, safe="")
            + "/raw",
            urlencode({"ref": ref}),
            "",
        )
    )
    display_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    repository = f"{display_host}{f':{port}' if port is not None else ''}/"
    repository += "/".join(repo_parts)
    return GitSecretReference(file_url, repository, ref, path)


def _build_http_client(
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build an anonymous client that ignores ambient auth and proxy settings."""
    return httpx.AsyncClient(
        transport=transport,
        verify=ssl.create_default_context(),
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(_MAX_SOCKET_TIMEOUT),
    )


class GitSecretReferenceProvider(SecretsProvider):
    """Resolve GitLab HTTPS pointers, delegating ordinary names unchanged."""

    def __init__(
        self,
        inner: SecretsProvider,
        *,
        temp_root: str | Path | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._inner = inner
        self._temp_root = Path(temp_root or tempfile.gettempdir())
        self._transport_factory = transport_factory

    def _parse(self, path: str) -> GitSecretReference | None:
        try:
            return parse_git_secret_reference(path)
        except GitSecretReferenceError:
            raise
        except Exception:
            raise GitSecretReferenceError("Invalid Git secret reference URI") from None

    def _display_source(self, reference: GitSecretReference) -> str:
        return f"{reference.repository}@{reference.ref}"

    def _fetch_error(
        self,
        reference: GitSecretReference,
        reason: str = "",
    ) -> GitSecretReferenceError:
        detail = f" {reason}." if reason else ""
        return GitSecretReferenceError(
            f"GitLab HTTPS secret source '{self._display_source(reference)}' could "
            f"not be resolved.{detail} Check anonymous HTTPS read access, TLS "
            "trust, network access, the project/ref/file path, and the 64 KiB "
            "file limit."
        )

    async def _fetch(self, reference: GitSecretReference) -> bytes:
        try:
            return await self._download(reference.file_url)
        except TimeoutError:
            raise self._fetch_error(
                reference, "the overall HTTPS request deadline was exceeded"
            ) from None
        except GitSecretReferenceError as exc:
            raise self._fetch_error(reference, str(exc)) from None
        except Exception:
            raise self._fetch_error(reference) from None

    async def _download(self, file_url: str) -> bytes:
        transport = self._transport_factory() if self._transport_factory else None
        async with asyncio.timeout(_MAX_TOTAL_TIMEOUT):
            async with _build_http_client(transport) as client:
                async with client.stream(
                    "GET",
                    file_url,
                    headers={
                        "Accept": "application/octet-stream",
                        "Accept-Encoding": "identity",
                        "User-Agent": "agentic-perf-secret-reader/1",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise GitSecretReferenceError(
                            self._http_status_reason(response.status_code)
                        )
                    encoding = (
                        response.headers.get("Content-Encoding", "identity")
                        .strip()
                        .lower()
                    )
                    if encoding not in {"", "identity"}:
                        raise GitSecretReferenceError(
                            "GitLab returned a non-identity encoded secret response"
                        )
                    content_length = response.headers.get("Content-Length")
                    declared_size: int | None = None
                    if content_length is not None:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            raise GitSecretReferenceError(
                                "GitLab returned an invalid secret content length"
                            ) from None
                        if declared_size < 0 or declared_size > _MAX_SECRET_BYTES:
                            raise GitSecretReferenceError(
                                "Git secret file exceeds the 64 KiB size limit"
                            )

                    content = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=8192):
                        if len(content) + len(chunk) > _MAX_SECRET_BYTES:
                            raise GitSecretReferenceError(
                                "Git secret file exceeds the 64 KiB size limit"
                            )
                        content.extend(chunk)
                    if declared_size is not None and len(content) != declared_size:
                        raise GitSecretReferenceError(
                            "GitLab returned an incomplete secret file"
                        )
                    return bytes(content)

    @staticmethod
    def _http_status_reason(status: int) -> str:
        if status == 401:
            return (
                "GitLab HTTPS API returned HTTP 401; anonymous read access is required"
            )
        if status == 403:
            return (
                "GitLab HTTPS API returned HTTP 403; confirm the project allows "
                "anonymous reads"
            )
        if status == 404:
            return (
                "GitLab HTTPS API returned HTTP 404; check the project, ref, and "
                "file path"
            )
        if 300 <= status < 400:
            return (
                f"GitLab HTTPS redirect HTTP {status} was rejected before reading "
                "its body; configure the raw API endpoint to serve the file "
                "without redirects"
            )
        return f"GitLab HTTPS API returned HTTP {status}"

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
        secret_file: Path | None = None
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
                    if secret_file is not None:
                        filesystem.unlink(
                            secret_file.relative_to(self._temp_root), missing_ok=True
                        )
                    filesystem.rmdir(temp_dir.relative_to(self._temp_root))
                except Exception:
                    pass
            raise self._fetch_error(reference) from None
        assert secret_file is not None
        try:
            yield secret_file
        finally:
            try:
                filesystem.unlink(
                    secret_file.relative_to(self._temp_root), missing_ok=True
                )
                filesystem.rmdir(temp_dir.relative_to(self._temp_root))
            except Exception:
                raise self._fetch_error(reference) from None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return await self._inner.list_secrets(prefix)


def wrap_git_secret_references(provider: SecretsProvider) -> SecretsProvider:
    """Add pointer resolution once while preserving provider composition."""
    if isinstance(provider, GitSecretReferenceProvider):
        return provider
    return GitSecretReferenceProvider(provider)
