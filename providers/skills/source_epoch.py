"""Durable, ticket-scoped captures for mutable context sources."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from providers.execution import (
    AuditedFilesystem,
    RootedPath,
    durable_filesystem_emitter,
)

from .gateway import SkillGatewayError

_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9:/._-]{0,127}$")
_MAX_RECORD_BYTES = 64 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SourceEpoch:
    """Store immutable source captures for one trusted ticket.

    The index is the tombstone for a source capture: once indexed, a missing
    or corrupt snapshot fails closed instead of recapturing today's content.
    Source IDs and identities stay service-side; callers expose only revisions
    and document provenance that they explicitly project.
    """

    def __init__(
        self,
        snapshot_root: str | Path,
        *,
        ticket_id: str,
        audit_emit: Any | None = None,
    ) -> None:
        if not ticket_id:
            raise SkillGatewayError("missing_ticket", "Trusted ticket is required")
        self.snapshot_root = Path(snapshot_root).resolve()
        self.ticket_id = ticket_id
        self.key = hashlib.sha256(_canonical(["source-epoch", ticket_id])).hexdigest()
        self.audit_emit = audit_emit or durable_filesystem_emitter()

    @staticmethod
    def _source_key(source_id: str) -> str:
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise SkillGatewayError("invalid_source", "Invalid context source id")
        return hashlib.sha256(source_id.encode("utf-8")).hexdigest()

    def _filesystem(self) -> AuditedFilesystem:
        return AuditedFilesystem(
            RootedPath(self.snapshot_root, "skill-service"),
            ticket_id=self.ticket_id,
            emit=self.audit_emit,
            critical=True,
        )

    def _directory(self) -> Path:
        return self.snapshot_root / self.key

    def _lifecycle_path(self) -> Path:
        return self.snapshot_root / "lifecycle" / f"{self.key}.json"

    def _acquire_lock(self, filesystem: AuditedFilesystem) -> int:
        filesystem.mkdir(".", mode=0o700)
        filesystem.mkdir("locks", mode=0o700)
        lock_fd = filesystem.open_descriptor(
            f"locks/{self.key}.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            mode=0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd

    @staticmethod
    def _release_lock(filesystem: AuditedFilesystem, lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            try:
                filesystem.forget_descriptor(lock_fd)
            finally:
                os.close(lock_fd)

    def _is_closed(self) -> bool:
        lifecycle_path = self._lifecycle_path()
        pin_path = lifecycle_path.with_suffix(".pin.json")
        if lifecycle_path.parent.is_symlink():
            raise SkillGatewayError(
                "invalid_snapshot", "Ticket source lifecycle directory is corrupt"
            )
        if not any(
            path.exists() or path.is_symlink() for path in (lifecycle_path, pin_path)
        ):
            return False
        try:
            if lifecycle_path.is_symlink() or pin_path.is_symlink():
                raise ValueError("lifecycle record is a symlink")
            data = lifecycle_path.read_bytes()
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            value = json.loads(data)
            if (
                len(data) > 4096
                or pin.get("digest") != _digest(data)
                or value.get("schema_version") != 1
                or value.get("ticket_key") != self.key
                or value.get("ticket_id") != self.ticket_id
                or value.get("state") != "closed"
            ):
                raise ValueError("invalid lifecycle record")
            return True
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise SkillGatewayError(
                "invalid_snapshot", "Ticket source lifecycle record is corrupt"
            ) from None

    def _ensure_open(self) -> None:
        if self._is_closed():
            raise SkillGatewayError(
                "ticket_closed", "Ticket context snapshots have been closed"
            )

    def _write_index(self, sources: set[str]) -> None:
        filesystem = self._filesystem()
        index_bytes = _canonical(
            {
                "schema_version": 1,
                "ticket_id": self.ticket_id,
                "sources": sorted(sources),
            }
        )
        filesystem.write(f"{self.key}/epoch.json", index_bytes)
        filesystem.write(
            f"{self.key}/epoch.pin.json",
            _canonical({"digest": _digest(index_bytes)}),
        )

    def _mark_closed(self, filesystem: AuditedFilesystem) -> None:
        lifecycle_path = self._lifecycle_path()
        lifecycle_record = _canonical(
            {
                "schema_version": 1,
                "ticket_key": self.key,
                "ticket_id": self.ticket_id,
                "state": "closed",
            }
        )
        relative = lifecycle_path.relative_to(self.snapshot_root)
        pin_relative = lifecycle_path.with_suffix(".pin.json").relative_to(
            self.snapshot_root
        )
        filesystem.mkdir("lifecycle", mode=0o700)
        filesystem.write(relative, lifecycle_record)
        filesystem.write(
            pin_relative, _canonical({"digest": _digest(lifecycle_record)})
        )

    def _read_index(self) -> set[str]:
        directory = self._directory()
        index_path = directory / "epoch.json"
        pin_path = directory / "epoch.pin.json"
        if directory.is_symlink():
            raise SkillGatewayError(
                "invalid_snapshot", "Context epoch directory cannot be a symlink"
            )
        if not any(
            path.exists() or path.is_symlink() for path in (index_path, pin_path)
        ):
            return set()
        if (
            index_path.is_symlink()
            or pin_path.is_symlink()
            or not index_path.is_file()
            or not pin_path.is_file()
        ):
            raise SkillGatewayError(
                "invalid_snapshot", "Context epoch index is missing"
            )
        try:
            data = index_path.read_bytes()
            if len(data) > 1024 * 1024:
                raise ValueError("index exceeds limit")
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            if pin.get("digest") != _digest(data):
                raise ValueError("index digest mismatch")
            value = json.loads(data)
            sources = value["sources"]
            if (
                value.get("schema_version") != 1
                or value.get("ticket_id") != self.ticket_id
                or self.key
                != hashlib.sha256(
                    _canonical(["source-epoch", self.ticket_id])
                ).hexdigest()
                or not isinstance(sources, list)
                or any(not isinstance(item, str) for item in sources)
                or sources != sorted(set(sources))
            ):
                raise ValueError("invalid index")
            return set(sources)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise SkillGatewayError(
                "invalid_snapshot", "Context epoch index is corrupt"
            ) from None

    def _read_record(
        self, source_id: str, identity: str | None
    ) -> dict[str, Any] | None:
        source_key = self._source_key(source_id)
        directory = self._directory()
        indexed = source_id in self._read_index()
        record_path = directory / f"source-{source_key}.json"
        pin_path = directory / f"source-{source_key}.pin.json"
        if not indexed:
            if any(
                path.exists() or path.is_symlink() for path in (record_path, pin_path)
            ):
                raise SkillGatewayError(
                    "invalid_snapshot", "Context source pin is not indexed"
                )
            return None
        try:
            if (
                directory.is_symlink()
                or record_path.is_symlink()
                or pin_path.is_symlink()
            ):
                raise ValueError("context source pin cannot be a symlink")
            with record_path.open("rb") as handle:
                record_bytes = handle.read(_MAX_RECORD_BYTES + 1)
            if len(record_bytes) > _MAX_RECORD_BYTES:
                raise ValueError("source snapshot exceeds limit")
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            if pin.get("digest") != _digest(record_bytes):
                raise ValueError("source digest mismatch")
            record = json.loads(record_bytes)
            if (
                record.get("schema_version") != 1
                or record.get("source_id") != source_id
                or (identity is not None and record.get("identity") != identity)
                or not isinstance(record.get("snapshot"), dict)
            ):
                raise SkillGatewayError(
                    "source_binding_changed", "Context source binding changed"
                )
            snapshot = dict(record["snapshot"])
            captured_at = record.get("captured_at")
            if isinstance(captured_at, str):
                snapshot["captured_at"] = captured_at
            return snapshot
        except SkillGatewayError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned context source is unavailable"
            ) from None

    def read(self, source_id: str, identity: str) -> dict[str, Any] | None:
        """Return a verified pin without reading the live source."""
        self._source_key(source_id)
        filesystem = self._filesystem()
        try:
            lock_fd = self._acquire_lock(filesystem)
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            self._ensure_open()
            return self._read_record(source_id, identity)
        finally:
            self._release_lock(filesystem, lock_fd)

    def read_pinned(self, source_id: str) -> dict[str, Any] | None:
        """Return a verified ticket pin without consulting current source config."""
        self._source_key(source_id)
        filesystem = self._filesystem()
        try:
            lock_fd = self._acquire_lock(filesystem)
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            self._ensure_open()
            return self._read_record(source_id, None)
        finally:
            self._release_lock(filesystem, lock_fd)

    def cleanup_ticket(self) -> int:
        """Remove this ticket's source records after trusted closure cleanup."""
        directory = self._directory()
        filesystem = self._filesystem()
        try:
            lock_fd = self._acquire_lock(filesystem)
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            try:
                already_closed = self._is_closed()
            except SkillGatewayError:
                # Repair an interrupted tombstone write while holding the
                # stable ticket lock; cleanup is the only path that may do so.
                already_closed = False
            if not already_closed:
                self._mark_closed(filesystem)
            if not directory.is_dir() or directory.is_symlink():
                return 0
            removed_files = 0
            for current, directories, files in os.walk(
                directory, topdown=False, followlinks=False
            ):
                current_path = Path(current)
                for name in files:
                    filesystem.unlink(
                        (current_path / name).relative_to(self.snapshot_root),
                        missing_ok=True,
                    )
                    removed_files += 1
                for name in directories:
                    child = current_path / name
                    relative = child.relative_to(self.snapshot_root)
                    if child.is_symlink():
                        filesystem.unlink(relative, missing_ok=True)
                    else:
                        try:
                            filesystem.rmdir(relative)
                        except FileNotFoundError:
                            pass
            try:
                filesystem.rmdir(directory.relative_to(self.snapshot_root))
            except FileNotFoundError:
                pass
            return removed_files
        finally:
            self._release_lock(filesystem, lock_fd)

    @classmethod
    def ticket_ids_with_pins(cls, snapshot_root: str | Path) -> set[str]:
        """Return ticket IDs from verified source-epoch indexes for cleanup."""
        root = Path(snapshot_root).resolve()
        if not root.is_dir() or root.is_symlink():
            return set()
        ticket_ids = set()
        for directory in root.iterdir():
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not re.fullmatch(r"[a-f0-9]{64}", directory.name)
            ):
                continue
            index_path = directory / "epoch.json"
            pin_path = directory / "epoch.pin.json"
            try:
                if index_path.is_symlink() or pin_path.is_symlink():
                    continue
                data = index_path.read_bytes()
                if len(data) > 1024 * 1024:
                    continue
                pin = json.loads(pin_path.read_text(encoding="utf-8"))
                value = json.loads(data)
                ticket_id = value.get("ticket_id")
                sources = value.get("sources")
                key = hashlib.sha256(
                    _canonical(["source-epoch", ticket_id])
                ).hexdigest()
                if (
                    pin.get("digest") == _digest(data)
                    and value.get("schema_version") == 1
                    and isinstance(ticket_id, str)
                    and bool(ticket_id)
                    and key == directory.name
                    and isinstance(sources, list)
                    and all(isinstance(item, str) for item in sources)
                    and sources == sorted(set(sources))
                ):
                    ticket_ids.add(ticket_id)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
        lifecycle_dir = root / "lifecycle"
        if lifecycle_dir.is_dir() and not lifecycle_dir.is_symlink():
            for lifecycle_path in lifecycle_dir.glob("*.json"):
                if (
                    lifecycle_path.name.endswith(".pin.json")
                    or lifecycle_path.is_symlink()
                ):
                    continue
                pin_path = lifecycle_path.with_suffix(".pin.json")
                try:
                    data = lifecycle_path.read_bytes()
                    if len(data) > 4096 or pin_path.is_symlink():
                        continue
                    pin = json.loads(pin_path.read_text(encoding="utf-8"))
                    value = json.loads(data)
                    ticket_id = value.get("ticket_id")
                    key = hashlib.sha256(
                        _canonical(["source-epoch", ticket_id])
                    ).hexdigest()
                    if (
                        pin.get("digest") == _digest(data)
                        and value.get("schema_version") == 1
                        and value.get("state") == "closed"
                        and isinstance(ticket_id, str)
                        and bool(ticket_id)
                        and value.get("ticket_key") == key
                        and lifecycle_path.stem == key
                        and (root / key).exists()
                    ):
                        ticket_ids.add(ticket_id)
                except (OSError, ValueError, TypeError, RecursionError):
                    continue
        return ticket_ids

    def get_or_capture(
        self,
        source_id: str,
        identity: str,
        capture: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Return an existing verified pin or durably capture it exactly once."""
        source_key = self._source_key(source_id)
        filesystem = self._filesystem()
        directory = self._directory()
        try:
            lock_fd = self._acquire_lock(filesystem)
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            self._ensure_open()
            filesystem.mkdir(self.key, mode=0o700)
            # A ticket always reuses its first verified capture, even when
            # current instance configuration has since changed.
            existing = self._read_record(source_id, None)
            if existing is not None:
                return existing
            # Register in-flight captures before network or filesystem work so
            # the closed-ticket sweeper can find and fence them.
            self._write_index(self._read_index())
            snapshot = capture()
            if not isinstance(snapshot, dict):
                raise SkillGatewayError(
                    "source_snapshot_unavailable", "Context source capture is invalid"
                )
            record = {
                "schema_version": 1,
                "source_id": source_id,
                "identity": identity,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "snapshot": snapshot,
            }
            record_bytes = _canonical(record)
            if len(record_bytes) > _MAX_RECORD_BYTES:
                raise SkillGatewayError(
                    "source_too_large", "Context source snapshot exceeds its limit"
                )
            pin_bytes = _canonical({"digest": _digest(record_bytes)})
            record_name = f"source-{source_key}.json"
            pin_name = f"source-{source_key}.pin.json"
            if any(
                path.exists() or path.is_symlink()
                for path in (directory / record_name, directory / pin_name)
            ):
                raise SkillGatewayError(
                    "invalid_snapshot", "Pinned context source is incomplete"
                )
            filesystem.write(f"{self.key}/{record_name}", record_bytes)
            filesystem.write(f"{self.key}/{pin_name}", pin_bytes)
            sources = self._read_index()
            sources.add(source_id)
            self._write_index(sources)
            result = dict(snapshot)
            result["captured_at"] = record["captured_at"]
            return result
        except SkillGatewayError:
            raise
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot persist context source snapshot"
            ) from None
        finally:
            self._release_lock(filesystem, lock_fd)

    def capture_git_repository(
        self,
        source_id: str,
        identity: str,
        repository_url: str,
        *,
        ref: str = "HEAD",
    ) -> dict[str, Any]:
        """Fetch an exact remote commit into an immutable ticket-owned checkout."""
        parsed = urlsplit(repository_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "\x00" in repository_url
        ):
            raise SkillGatewayError(
                "invalid_source", "Upstream source must be a credential-free HTTPS URL"
            )
        if (
            not isinstance(ref, str)
            or not ref
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", ref)
            or ref.startswith("-")
            or ".." in ref.split("/")
        ):
            raise SkillGatewayError("invalid_source", "Invalid upstream revision ref")

        def capture() -> dict[str, Any]:
            from providers.execution import AuditedSubprocessRunner

            source_key = self._source_key(source_id)
            filesystem = self._filesystem()
            repository_relative = f"{self.key}/repositories/{source_key}.git"
            checkout_relative = f"{self.key}/checkouts/{source_key}"
            repository_path = self.snapshot_root / repository_relative
            checkout_path = self.snapshot_root / checkout_relative
            filesystem.mkdir(f"{self.key}/repositories", mode=0o700)
            filesystem.mkdir(f"{self.key}/checkouts", mode=0o700)
            self._remove_tree(filesystem, repository_path)
            self._remove_tree(filesystem, checkout_path)
            runner = AuditedSubprocessRunner(output_limit=64 * 1024)
            try:
                initialized = runner.run_sync(
                    ["git", "init", "--bare", str(repository_path)],
                    timeout=15,
                    mutating=True,
                )
                if initialized.returncode != 0:
                    raise SkillGatewayError(
                        "source_snapshot_unavailable", "Cannot prepare upstream capture"
                    )
                resolved = runner.run_sync(
                    ["git", "ls-remote", "--symref", repository_url, ref],
                    timeout=30,
                )
                if resolved.returncode != 0 or len(resolved.stdout) >= 64 * 1024:
                    raise SkillGatewayError(
                        "source_snapshot_unavailable",
                        "Cannot resolve upstream revision",
                    )
                candidates = []
                for line in resolved.stdout.decode(
                    "utf-8", errors="replace"
                ).splitlines():
                    fields = line.split("\t", 1)
                    if len(fields) != 2 or not re.fullmatch(
                        r"[0-9a-f]{40,64}", fields[0]
                    ):
                        continue
                    remote_ref = fields[1]
                    if remote_ref.endswith("^{}"):
                        candidates.insert(
                            0, (remote_ref.removesuffix("^{}"), fields[0])
                        )
                    else:
                        candidates.append((remote_ref, fields[0]))
                if re.fullmatch(r"[0-9a-f]{40,64}", ref):
                    revision = ref
                elif ref == "HEAD":
                    revision = next(
                        (sha for remote_ref, sha in candidates if remote_ref == "HEAD"),
                        None,
                    )
                else:
                    wanted = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
                    revision = next(
                        (sha for remote_ref, sha in candidates if remote_ref == wanted),
                        None,
                    )
                    if revision is None and not ref.startswith("refs/"):
                        revision = next(
                            (
                                sha
                                for remote_ref, sha in candidates
                                if remote_ref == f"refs/tags/{ref}"
                            ),
                            None,
                        )
                if revision is None:
                    raise SkillGatewayError(
                        "source_snapshot_unavailable",
                        "Upstream did not advertise the selected ref",
                    )
                fetched = runner.run_sync(
                    [
                        "git",
                        "--git-dir",
                        str(repository_path),
                        "fetch",
                        "--depth=1",
                        repository_url,
                        f"{revision}:refs/agentic-perf/pinned",
                    ],
                    timeout=120,
                    mutating=True,
                )
                if fetched.returncode != 0:
                    raise SkillGatewayError(
                        "source_snapshot_unavailable",
                        "Cannot fetch the resolved upstream revision",
                    )
                commit_result = runner.run_sync(
                    [
                        "git",
                        "--git-dir",
                        str(repository_path),
                        "rev-parse",
                        "refs/agentic-perf/pinned^{commit}",
                    ],
                    timeout=10,
                )
                commit = commit_result.stdout.decode("ascii", errors="ignore").strip()
                if commit_result.returncode != 0 or not re.fullmatch(
                    r"[0-9a-f]{40,64}", commit
                ):
                    raise SkillGatewayError(
                        "source_snapshot_unavailable",
                        "Fetched upstream commit is invalid",
                    )
                checked_out = runner.run_sync(
                    [
                        "git",
                        "--git-dir",
                        str(repository_path),
                        "worktree",
                        "add",
                        "--detach",
                        str(checkout_path),
                        commit,
                    ],
                    timeout=60,
                    mutating=True,
                )
                if checked_out.returncode != 0:
                    raise SkillGatewayError(
                        "source_snapshot_unavailable",
                        "Cannot materialize pinned upstream content",
                    )
                return {
                    "revision": commit,
                    "repository": repository_url,
                    "ref": ref,
                    "repository_path": repository_relative,
                    "checkout_path": checkout_relative,
                }
            except BaseException:
                self._remove_tree(filesystem, repository_path)
                self._remove_tree(filesystem, checkout_path)
                raise

        from providers.execution import AuditedSubprocessRunner

        snapshot = self.get_or_capture(source_id, identity, capture)
        source_key = self._source_key(source_id)
        repository_relative = f"{self.key}/repositories/{source_key}.git"
        checkout_relative = f"{self.key}/checkouts/{source_key}"
        if (
            snapshot.get("repository_path") != repository_relative
            or snapshot.get("checkout_path") != checkout_relative
        ):
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned upstream content is unavailable"
            )
        expected_repository = self.snapshot_root / repository_relative
        expected_checkout = self.snapshot_root / checkout_relative
        try:
            expected_repository.resolve(strict=True).relative_to(self._directory())
            expected_checkout.resolve(strict=True).relative_to(self._directory())
        except (OSError, ValueError, RuntimeError):
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned upstream content is unavailable"
            ) from None
        if expected_repository.is_symlink() or expected_checkout.is_symlink():
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned upstream content is unavailable"
            )
        runner = AuditedSubprocessRunner()
        verified = runner.run_sync(
            ["git", "-C", str(expected_checkout), "rev-parse", "HEAD"], timeout=10
        )
        revision = verified.stdout.decode("ascii", errors="ignore").strip()
        if verified.returncode != 0 or revision != snapshot.get("revision"):
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned upstream revision no longer matches"
            )
        status = runner.run_sync(
            [
                "git",
                "-C",
                str(expected_checkout),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            timeout=10,
        )
        if status.returncode != 0 or status.stdout:
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned upstream checkout was modified"
            )
        return snapshot

    def _remove_tree(self, filesystem: AuditedFilesystem, target: Path) -> None:
        """Remove a ticket-owned capture directory through the audited facade."""
        if target.is_symlink():
            filesystem.unlink(target.relative_to(self.snapshot_root), missing_ok=True)
            return
        if not target.is_dir():
            if target.exists():
                filesystem.unlink(
                    target.relative_to(self.snapshot_root), missing_ok=True
                )
            return
        for current, directories, files in os.walk(
            target, topdown=False, followlinks=False
        ):
            current_path = Path(current)
            for name in files:
                filesystem.unlink(
                    (current_path / name).relative_to(self.snapshot_root),
                    missing_ok=True,
                )
            for name in directories:
                child = current_path / name
                relative = child.relative_to(self.snapshot_root)
                if child.is_symlink():
                    filesystem.unlink(relative, missing_ok=True)
                else:
                    try:
                        filesystem.rmdir(relative)
                    except FileNotFoundError:
                        pass
        try:
            filesystem.rmdir(target.relative_to(self.snapshot_root))
        except FileNotFoundError:
            pass
