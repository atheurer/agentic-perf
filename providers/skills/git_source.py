"""Authenticated, revision-pinned Git source for organization skills."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import stat
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, urlsplit

from paths import AGENTIC_PERF_HOME
from providers.execution import AuditedFilesystem, AuditedSubprocessRunner
from providers.secrets.base import SecretsProvider

_REF_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SCP_GIT_URL = re.compile(
    r"^(?P<username>[A-Za-z0-9._-]{1,64})@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^?#\s]+)$"
)
_MAX_SECRET_BYTES = 64 * 1024
_MAX_GIT_TIMEOUT = 180


class GitSourceError(ValueError):
    """A configured organization Git source is invalid or unavailable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PreparedGitSource:
    """An immutable local checkout and opaque identity for one Git binding."""

    root: Path
    commit: str
    identity: str


@dataclass(frozen=True)
class _GitSourceConfig:
    url: str
    ref: str
    auth_kind: str
    secret_ref: str | None
    username: str
    cache_key: str
    identity: str


def parse_git_source(source: object) -> _GitSourceConfig:
    """Validate supported URLs and auth references without accessing secrets."""
    if (
        not isinstance(source, dict)
        or source.get("kind") != "git"
        or set(source) - {"kind", "url", "ref", "auth"}
    ):
        raise GitSourceError(
            "invalid_config", "Invalid organization Git source configuration"
        )
    url = source.get("url")
    ref = source.get("ref", "main")
    auth = source.get("auth", {})
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or "?" in url
        or "#" in url
        or not isinstance(ref, str)
        or not isinstance(auth, dict)
    ):
        raise GitSourceError(
            "invalid_config", "Invalid organization Git source configuration"
        )
    if "://" not in url:
        scp_match = _SCP_GIT_URL.fullmatch(url)
        if scp_match:
            scp_path = scp_match.group("path")
            # scp-style paths are literal input. Encode reserved characters
            # before converting to ssh:// because Git URL-decodes that form.
            encoded_scp_path = quote(scp_path, safe="/~")
            if scp_path.startswith("/"):
                # SCP-style absolute paths remain absolute after conversion.
                ssh_path = encoded_scp_path
            elif scp_path.startswith("~"):
                # Preserve explicit remote-home expansion (including ~user).
                if scp_path == "~" or re.fullmatch(r"~[^/]+", scp_path):
                    raise GitSourceError(
                        "invalid_config", "Invalid organization Git source URL"
                    )
                ssh_path = f"/{encoded_scp_path}"
            else:
                # A relative scp-style path is relative to the remote user's
                # home. ssh:// needs an explicit /~/ path to retain that meaning.
                ssh_path = f"/~/{encoded_scp_path}"
            url = (
                f"ssh://{scp_match.group('username')}@{scp_match.group('host')}"
                f"{ssh_path}"
            )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise GitSourceError(
            "invalid_config", "Invalid organization Git source URL"
        ) from None
    if (
        parsed.scheme not in {"http", "https", "ssh"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
        or not parsed.path.strip("/")
    ):
        raise GitSourceError("invalid_config", "Invalid organization Git source URL")
    if parsed.scheme in {"http", "https"} and "@" in parsed.netloc:
        raise GitSourceError(
            "invalid_config", "Git credentials must use a secret reference"
        )
    if parsed.scheme == "ssh" and (
        parsed.password is not None
        or parsed.username is not None
        and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", parsed.username)
    ):
        raise GitSourceError("invalid_config", "Invalid organization SSH source URL")
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
        raise GitSourceError(
            "invalid_config", "Organization Git ref must be a branch name"
        )

    auth_kind = auth.get("kind", "default")
    secret_ref = auth.get("secret_ref")
    username = auth.get("username", "oauth2")
    allowed = {
        "default": set(),
        "https-token": {"secret_ref", "username"},
        "ssh-key-secret": {"secret_ref"},
    }
    if not isinstance(auth_kind, str) or auth_kind not in allowed:
        raise GitSourceError(
            "invalid_config", "Invalid organization Git authentication settings"
        )
    if set(auth) - {"kind", *allowed[auth_kind]}:
        raise GitSourceError(
            "invalid_config", "Invalid organization Git authentication settings"
        )
    if auth_kind == "https-token":
        if parsed.scheme != "https" or not _valid_secret_ref(secret_ref):
            raise GitSourceError(
                "invalid_config", "HTTPS token auth requires a secret reference"
            )
        if (
            not isinstance(username, str)
            or not username
            or len(username) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in username)
        ):
            raise GitSourceError(
                "invalid_config", "Invalid HTTPS Git authentication username"
            )
    elif auth_kind == "ssh-key-secret":
        if parsed.scheme != "ssh" or not _valid_secret_ref(secret_ref):
            raise GitSourceError(
                "invalid_config", "SSH key auth requires a secret reference"
            )
    elif secret_ref is not None or "username" in auth:
        raise GitSourceError(
            "invalid_config", "Unexpected organization Git authentication fields"
        )

    cache_key = hashlib.sha256(f"{url}\0{ref}".encode()).hexdigest()
    identity = hashlib.sha256(
        f"{url}\0{ref}\0{auth_kind}\0{secret_ref or ''}\0{username}".encode()
    ).hexdigest()
    return _GitSourceConfig(
        url=url,
        ref=ref,
        auth_kind=auth_kind,
        secret_ref=secret_ref,
        username=username,
        cache_key=cache_key,
        identity=identity,
    )


