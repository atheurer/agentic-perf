"""Subject-bound organization documents and service-only configuration.

This resolver is transport independent. Software adapters remain independent
sources; organization preferences never replace installed-software facts.
Search requires GNU grep, using deadline-bound POSIX extended expressions
without backreferences. The transport is the existing audited process runner.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import yaml

from paths import AGENTIC_PERF_HOME, ARTIFACT_DIR, CONFIG_PATH, TICKET_DIR
from providers.execution import (
    AuditedFilesystem,
    AuditedSubprocessRunner,
    RootedPath,
    durable_filesystem_emitter,
)
from providers.secrets.base import SecretsProvider
from providers.skills.runtime_config import validate_runtime_config

MAX_PAGE_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 16 * MAX_DOCUMENT_BYTES
SUBJECT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillGatewayError(ValueError):
    """A configured subject cannot be safely resolved."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _subject(subject: str) -> str:
    if (
        not isinstance(subject, str)
        or not SUBJECT_PATTERN.fullmatch(subject)
        or any(len(part) > 64 for part in subject.split("/"))
    ):
        raise SkillGatewayError("invalid_subject", "Invalid subject identifier")
    return subject


def _relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise SkillGatewayError("invalid_document", "Invalid document path")
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or ":" in path or "\x00" in path:
        raise SkillGatewayError("invalid_document", "Invalid document path")
    normalized = str(value)
    if normalized in {".", ""}:
        raise SkillGatewayError("invalid_document", "Invalid document path")
    return normalized