def _valid_secret_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SECRET_REF.fullmatch(value) is not None
        and ".." not in value.split("/")
        and "//" not in value
        and not value.endswith("/")
    )


@asynccontextmanager
async def _git_environment(
    config: _GitSourceConfig,
    secrets_provider: SecretsProvider | None,
) -> AsyncIterator[dict[str, str]]:
    """Build a child-only Git environment, materializing secrets just in time."""
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "SSH_AUTH_SOCK",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
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

    if config.auth_kind == "default":
        if config.url.startswith("ssh:"):
            ssh = shutil.which("ssh")
            if ssh is None:
                raise GitSourceError(
                    "organization_source_unavailable", "OpenSSH is unavailable"
                )
            env["GIT_SSH_COMMAND"] = shlex.join(
                [ssh, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
            )
        yield env
        return

    if secrets_provider is None or config.secret_ref is None:
        raise GitSourceError(
            "organization_auth_unavailable",
            "Organization Git authentication is not configured",
        )
    try:
        async with AsyncExitStack() as stack:
            secret_path = await stack.enter_async_context(
                secrets_provider.secret_file(config.secret_ref)
            )
            if secret_path is None:
                raise ValueError("configured secret is unavailable")
            secret_path = Path(secret_path)
            metadata = secret_path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size == 0
                or metadata.st_size > _MAX_SECRET_BYTES
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ValueError("configured secret file is invalid")
            if config.auth_kind == "ssh-key-secret":
                ssh = shutil.which("ssh")
                if ssh is None:
                    raise GitSourceError(
                        "organization_source_unavailable", "OpenSSH is unavailable"
                    )
                env["GIT_SSH_COMMAND"] = shlex.join(
                    [
                        ssh,
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-i",
                        str(secret_path),
                    ]
                )
            else:
                helper = Path(__file__).with_name("git_askpass.py")
                env.update(
                    {
                        "GIT_ASKPASS": shlex.join([sys.executable, str(helper)]),
                        "GIT_ASKPASS_REQUIRE": "force",
                        "AGENTIC_PERF_GIT_TOKEN_FILE": str(secret_path),
                        "AGENTIC_PERF_GIT_USERNAME": config.username,
                    }
                )
            yield env
    except GitSourceError:
        raise
    except Exception:
        raise GitSourceError(
            "organization_auth_unavailable",
            "Organization Git authentication is unavailable",
        ) from None


def _git_argv(*args: str) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise GitSourceError("organization_source_unavailable", "Git is unavailable")
    return [
        git,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "http.followRedirects=false",
        *args,
    ]


async def _run_git(
    runner: AuditedSubprocessRunner,
    argv: list[str],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
) -> str:
    try:
        result = await runner.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=_MAX_GIT_TIMEOUT,
            system_context=True,
        )
    except Exception:
        raise GitSourceError(
            "organization_source_unavailable",
            "Unable to retrieve the configured organization repository",
        ) from None
    if result.returncode != 0 or result.timed_out:
        raise GitSourceError(
            "organization_source_unavailable",
            "Unable to retrieve the configured organization repository",
        )
    return result.stdout.decode(errors="replace").strip()


async def _acquire_lock(fd: int) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            await asyncio.sleep(0.05)


def _remove_tree(relative: Path, root: Path) -> None:
    """Remove a failed temporary checkout through the audited filesystem."""
    filesystem = AuditedFilesystem.system(root, scheme="organization-git")
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


def _make_tree_read_only(relative: Path, root: Path) -> None:
    """Prevent accidental edits to a commit checkout reused from the cache."""
    filesystem = AuditedFilesystem.system(root, scheme="organization-git")
    path = root / relative
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            child = current_path / name
            if not child.is_symlink():
                mode = stat.S_IMODE(child.stat().st_mode)
                filesystem.chmod(child.relative_to(root), mode & ~0o222)
        for name in directories:
            child = current_path / name
            if not child.is_symlink():
                mode = stat.S_IMODE(child.stat().st_mode)
                filesystem.chmod(child.relative_to(root), mode & ~0o222)
        mode = stat.S_IMODE(current_path.stat().st_mode)
        filesystem.chmod(current_path.relative_to(root), mode & ~0o222)


async def prepare_git_source(
    source: object,
    *,
    secrets_provider: SecretsProvider | None = None,
    cache_root: str | Path | None = None,
    pinned_commit: str | None = None,
) -> PreparedGitSource:
    """Prepare an immutable commit tree, using an epoch pin without fetching."""
    config = parse_git_source(source)
    if pinned_commit is not None and not re.fullmatch(
        r"[a-f0-9]{40,64}", pinned_commit
    ):
        raise GitSourceError(
            "invalid_config", "Pinned organization repository revision is invalid"
        )
    root = Path(cache_root or (AGENTIC_PERF_HOME / "organization-git")).resolve()
    home = AGENTIC_PERF_HOME.resolve()
    if not root.is_absolute() or not root.is_relative_to(home):
        raise GitSourceError(
            "invalid_config", "Organization Git cache must be inside service storage"
        )
    try:
        filesystem = AuditedFilesystem.system(home)
        relative_root = root.relative_to(home)
        filesystem.mkdir(relative_root, mode=0o700)
        filesystem.chmod(relative_root, 0o700)
        for directory in ("mirrors", "checkouts", "locks"):
            filesystem.mkdir(relative_root / directory, mode=0o700)
            filesystem.chmod(relative_root / directory, 0o700)
        lock_rel = relative_root / "locks" / f"{config.cache_key}.lock"
        lock_fd = filesystem.open_descriptor(
            lock_rel,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            mode=0o600,
        )
    except Exception:
        raise GitSourceError(
            "organization_source_unavailable",
            "Unable to prepare organization repository storage",
        ) from None

    mirror_rel = relative_root / "mirrors" / f"{config.cache_key}.git"
    mirror = home / mirror_rel
    if pinned_commit is not None:
        checkout_rel = (
            relative_root / "checkouts" / f"{config.cache_key}-{pinned_commit}"
        )
        checkout = home / checkout_rel
        lock_acquired = False
        try:
            await _acquire_lock(lock_fd)
            lock_acquired = True
            if checkout.is_symlink() or not checkout.is_dir():
                raise ValueError("pinned checkout is missing")
            env = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
                if key in os.environ
            }
            env.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0",
                }
            )
            runner = AuditedSubprocessRunner(output_limit=4096)
            existing_commit = await _run_git(
                runner,
                _git_argv(
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ),
                env,
            )
            status = await _run_git(
                runner,
                _git_argv(
                    "-C",
                    str(checkout),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
                env,
            )
            if existing_commit != pinned_commit or status:
                raise ValueError("pinned checkout is invalid")
            return PreparedGitSource(
                root=checkout,
                commit=pinned_commit,
                identity=config.identity,
            )
        except GitSourceError as exc:
            if exc.code == "organization_source_unavailable":
                raise GitSourceError(
                    "organization_snapshot_unavailable",
                    "Pinned organization repository snapshot is unavailable",
                ) from None
            raise
        except Exception:
            raise GitSourceError(
                "organization_snapshot_unavailable",
                "Pinned organization repository snapshot is unavailable",
            ) from None
        finally:
            try:
                if lock_acquired:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    try:
        async with _git_environment(config, secrets_provider) as env:
            await _acquire_lock(lock_fd)
            runner = AuditedSubprocessRunner(output_limit=4096)
            if mirror.is_symlink():
                raise GitSourceError(
                    "organization_source_unavailable",
                    "Configured organization repository cache is invalid",
                )

            async def initialize_mirror() -> None:
                await _run_git(
                    runner,
                    _git_argv("init", "--bare", "--quiet", str(mirror)),
                    env,
                )
                await _run_git(
                    runner,
                    _git_argv(
                        "--git-dir", str(mirror), "remote", "add", "origin", config.url
                    ),
                    env,
                )

            initialize = not mirror.exists()
            if not initialize:
                try:
                    existing_url = await _run_git(
                        runner,
                        _git_argv(
                            "--git-dir", str(mirror), "remote", "get-url", "origin"
                        ),
                        env,
                    )
                    initialize = existing_url != config.url
                except GitSourceError:
                    initialize = True
                if initialize:
                    _remove_tree(mirror_rel, home)
            if initialize:
                await initialize_mirror()

            await _run_git(
                runner,
                _git_argv(
                    "--git-dir",
                    str(mirror),
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{config.ref}:refs/heads/agentic-perf-current",
                ),
                env,
            )
            commit = await _run_git(
                runner,
                _git_argv(
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "refs/heads/agentic-perf-current^{commit}",
                ),
                env,
            )
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise GitSourceError(
                    "organization_source_unavailable",
                    "Configured organization repository returned an invalid revision",
                )
            await _run_git(
                runner,
                _git_argv(
                    "--git-dir",
                    str(mirror),
                    "symbolic-ref",
                    "HEAD",
                    "refs/heads/agentic-perf-current",
                ),
                env,
            )

            checkout_rel = relative_root / "checkouts" / f"{config.cache_key}-{commit}"
            checkout = home / checkout_rel
            if checkout.is_symlink():
                raise GitSourceError(
                    "organization_source_unavailable",
                    "Configured organization repository cache is invalid",
                )
            checkout_valid = False
            if checkout.exists():
                try:
                    existing_commit = await _run_git(
                        runner,
                        _git_argv(
                            "-C",
                            str(checkout),
                            "rev-parse",
                            "--verify",
                            "HEAD^{commit}",
                        ),
                        env,
                    )
                    status = await _run_git(
                        runner,
                        _git_argv(
                            "-C",
                            str(checkout),
                            "status",
                            "--porcelain=v1",
                            "--untracked-files=all",
                        ),
                        env,
                    )
                    checkout_valid = existing_commit == commit and not status
                except GitSourceError:
                    checkout_valid = False
                if not checkout_valid:
                    _remove_tree(checkout_rel, home)
            if not checkout_valid:
                temp_rel = filesystem.temporary_directory(
                    relative_root / "checkouts", prefix="checkout-"
                ).relative_to(home)
                temp = home / temp_rel
                try:
                    await _run_git(
                        runner,
                        _git_argv(
                            "-c",
                            "init.templateDir=/dev/null",
                            "-c",
                            "protocol.file.allow=always",
                            "clone",
                            "--quiet",
                            "--shared",
                            "--dissociate",
                            "--no-checkout",
                            "--branch",
                            "agentic-perf-current",
                            str(mirror),
                            str(temp),
                        ),
                        env,
                    )
                    await _run_git(
                        runner,
                        _git_argv(
                            "-C",
                            str(temp),
                            "checkout",
                            "--detach",
                            "--quiet",
                            commit,
                        ),
                        env,
                    )
                    _make_tree_read_only(temp_rel, home)
                    filesystem.rename(temp_rel, checkout_rel)
                except Exception:
                    try:
                        _remove_tree(temp_rel, home)
                    except OSError:
                        pass
                    raise
            return PreparedGitSource(
                root=checkout,
                commit=commit,
                identity=config.identity,
            )
    except GitSourceError:
        raise
    except Exception:
        raise GitSourceError(
            "organization_source_unavailable",
            "Unable to prepare the configured organization repository",
        ) from None
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