def is_organization_ref(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith("skill://organization/")


def parse_organization_ref(ref: str) -> tuple[str, str, str]:
    """Return revision, subject and document; retain the supplying origin."""
    parsed = urlsplit(ref)
    parts = unquote(parsed.path).lstrip("/").split("/", 3)
    if (
        parsed.scheme != "skill"
        or parsed.netloc != "organization"
        or parsed.query
        or len(parts) != 4
        or not re.fullmatch(r"[a-f0-9]{64}", parts[0])
    ):
        raise SkillGatewayError("invalid_ref", "Invalid organization document ref")
    return parts[0], _subject("/".join(parts[1:3])), _relative(parts[3])


@dataclass(frozen=True)
class OrganizationBinding:
    subject: str
    root: Path | None
    service_config: Path | None
    required: bool = True
    legacy_config: bool = False
    repository_root: Path | None = None
    override_identity: str = ""
    source_identity: str = ""
    source_revision: str = ""

    @property
    def identity(self) -> str:
        if self.repository_root:
            # Adding a document/config counterpart changes a future capture,
            # while existing tickets retain their coherent pinned snapshot.
            return _digest(
                [
                    self.subject,
                    self.source_identity or str(self.repository_root),
                    self.override_identity,
                ]
            )
        return _digest(
            [self.subject, str(self.root), str(self.service_config), self.legacy_config]
        )


def discover_organization_bindings(
    repository_root: str | Path, *, required: bool = True
) -> dict[str, OrganizationBinding]:
    """Discover the exact package/config hierarchy without reading file contents.

    This metadata-only helper is suitable for redacted diagnostics. Full
    manifest, SKILL.md and configuration validation happens on resolution.
    Other repository directories, including migration notes, are not searched.
    """
    try:
        if not isinstance(required, bool):
            raise ValueError("required must be a boolean")
        root = Path(repository_root)
        if not root.is_absolute():
            raise ValueError("repository root must be absolute")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("repository root must be a directory")

        def checked(path: Path, *, directory: bool) -> Path:
            if path.is_symlink():
                raise ValueError("repository hierarchy cannot use symlinks")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("repository entry escapes root")
            if directory and not resolved.is_dir():
                raise ValueError("repository hierarchy needs directories")
            if not directory and (
                not resolved.is_file() or resolved.stat().st_nlink != 1
            ):
                raise ValueError("repository source must be an unlinked regular file")
            return resolved

        def children(path: Path, limit: int) -> list[Path]:
            entries = []
            for entry in checked(path, directory=True).iterdir():
                entries.append(entry)
                if len(entries) > limit:
                    raise ValueError("repository hierarchy exceeds limit")
            return sorted(entries)

        bindings: dict[str, OrganizationBinding] = {}
        for tree_name, documents in (("skills", True), ("service-config", False)):
            tree = root / tree_name
            if tree.is_symlink():
                raise ValueError("repository hierarchy cannot use symlinks")
            if not tree.exists():
                continue
            for namespace in children(tree, 64):
                if namespace.name.startswith(".") or namespace.is_file():
                    continue
                checked(namespace, directory=True)
                for entry in children(namespace, 256):
                    if entry.name.startswith("."):
                        continue
                    if documents:
                        if entry.is_file():
                            continue
                        package = checked(entry, directory=True)
                        marker = package / "skill.json"
                        checked(marker, directory=False)
                        subject = _subject(f"{namespace.name}/{package.name}")
                        prior = bindings.get(subject)
                        bindings[subject] = OrganizationBinding(
                            subject,
                            package,
                            prior.service_config if prior else None,
                            required,
                            repository_root=root,
                        )
                    else:
                        if entry.suffix != ".json":
                            continue
                        config = checked(entry, directory=False)
                        subject = _subject(f"{namespace.name}/{entry.stem}")
                        prior = bindings.get(subject)
                        bindings[subject] = OrganizationBinding(
                            subject,
                            prior.root if prior else None,
                            config,
                            required,
                            repository_root=root,
                        )
                    if len(bindings) > 256:
                        raise ValueError("repository subject count exceeds limit")
        if not bindings:
            raise ValueError("repository contains no subject packages or configs")
        return dict(sorted(bindings.items()))
    except (OSError, ValueError, TypeError, RuntimeError):
        raise SkillGatewayError(
            "organization_source_unavailable",
            "Configured organization repository is missing, invalid or unreadable",
        ) from None


class OrganizationSkillResolver:
    """Resolve explicit exports, pinning documents and config for one attempt.

    Snapshot storage is outside ticket workspaces and artifact roots. It is not
    a model-readable resource namespace; only document projections are exposed.
    Callers supply trusted ticket/attempt/phase values, never model arguments.
    """

    def __init__(
        self,
        bindings: dict[str, OrganizationBinding],
        *,
        snapshot_root: str | Path | None = None,
        audit_emit: Any | None = None,
        ticket_id: str = "",
        attempt_id: str = "",
        phase: str = "",
    ) -> None:
        self.bindings = bindings
        self.snapshot_root = Path(
            snapshot_root or AGENTIC_PERF_HOME / "skill-snapshots"
        ).resolve()
        for exposed in (TICKET_DIR.resolve(), ARTIFACT_DIR.resolve()):
            if self.snapshot_root.is_relative_to(exposed):
                raise SkillGatewayError(
                    "unsafe_snapshot_root", "Snapshot root must be service-only"
                )
        for binding in bindings.values():
            if binding.root and self.snapshot_root.is_relative_to(binding.root):
                raise SkillGatewayError(
                    "unsafe_snapshot_root", "Snapshot root is inside a served source"
                )
            if binding.repository_root and self.snapshot_root.is_relative_to(
                binding.repository_root / "skills"
            ):
                raise SkillGatewayError(
                    "unsafe_snapshot_root",
                    "Snapshot root is inside a served skills tree",
                )
        self.audit_emit = audit_emit
        self.ticket_id = ticket_id
        self.attempt_id = attempt_id
        self.phase = phase

    @classmethod
    def from_instance_config(
        cls,
        raw_config: dict[str, Any] | None = None,
        *,
        snapshot_root: str | Path | None = None,
        audit_emit: Any | None = None,
    ) -> OrganizationSkillResolver:
        if raw_config is None:
            try:
                raw_config = (
                    json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SkillGatewayError(
                    "invalid_config", "Cannot read instance skill configuration"
                ) from exc
        try:
            organization = raw_config.get("skill_gateway", {}).get("organization", {})
            subjects = organization.get("subjects", {})
            if not isinstance(subjects, dict):
                raise ValueError("subjects must be an object")
            bindings = {}
            required_default = organization.get("required", True)
            if not isinstance(required_default, bool):
                raise ValueError("required must be a boolean")
            repository = organization.get("source")
            repository_root = None
            if "source" in organization:
                if not isinstance(repository, dict) or repository.get("kind") != "path":
                    raise ValueError("unsupported organization source")
                bindings = discover_organization_bindings(
                    repository["path"], required=required_default
                )
                repository_root = Path(repository["path"]).resolve()
            for subject, value in subjects.items():
                _subject(subject)
                if not isinstance(value, dict):
                    raise ValueError("subject override must be an object")
                prior = bindings.get(subject)
                source = value.get("source")
                if source is not None and source.get("kind") != "path":
                    raise ValueError("unsupported organization source")
                root = (
                    (Path(source["path"]) if source else None)
                    if "source" in value
                    else prior.root
                    if prior
                    else None
                )
                runtime = value.get("service_config")
                if runtime is not None and (
                    not isinstance(runtime, dict)
                    or runtime.get("kind", "path") != "path"
                ):
                    raise ValueError("unsupported service configuration source")
                config = (
                    (Path(runtime["path"]) if runtime is not None else None)
                    if "service_config" in value
                    else prior.service_config
                    if prior
                    else None
                )
                legacy = value.get("legacy_config", False)
                if legacy and runtime is None:
                    config = None
                if (root and not root.is_absolute()) or (
                    config and not config.is_absolute()
                ):
                    raise ValueError("administrator source paths must be absolute")
                root = root.resolve() if root else None
                config = config.resolve() if config else None
                if config and root and config.is_relative_to(root):
                    raise ValueError("service configuration is inside served root")
                required = value.get(
                    "required", prior.required if prior else required_default
                )
                if not isinstance(legacy, bool) or not isinstance(required, bool):
                    raise ValueError("required and legacy_config must be booleans")
                if legacy and config:
                    raise ValueError("select one runtime configuration source")
                if root is None and config is None:
                    raise ValueError("subject has no organization source")
                bindings[subject] = OrganizationBinding(
                    subject,
                    root,
                    config,
                    required,
                    legacy,
                    repository_root=repository_root,
                    override_identity=_digest(
                        {
                            "source": str(root) if source is not None else None,
                            "service_config": str(config)
                            if runtime is not None
                            else None,
                            "source_explicit": "source" in value,
                            "service_config_explicit": "service_config" in value,
                            "legacy_config": legacy,
                        }
                    )
                    if "source" in value or "service_config" in value or legacy
                    else "",
                )
            for binding in bindings.values():
                if binding.service_config is None:
                    continue
                for other in bindings.values():
                    if other.root and binding.service_config.is_relative_to(other.root):
                        raise ValueError(
                            "service configuration is inside a served source"
                        )
                    if other.repository_root and binding.service_config.is_relative_to(
                        other.repository_root / "skills"
                    ):
                        raise ValueError(
                            "service configuration is inside served skills tree"
                        )
        except SkillGatewayError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OSError, RuntimeError):
            raise SkillGatewayError(
                "invalid_config", "Invalid organization skill source binding"
            ) from None
        return cls(bindings, snapshot_root=snapshot_root, audit_emit=audit_emit)

    @classmethod
    async def from_instance_config_async(
        cls,
        raw_config: dict[str, Any] | None = None,
        *,
        snapshot_root: str | Path | None = None,
        audit_emit: Any | None = None,
        secrets_provider: SecretsProvider | None = None,
    ) -> OrganizationSkillResolver:
        """Resolve Git sources before using the synchronous local resolver."""
        if raw_config is None:
            try:
                raw_config = (
                    json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
                )
            except (OSError, json.JSONDecodeError):
                raise SkillGatewayError(
                    "invalid_config", "Cannot read instance skill configuration"
                ) from None
        try:
            organization = raw_config.get("skill_gateway", {}).get("organization", {})
            source = organization.get("source")
            if not isinstance(source, dict) or source.get("kind") != "git":
                return cls.from_instance_config(
                    raw_config,
                    snapshot_root=snapshot_root,
                    audit_emit=audit_emit,
                )
            from providers.skills.git_source import GitSourceError, prepare_git_source

            try:
                prepared = await prepare_git_source(
                    source, secrets_provider=secrets_provider
                )
            except GitSourceError as exc:
                raise SkillGatewayError(exc.code, str(exc)) from None
            resolved_config = copy.deepcopy(raw_config)
            resolved_config["skill_gateway"]["organization"]["source"] = {
                "kind": "path",
                "path": str(prepared.root),
            }
            resolver = cls.from_instance_config(
                resolved_config,
                snapshot_root=snapshot_root,
                audit_emit=audit_emit,
            )
            bindings = {
                subject: replace(
                    binding,
                    source_identity=prepared.identity,
                    source_revision=prepared.commit,
                )
                for subject, binding in resolver.bindings.items()
            }
            return cls(
                bindings,
                snapshot_root=resolver.snapshot_root,
                audit_emit=audit_emit,
            )
        except SkillGatewayError:
            raise
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            raise SkillGatewayError(
                "invalid_config", "Invalid organization skill source binding"
            ) from None

    def for_attempt(
        self, ticket_id: str, attempt_id: str, phase: str
    ) -> OrganizationSkillResolver:
        if not ticket_id or not attempt_id or not phase:
            raise SkillGatewayError(
                "missing_attempt", "Trusted ticket, attempt and phase are required"
            )
        return type(self)(
            self.bindings,
            snapshot_root=self.snapshot_root,
            audit_emit=self.audit_emit,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            phase=phase,
        )

    def configured_subjects(self) -> list[str]:
        return sorted(self.bindings)

    def _pin_path(self, subject: str) -> Path:
        key = _digest([self.ticket_id, self.attempt_id, _subject(subject)])
        return self.snapshot_root / key / "pin.json"

    def has_subject(self, subject: str) -> bool:
        """Include removed pinned subjects so they never fall through to legacy."""
        if _subject(subject) in self.bindings:
            return True
        if not self.ticket_id or not self.attempt_id:
            return False
        try:
            # A started capture is also a tombstone if its commit pin was
            # removed or never completed; do not route that ticket to legacy.
            return self._pin_path(subject).parent.is_dir()
        except (OSError, RuntimeError):
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot inspect pinned skill source"
            ) from None

    @staticmethod
    def _read_bytes(path: Path, limit: int) -> bytes:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise SkillGatewayError("source_too_large", "Skill source exceeds limit")
        return content

    @classmethod
    def _source_document(cls, root: Path, relative: str) -> bytes:
        target = (root / _relative(relative)).resolve(strict=True)
        if (
            not target.is_relative_to(root)
            or not target.is_file()
            or target.stat().st_nlink != 1
        ):
            raise SkillGatewayError("invalid_document", "Document escapes source root")
        return cls._read_bytes(target, MAX_DOCUMENT_BYTES)

    def _capture_documents(
        self, binding: OrganizationBinding
    ) -> tuple[dict[str, str], list[dict[str, Any]], str, bytes]:
        try:
            assert binding.root is not None
            manifest_bytes = self._source_document(binding.root, "skill.json")
            if len(manifest_bytes) > 64 * 1024:
                raise ValueError("manifest exceeds limit")
            manifest = json.loads(manifest_bytes)
            if manifest.get("schema_version") != 1:
                raise ValueError("unsupported schema version")
            if manifest.get("subject") != binding.subject:
                raise ValueError("manifest subject mismatch")
            entries = manifest["documents"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
                raise ValueError("invalid document manifest")
            documents, exported = {}, []
            total = 0
            for entry in entries:
                relative = _relative(entry["path"])
                if not relative.endswith(".md") or relative in documents:
                    raise ValueError("exports must be distinct Markdown documents")
                phases = entry.get("phases", ["*"])
                if (
                    not isinstance(phases, list)
                    or not phases
                    or any(not isinstance(p, str) or not p for p in phases)
                    or not isinstance(entry.get("entrypoint", False), bool)
                ):
                    raise ValueError("invalid document applicability")
                content = self._source_document(binding.root, relative)
                if b"\x00" in content:
                    raise ValueError("document contains binary data")
                if any(
                    other.service_config is not None
                    and other.service_config.exists()
                    and (binding.root / relative).samefile(other.service_config)
                    for other in self.bindings.values()
                ):
                    raise ValueError("service configuration is exported as a document")
                documents[relative] = content.decode("utf-8")
                total += len(content)
                if total > MAX_PACKAGE_BYTES:
                    raise ValueError("package exceeds limit")
                exported.append(
                    {
                        "path": relative,
                        "phases": phases,
                        "entrypoint": entry.get("entrypoint", False),
                        "size_bytes": len(content),
                        "digest": hashlib.sha256(content).hexdigest(),
                    }
                )
            skill = documents.get("SKILL.md", "")
            if not skill.startswith("---\n"):
                raise ValueError("SKILL.md frontmatter missing")
            header = re.match(r"\A---\n(.*?)\n---(?:\n|$)", skill, re.DOTALL)
            if header is None or len(header[1].encode()) > 64 * 1024:
                raise ValueError("invalid skill frontmatter")
            frontmatter = yaml.safe_load(header[1])
            name = frontmatter.get("name", "")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or len(name) > 64
                or binding.root.name != name
                or not isinstance(frontmatter.get("description"), str)
                or not frontmatter["description"].strip()
                or len(frontmatter["description"]) > 1024
            ):
                raise ValueError("invalid skill name or description")
            metadata = frontmatter.get("metadata", {})
            if metadata.get("subject", binding.subject) != binding.subject:
                raise ValueError("skill subject mismatch")
            return documents, exported, name, manifest_bytes
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            yaml.YAMLError,
        ):
            raise SkillGatewayError(
                "organization_source_unavailable",
                "Configured organization skill documents are missing or invalid",
            ) from None

    def _capture(self, binding: OrganizationBinding) -> dict[str, Any]:
        try:
            documents, exported, name, manifest_bytes = {}, [], None, None
            if binding.root:
                documents, exported, name, manifest_bytes = self._capture_documents(
                    binding
                )
            runtime = {}
            runtime_bytes = None
            if binding.service_config:
                if (
                    binding.service_config.is_symlink()
                    or not binding.service_config.is_file()
                    or binding.service_config.stat().st_nlink != 1
                ):
                    raise ValueError(
                        "service configuration must be an unlinked regular file"
                    )
                runtime_bytes = self._read_bytes(
                    binding.service_config, MAX_DOCUMENT_BYTES
                )
                runtime = json.loads(runtime_bytes)
                if not isinstance(runtime, dict):
                    raise ValueError("service configuration must be an object")
            validate_runtime_config(binding.subject, runtime)
            # Detect updates during capture before pinning a mixed package.
            if (
                binding.root
                and self._source_document(binding.root, "skill.json") != manifest_bytes
            ):
                raise ValueError("manifest changed during capture")
            for relative, content in documents.items():
                assert binding.root is not None
                if self._source_document(binding.root, relative) != content.encode():
                    raise ValueError("document changed during capture")
            if (
                binding.service_config
                and self._read_bytes(binding.service_config, MAX_DOCUMENT_BYTES)
                != runtime_bytes
            ):
                raise ValueError("configuration changed during capture")
            # Document and approved configuration-view refs identify one
            # canonical snapshot, including config-only administrator updates.
            revision = _digest([exported, documents, runtime])
            return {
                "schema_version": 1,
                "subject": binding.subject,
                "binding": binding.identity,
                "source_revision": binding.source_revision or None,
                "revision": revision,
                "name": name,
                "service_configured": binding.service_config is not None,
                "documents": exported,
                "content": documents,
                "runtime_config": runtime,
            }
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            yaml.YAMLError,
        ):
            # Source errors must not include private paths or contents in tools/logs.
            raise SkillGatewayError(
                "organization_source_unavailable",
                "Configured organization skill is missing, invalid or changed",
            ) from None

    def _snapshot(self, subject: str) -> dict[str, Any] | None:
        binding = self.bindings.get(_subject(subject))
        if binding is None:
            if self.has_subject(subject):
                raise SkillGatewayError(
                    "organization_subject_removed",
                    "Pinned organization subject was removed",
                )
            return None
        if not self.ticket_id:
            return self._capture(binding)
        if not self.attempt_id:
            raise SkillGatewayError("missing_attempt", "Trusted attempt is required")
        key = _digest([self.ticket_id, self.attempt_id, subject])
        lock = None
        try:
            filesystem = AuditedFilesystem(
                RootedPath(self.snapshot_root, "skill-service"),
                ticket_id=self.ticket_id,
                emit=self.audit_emit or durable_filesystem_emitter(),
                critical=True,
            )
            filesystem.mkdir(".", mode=0o700)
            filesystem.mkdir(key, mode=0o700)
            lock = filesystem.open_descriptor(f"{key}/lock", os.O_CREAT | os.O_RDWR)
            fcntl.flock(lock, fcntl.LOCK_EX)
            pin_path = self.snapshot_root / key / "pin.json"
            if pin_path.exists():
                try:
                    pin = json.loads(pin_path.read_text())
                    snapshot_path = self.snapshot_root / key / "private-snapshot.json"
                    snapshot_bytes = self._read_bytes(
                        snapshot_path, 8 * MAX_PACKAGE_BYTES
                    )
                    if hashlib.sha256(snapshot_bytes).hexdigest() != pin["digest"]:
                        raise ValueError("snapshot digest mismatch")
                    snapshot = json.loads(snapshot_bytes)
                    if snapshot["binding"] != binding.identity:
                        raise ValueError("administrator source binding changed")
                    return snapshot
                except (OSError, ValueError, KeyError, TypeError, RecursionError):
                    raise SkillGatewayError(
                        "invalid_snapshot", "Pinned skill snapshot is unavailable"
                    ) from None
            snapshot = self._capture(binding)
            content = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
            filesystem.write(f"{key}/private-snapshot.json", content)
            filesystem.write(
                f"{key}/pin.json",
                json.dumps({"digest": hashlib.sha256(content).hexdigest()}),
            )
            return snapshot
        except SkillGatewayError:
            raise
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access durable skill snapshot storage"
            ) from None
        finally:
            if lock is not None:
                try:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    finally:
                        os.close(lock)
                except Exception:
                    raise SkillGatewayError(
                        "snapshot_unavailable", "Cannot release skill snapshot storage"
                    ) from None

    @staticmethod
    def _ref(snapshot: dict[str, Any], path: str) -> str:
        return (
            f"skill://organization/{snapshot['revision']}/{snapshot['subject']}/"
            f"{quote(path, safe='/')}"
        )

    def _visible(self, entry: dict[str, Any]) -> bool:
        return "*" in entry["phases"] or self.phase in entry["phases"]

    def bootstrap(self, subject: str) -> dict[str, Any]:
        binding = self.bindings.get(_subject(subject))
        if binding is None:
            if self.has_subject(subject):
                return {
                    "subject": subject,
                    "source": "organization",
                    "status": "unavailable",
                    "required": True,
                    "error": "organization_subject_removed",
                    "message": "Pinned organization subject was removed",
                }
            return {
                "subject": subject,
                "source": "organization",
                "status": "unconfigured",
            }
        try:
            snapshot = self._snapshot(subject)
        except SkillGatewayError as exc:
            return {
                "subject": subject,
                "source": "organization",
                "status": "unavailable",
                "required": binding.required,
                "error": exc.code,
                "message": str(exc),
            }
        assert snapshot is not None
        documents = [
            dict(entry, ref=self._ref(snapshot, entry["path"]))
            for entry in snapshot["documents"]
            if self._visible(entry)
        ]
        return {
            "subject": subject,
            "source": "organization",
            "scope": "organization",
            "status": "available",
            "required": binding.required,
            "revision": snapshot["revision"],
            "name": snapshot["name"],
            "service_configured": snapshot.get("service_configured", False),
            "documents_available": bool(documents),
            "phase": self.phase,
            "documents": documents,
            "entrypoints": [d["ref"] for d in documents if d["entrypoint"]],
        }

    def get_runtime_config(self, subject: str) -> dict[str, Any] | None:
        """Service API only; never register this method as a model resource."""
        binding = self.bindings.get(_subject(subject))
        if binding is None:
            if self.has_subject(subject):
                raise SkillGatewayError(
                    "organization_subject_removed",
                    "Pinned organization subject was removed",
                )
            return None
        if binding.legacy_config:
            self._snapshot(subject)
            return None
        snapshot = self._snapshot(subject)
        assert snapshot is not None
        return copy.deepcopy(snapshot["runtime_config"])

    def _document(
        self, subject: str, ref: str, from_ref: str | None
    ) -> tuple[dict[str, Any], str]:
        snapshot = self._snapshot(subject)
        if snapshot is None:
            raise SkillGatewayError("subject_unconfigured", "Subject is unconfigured")
        if not is_organization_ref(ref):
            if not from_ref:
                raise SkillGatewayError(
                    "origin_required", "Relative read needs origin ref"
                )
            origin_revision, origin_subject, origin_path = parse_organization_ref(
                from_ref
            )
            if origin_revision != snapshot["revision"] or origin_subject != subject:
                raise SkillGatewayError(
                    "invalid_origin", "Origin does not match snapshot"
                )
            origin_entry = next(
                (d for d in snapshot["documents"] if d["path"] == origin_path), None
            )
            if origin_entry is None or not self._visible(origin_entry):
                raise SkillGatewayError(
                    "invalid_origin", "Origin document is not exported for phase"
                )
            pointer = urlsplit(ref)
            if (
                pointer.scheme
                or pointer.netloc
                or pointer.query
                or "\\" in pointer.path
            ):
                raise SkillGatewayError(
                    "invalid_ref", "Invalid relative document pointer"
                )
            relative = (
                posixpath.normpath(
                    posixpath.join(
                        posixpath.dirname(origin_path), unquote(pointer.path)
                    )
                )
                if pointer.path
                else origin_path
            )
            ref = self._ref(snapshot, _relative(relative))
        revision, ref_subject, relative = parse_organization_ref(ref)
        if revision != snapshot["revision"] or ref_subject != subject:
            raise SkillGatewayError(
                "invalid_ref", "Document does not match pinned source"
            )
        entry = next((d for d in snapshot["documents"] if d["path"] == relative), None)
        if entry is None or not self._visible(entry):
            raise SkillGatewayError(
                "document_unavailable", "Document is not exported for phase"
            )
        return snapshot, relative

    @staticmethod
    def _page(content: str, offset: int, max_bytes: int) -> dict[str, Any]:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 4 <= max_bytes <= MAX_PAGE_BYTES
        ):
            raise SkillGatewayError("invalid_page", "Invalid bounded page request")
        data = content.encode("utf-8")
        if offset > len(data):
            raise SkillGatewayError("invalid_page", "Offset exceeds content size")
        try:
            data[:offset].decode("utf-8")
        except UnicodeDecodeError:
            raise SkillGatewayError(
                "invalid_page", "Offset splits UTF-8 character"
            ) from None
        page = data[offset : offset + max_bytes]
        while True:
            try:
                text = page.decode("utf-8")
                break
            except UnicodeDecodeError:
                page = page[:-1]
        next_offset = offset + len(page)
        return {
            "content": text,
            "offset": offset,
            "offset_bytes": offset,
            "next_offset": next_offset if next_offset < len(data) else None,
            "next_offset_bytes": next_offset if next_offset < len(data) else None,
            "size_bytes": len(data),
            "truncated": next_offset < len(data),
        }

    def read(
        self,
        subject: str,
        ref: str,
        *,
        from_ref: str | None = None,
        offset: int = 0,
        max_bytes: int = MAX_PAGE_BYTES,
    ) -> dict[str, Any]:
        snapshot, relative = self._document(subject, ref, from_ref)
        return {
            "subject": subject,
            "source": "organization",
            "ref": self._ref(snapshot, relative),
            "revision": snapshot["revision"],
            **self._page(snapshot["content"][relative], offset, max_bytes),
        }

    def search(
        self,
        subject: str,
        ref: str | None,
        pattern: str,
        *,
        from_ref: str | None = None,
        offset: int = 0,
        max_bytes: int = MAX_PAGE_BYTES,
    ) -> dict[str, Any]:
        if not pattern or len(pattern) > 256:
            raise SkillGatewayError("invalid_pattern", "Search pattern exceeds limit")
        if re.search(r"\\[1-9]", pattern):
            raise SkillGatewayError(
                "invalid_pattern", "Search backreferences are unsupported"
            )
        snapshot = self._snapshot(subject)
        if snapshot is None:
            raise SkillGatewayError("subject_unconfigured", "Subject is unconfigured")
        if ref:
            snapshot, relative = self._document(subject, ref, from_ref)
            paths = [relative]
        else:
            paths = [d["path"] for d in snapshot["documents"] if self._visible(d)]
        lines, positions = [], []
        for path in paths:
            source_lines = snapshot["content"][path].split("\n")
            if source_lines[-1] == "":
                source_lines.pop()
            for number, line in enumerate(source_lines, 1):
                lines.append(line)
                positions.append((path, number))
                if len(lines) > 200_000:
                    raise SkillGatewayError(
                        "source_too_large", "Search document line count exceeds limit"
                    )
        matches = []
        if lines:
            # GNU grep uses its bounded DFA path for ERE without backreferences.
            # A subprocess deadline also bounds unsupported or expensive input;
            # the child receives exported text, with no config or credentials.
            filesystem = None
            relative_search = None
            try:
                filesystem = (
                    AuditedFilesystem(
                        RootedPath(self.snapshot_root, "skill-service"),
                        ticket_id=self.ticket_id,
                        emit=self.audit_emit or durable_filesystem_emitter(),
                        critical=True,
                    )
                    if self.ticket_id
                    else AuditedFilesystem.system(
                        self.snapshot_root, scheme="skill-service"
                    )
                )
                search_file = filesystem.temporary_file(prefix="private-search-")
                relative_search = search_file.relative_to(self.snapshot_root)
                filesystem.write(relative_search, "\n".join(lines) + "\n")
                result = AuditedSubprocessRunner(
                    output_limit=2 * MAX_PACKAGE_BYTES
                ).run_sync(
                    [
                        "grep",
                        "-a",
                        "-h",
                        "-n",
                        "-E",
                        "-m",
                        "4097",
                        "--",
                        pattern,
                        str(search_file),
                    ],
                    timeout=2.0,
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                )
            except Exception:
                raise SkillGatewayError(
                    "search_unavailable", "Cannot access bounded skill search"
                ) from None
            finally:
                if filesystem is not None and relative_search is not None:
                    try:
                        filesystem.unlink(relative_search, missing_ok=True)
                    except Exception:
                        raise SkillGatewayError(
                            "search_unavailable", "Cannot release bounded skill search"
                        ) from None
            if result.timed_out:
                raise SkillGatewayError("search_timeout", "Search exceeded time limit")
            if result.returncode not in {0, 1}:
                raise SkillGatewayError("invalid_pattern", "Invalid search expression")
            for match in result.stdout.split(b"\n"):
                if not match:
                    continue
                number_bytes, text = match.split(b":", 1)
                path, number = positions[int(number_bytes) - 1]
                matches.append(
                    {
                        "ref": self._ref(snapshot, path),
                        "path": path,
                        "line": number,
                        "snippet": text.decode("utf-8")[:512],
                    }
                )
        search_limited = len(matches) > 4096
        matches = matches[:4096]
        records = [json.dumps(match, ensure_ascii=False) + "\n" for match in matches]
        body = "".join(records)
        self._page(body, offset, max_bytes)
        cursor, page_bytes, page_matches = 0, 0, []
        boundaries = {0}
        for record in records:
            cursor += len(record.encode())
            boundaries.add(cursor)
        if offset not in boundaries:
            raise SkillGatewayError(
                "invalid_page", "Search offset must use a returned record boundary"
            )
        cursor = 0
        for match, record in zip(matches, records, strict=True):
            size = len(record.encode())
            if cursor >= offset:
                if page_bytes + size > max_bytes:
                    if not page_matches:
                        raise SkillGatewayError(
                            "page_too_small", "Search page cannot fit a complete result"
                        )
                    break
                page_matches.append(match)
                page_bytes += size
            cursor += size
        next_offset = offset + page_bytes
        has_more = next_offset < len(body.encode())
        return {
            "subject": subject,
            "source": "organization",
            "revision": snapshot["revision"],
            "matches_count": len(matches),
            "matches": page_matches,
            "search_limited": search_limited,
            "offset": offset,
            "offset_bytes": offset,
            "next_offset": next_offset if has_more else None,
            "next_offset_bytes": next_offset if has_more else None,
            "size_bytes": len(body.encode()),
            "truncated": has_more or search_limited,
        }
