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
    source_id: str = "default"

    @property
    def identity(self) -> str:
        if self.repository_root:
            # Adding a document/config counterpart changes a future capture,
            # while existing tickets retain their coherent pinned snapshot.
            return _digest(
                [
                    self.subject,
                    self.source_id,
                    self.source_identity or str(self.repository_root),
                    self.override_identity,
                ]
            )
        return _digest(
            [
                self.subject,
                self.source_id,
                str(self.root),
                str(self.service_config),
                self.legacy_config,
            ]
        )


def discover_organization_bindings(
    repository_root: str | Path,
    *,
    required: bool = True,
    source_id: str = "default",
) -> dict[str, OrganizationBinding]:
    """Discover the exact package/config hierarchy without reading file contents.

    This metadata-only helper is suitable for redacted diagnostics. Full
    manifest, SKILL.md and configuration validation happens on resolution.
    Other repository directories, including migration notes, are not searched.
    """
    try:
        if not isinstance(required, bool):
            raise ValueError("required must be a boolean")
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id)
            or len(source_id) > 64
        ):
            raise ValueError("source id must be a lowercase identifier")
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
                            source_id=source_id,
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
                            source_id=source_id,
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


def organization_source_descriptors(
    organization: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize the single-source shorthand and named source collection."""
    if "source" in organization and "sources" in organization:
        raise ValueError("configure source or sources, not both")
    if "sources" in organization:
        values = organization["sources"]
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError("sources must be a nonempty list of at most 32 entries")
        descriptors = values
        require_id = True
    elif "source" in organization:
        descriptors = [organization["source"]]
        require_id = False
    else:
        return []

    result = []
    seen = set()
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict):
            raise ValueError("organization source must be an object")
        source_id = descriptor.get("id", "default" if not require_id else None)
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id)
            or len(source_id) > 64
            or source_id in seen
        ):
            raise ValueError("organization source ids must be unique identifiers")
        seen.add(source_id)
        source = {key: value for key, value in descriptor.items() if key != "id"}
        if source.get("kind") not in {"path", "git"}:
            raise ValueError("unsupported organization source")
        result.append({"id": source_id, "source": source})
    return result


class OrganizationSkillResolver:
    """Resolve explicit exports, pinning documents and config for one attempt.

    Snapshot storage is outside ticket workspaces and artifact roots. It is not
    a model-readable resource namespace; only document projections are exposed.
    Callers supply trusted ticket/attempt/phase values, never model arguments.
    """

    def __init__(
        self,
        bindings: dict[
            str,
            OrganizationBinding
            | list[OrganizationBinding]
            | tuple[OrganizationBinding, ...],
        ],
        *,
        snapshot_root: str | Path | None = None,
        audit_emit: Any | None = None,
        ticket_id: str = "",
        attempt_id: str = "",
        phase: str = "",
    ) -> None:
        self.bindings = {
            subject: tuple(value) if isinstance(value, (list, tuple)) else (value,)
            for subject, value in bindings.items()
        }
        self.snapshot_root = Path(
            snapshot_root or AGENTIC_PERF_HOME / "skill-snapshots"
        ).resolve()
        for exposed in (TICKET_DIR.resolve(), ARTIFACT_DIR.resolve()):
            if self.snapshot_root.is_relative_to(exposed):
                raise SkillGatewayError(
                    "unsafe_snapshot_root", "Snapshot root must be service-only"
                )
        for group in self.bindings.values():
            for binding in group:
                if binding.root and self.snapshot_root.is_relative_to(binding.root):
                    raise SkillGatewayError(
                        "unsafe_snapshot_root",
                        "Snapshot root is inside a served source",
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
            bindings: dict[str, list[OrganizationBinding]] = {}
            required_default = organization.get("required", True)
            if not isinstance(required_default, bool):
                raise ValueError("required must be a boolean")
            descriptors = organization_source_descriptors(organization)
            source_ids = {item["id"] for item in descriptors}
            for descriptor in descriptors:
                source_id = descriptor["id"]
                repository = descriptor["source"]
                if repository.get("kind") != "path":
                    raise ValueError(
                        "Git organization sources require async resolution"
                    )
                discovered = discover_organization_bindings(
                    repository["path"],
                    required=required_default,
                    source_id=source_id,
                )
                for subject, binding in discovered.items():
                    bindings.setdefault(subject, []).append(binding)
            for subject, value in subjects.items():
                _subject(subject)
                if not isinstance(value, dict):
                    raise ValueError("subject override must be an object")
                prior_group = bindings.get(subject, [])
                source_id = value.get("source_id")
                if source_id is not None and (
                    not isinstance(source_id, str) or source_id not in source_ids
                ):
                    raise ValueError("subject override selects unknown source id")
                changes_source = any(
                    key in value
                    for key in ("source", "service_config", "legacy_config")
                )
                if source_id is not None:
                    selected = [b for b in prior_group if b.source_id == source_id]
                    if not selected and prior_group:
                        raise ValueError(
                            "subject override source does not contain subject"
                        )
                elif len(prior_group) == 1:
                    selected = prior_group
                elif len(prior_group) > 1 and changes_source:
                    raise ValueError("ambiguous subject override requires source_id")
                else:
                    selected = prior_group
                source = value.get("source")
                if source is not None and (
                    not isinstance(source, dict) or source.get("kind") != "path"
                ):
                    raise ValueError("unsupported organization source")
                runtime = value.get("service_config")
                if runtime is not None and (
                    not isinstance(runtime, dict)
                    or runtime.get("kind", "path") != "path"
                ):
                    raise ValueError("unsupported service configuration source")
                legacy = value.get("legacy_config")
                if legacy is not None and not isinstance(legacy, bool):
                    raise ValueError("legacy_config must be a boolean")
                required = value.get("required")
                if required is not None and not isinstance(required, bool):
                    raise ValueError("required must be a boolean")
                if not prior_group and not any(
                    key in value for key in ("source", "service_config")
                ):
                    raise ValueError("subject has no organization source")
                if not prior_group:
                    new_id = source_id or "override"
                    if new_id not in source_ids and source_id is not None:
                        raise ValueError("unknown source_id")
                    prior_group = [
                        OrganizationBinding(
                            subject,
                            None,
                            None,
                            required_default,
                            source_id=new_id,
                        )
                    ]
                targets = selected or prior_group
                updated = []
                for prior in prior_group:
                    if prior not in targets:
                        updated.append(prior)
                        continue
                    root = (
                        (Path(source["path"]) if source is not None else None)
                        if "source" in value
                        else prior.root
                    )
                    config = (
                        (Path(runtime["path"]) if runtime is not None else None)
                        if "service_config" in value
                        else prior.service_config
                    )
                    use_legacy = legacy if legacy is not None else prior.legacy_config
                    if use_legacy and "service_config" not in value:
                        config = None
                    if (root and not root.is_absolute()) or (
                        config and not config.is_absolute()
                    ):
                        raise ValueError("administrator source paths must be absolute")
                    root = root.resolve() if root else None
                    config = config.resolve() if config else None
                    if config and root and config.is_relative_to(root):
                        raise ValueError("service configuration is inside served root")
                    if use_legacy and config:
                        raise ValueError("select one runtime configuration source")
                    if root is None and config is None and not use_legacy:
                        raise ValueError("subject has no organization source")
                    updated.append(
                        replace(
                            prior,
                            root=root,
                            service_config=config,
                            required=required
                            if required is not None
                            else prior.required,
                            legacy_config=use_legacy,
                            override_identity=_digest(
                                {
                                    "source": str(root) if "source" in value else None,
                                    "service_config": str(config)
                                    if "service_config" in value
                                    else None,
                                    "source_explicit": "source" in value,
                                    "service_config_explicit": "service_config"
                                    in value,
                                    "legacy_config": use_legacy,
                                }
                            )
                            if changes_source
                            else prior.override_identity,
                        )
                    )
                if required is not None and not changes_source:
                    updated = [replace(item, required=required) for item in updated]
                bindings[subject] = updated
            flat_bindings = [
                binding for group in bindings.values() for binding in group
            ]
            for binding in flat_bindings:
                if binding.service_config is None:
                    continue
                for other in flat_bindings:
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
            descriptors = organization_source_descriptors(organization)
            if not any(item["source"].get("kind") == "git" for item in descriptors):
                return cls.from_instance_config(
                    raw_config,
                    snapshot_root=snapshot_root,
                    audit_emit=audit_emit,
                )
            from providers.skills.git_source import GitSourceError, prepare_git_source

            prepared_sources = {}
            resolved_sources = []
            for item in descriptors:
                source_id, source = item["id"], item["source"]
                if source.get("kind") == "git":
                    try:
                        prepared = await prepare_git_source(
                            source, secrets_provider=secrets_provider
                        )
                    except GitSourceError as exc:
                        raise SkillGatewayError(exc.code, str(exc)) from None
                    prepared_sources[source_id] = prepared
                    resolved_sources.append(
                        {
                            "id": source_id,
                            "kind": "path",
                            "path": str(prepared.root),
                        }
                    )
                else:
                    resolved_sources.append({"id": source_id, **source})
            resolved_config = copy.deepcopy(raw_config)
            resolved_organization = resolved_config["skill_gateway"]["organization"]
            if "sources" in resolved_organization:
                resolved_organization["sources"] = resolved_sources
            else:
                resolved_organization["source"] = {
                    **resolved_sources[0],
                    "id": resolved_sources[0]["id"],
                }
            resolver = cls.from_instance_config(
                resolved_config,
                snapshot_root=snapshot_root,
                audit_emit=audit_emit,
            )
            bindings = {
                subject: tuple(
                    replace(
                        binding,
                        source_identity=prepared_sources[binding.source_id].identity,
                        source_revision=prepared_sources[binding.source_id].commit,
                    )
                    if binding.source_id in prepared_sources
                    else binding
                    for binding in group
                )
                for subject, group in resolver.bindings.items()
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

    def uses_organization_config(self, subject: str) -> bool:
        """Return whether organization service config is canonical for a subject."""
        group = self.bindings.get(_subject(subject))
        if group is not None:
            return any(binding.service_config is not None for binding in group)
        return self.has_subject(subject)

    def has_runtime_config(self, subject: str) -> bool:
        """Return whether a subject binds organization or explicit legacy config.

        Document-only packages do not own runtime settings and must leave the
        harness defaults and legacy settings available to existing tools.
        Removed pinned subjects remain fail-closed because their former config
        binding cannot be safely inferred from the current source tree.
        """
        group = self.bindings.get(_subject(subject))
        if group is not None:
            return any(
                binding.service_config is not None or binding.legacy_config
                for binding in group
            )
        return self.has_subject(subject)

    def uses_legacy_config(self, subject: str) -> bool:
        return any(
            binding.legacy_config
            for binding in self.bindings.get(_subject(subject), ())
        )

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
                source_path = _relative(entry["path"])
                if not source_path.endswith(".md") or source_path in {
                    item.get("source_path") for item in exported
                }:
                    raise ValueError("exports must be distinct Markdown documents")
                phases = entry.get("phases", ["*"])
                if (
                    not isinstance(phases, list)
                    or not phases
                    or any(not isinstance(p, str) or not p for p in phases)
                    or not isinstance(entry.get("entrypoint", False), bool)
                ):
                    raise ValueError("invalid document applicability")
                content = self._source_document(binding.root, source_path)
                if b"\x00" in content:
                    raise ValueError("document contains binary data")
                if any(
                    other.service_config is not None
                    and other.service_config.exists()
                    and (binding.root / source_path).samefile(other.service_config)
                    for group in self.bindings.values()
                    for other in group
                ):
                    raise ValueError("service configuration is exported as a document")
                relative = f"sources/{binding.source_id}/{source_path}"
                documents[relative] = content.decode("utf-8")
                total += len(content)
                if total > MAX_PACKAGE_BYTES:
                    raise ValueError("package exceeds limit")
                exported.append(
                    {
                        "path": relative,
                        "source_path": source_path,
                        "source_id": binding.source_id,
                        "source_revision": binding.source_revision or None,
                        "phases": phases,
                        "entrypoint": entry.get("entrypoint", False),
                        "size_bytes": len(content),
                        "digest": hashlib.sha256(content).hexdigest(),
                    }
                )
            skill = documents.get(f"sources/{binding.source_id}/SKILL.md", "")
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

    def _capture_source(self, binding: OrganizationBinding) -> dict[str, Any]:
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
            for entry in exported:
                assert binding.root is not None
                if (
                    self._source_document(binding.root, entry["source_path"])
                    != documents[entry["path"]].encode()
                ):
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
                "source_id": binding.source_id,
                "binding": binding.identity,
                "source_revision": binding.source_revision or None,
                "revision": revision,
                "name": name,
                "service_configured": binding.service_config is not None,
                "documents": exported,
                "content": documents,
                "runtime_config": runtime,
                "legacy_config": binding.legacy_config,
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

    def _capture(self, bindings: tuple[OrganizationBinding, ...]) -> dict[str, Any]:
        """Capture every configured source for one subject without choosing a winner."""
        bindings = tuple(sorted(bindings, key=lambda binding: binding.source_id))
        captured = [self._capture_source(binding) for binding in bindings]
        if not captured:
            raise SkillGatewayError("subject_unconfigured", "Subject is unconfigured")

        documents = [entry for item in captured for entry in item["documents"]]
        content = {
            path: text for item in captured for path, text in item["content"].items()
        }
        if (
            sum(len(value.encode("utf-8")) for value in content.values())
            > MAX_PACKAGE_BYTES
        ):
            raise SkillGatewayError(
                "source_too_large", "Combined subject documents exceed the limit"
            )
        by_logical_path: dict[str, list[dict[str, Any]]] = {}
        by_digest: dict[str, list[dict[str, Any]]] = {}
        for entry in documents:
            public = {
                "source_id": entry["source_id"],
                "path": entry["source_path"],
                "digest": entry["digest"],
            }
            by_logical_path.setdefault(entry["source_path"], []).append(public)
            by_digest.setdefault(entry["digest"], []).append(public)
        overlaps = [
            {
                "path": path,
                "same_content": len({entry["digest"] for entry in entries}) == 1,
                "documents": entries,
            }
            for path, entries in sorted(by_logical_path.items())
            if len({entry["source_id"] for entry in entries}) > 1
        ]
        duplicates = [
            {"digest": digest, "documents": entries}
            for digest, entries in sorted(by_digest.items())
            if len({entry["source_id"] for entry in entries}) > 1
        ]
        runtime_sources = [
            {
                "source_id": item["source_id"],
                "value": item["runtime_config"],
            }
            for item in captured
            if item["service_configured"]
        ]
        runtime_values = [item["value"] for item in runtime_sources]
        legacy_sources = [
            item["source_id"] for item in captured if item["legacy_config"]
        ]
        has_runtime_conflict = len(runtime_values) > 1 and any(
            value != runtime_values[0] for value in runtime_values[1:]
        )
        has_legacy_conflict = bool(legacy_sources and runtime_sources)
        source_records = [
            {
                "id": item["source_id"],
                "revision": item["source_revision"],
                "package_revision": item["revision"],
                "service_configured": item["service_configured"],
                "legacy_config": item["legacy_config"],
            }
            for item in captured
        ]
        revision = _digest(
            [
                [item["source_id"], item["binding"], item["revision"]]
                for item in captured
            ]
        )
        names = sorted({item["name"] for item in captured if item["name"]})
        runtime_config = (
            runtime_values[0]
            if runtime_values and not has_runtime_conflict and not has_legacy_conflict
            else {}
        )
        return {
            "schema_version": 1,
            "subject": captured[0]["subject"],
            "binding": _digest([item["binding"] for item in captured]),
            "revision": revision,
            "sources": source_records,
            "name": names[0] if len(names) == 1 else None,
            "service_configured": bool(runtime_sources),
            "runtime_config": runtime_config,
            "runtime_config_sources": [item["source_id"] for item in runtime_sources],
            "runtime_config_conflict": has_runtime_conflict or has_legacy_conflict,
            "legacy_config": bool(legacy_sources) and not runtime_sources,
            "documents": documents,
            "content": content,
            "overlaps": overlaps,
            "duplicates": duplicates,
            "required": any(binding.required for binding in bindings),
        }

    def _snapshot(self, subject: str) -> dict[str, Any] | None:
        bindings = self.bindings.get(_subject(subject))
        if bindings is None:
            if self.has_subject(subject):
                raise SkillGatewayError(
                    "organization_subject_removed",
                    "Pinned organization subject was removed",
                )
            return None
        if not self.ticket_id:
            return self._capture(bindings)
        if not self.attempt_id:
            raise SkillGatewayError("missing_attempt", "Trusted attempt is required")
        key = _digest([self.ticket_id, self.attempt_id, subject])
        binding_identity = _digest([binding.identity for binding in bindings])
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
                    if snapshot["binding"] != binding_identity:
                        raise ValueError("administrator source binding changed")
                    return snapshot
                except (OSError, ValueError, KeyError, TypeError, RecursionError):
                    raise SkillGatewayError(
                        "invalid_snapshot", "Pinned skill snapshot is unavailable"
                    ) from None
            snapshot = self._capture(bindings)
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
        bindings = self.bindings.get(_subject(subject))
        if bindings is None:
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
                "required": any(binding.required for binding in bindings),
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
            "required": snapshot["required"],
            "revision": snapshot["revision"],
            "name": snapshot["name"],
            "service_configured": snapshot.get("service_configured", False),
            "sources": snapshot["sources"],
            "overlaps": snapshot["overlaps"],
            "duplicates": snapshot["duplicates"],
            "runtime_config_conflict": snapshot["runtime_config_conflict"],
            "runtime_config_sources": snapshot["runtime_config_sources"],
            "documents_available": bool(documents),
            "phase": self.phase,
            "documents": documents,
            "entrypoints": [d["ref"] for d in documents if d["entrypoint"]],
        }

    def get_runtime_config(self, subject: str) -> dict[str, Any] | None:
        """Service API only; never register this method as a model resource."""
        bindings = self.bindings.get(_subject(subject))
        if bindings is None:
            if self.has_subject(subject):
                raise SkillGatewayError(
                    "organization_subject_removed",
                    "Pinned organization subject was removed",
                )
            return None
        snapshot = self._snapshot(subject)
        assert snapshot is not None
        if snapshot["runtime_config_conflict"]:
            source_ids = ", ".join(snapshot["runtime_config_sources"])
            raise SkillGatewayError(
                "organization_config_conflict",
                "Multiple organization runtime configurations conflict for this subject"
                + (f" (sources: {source_ids})" if source_ids else ""),
            )
        if snapshot["legacy_config"]:
            return None
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
            if not relative.startswith(f"sources/{origin_entry['source_id']}/"):
                raise SkillGatewayError(
                    "invalid_ref", "Relative document pointer crossed source boundary"
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
            "source_id": next(
                (
                    entry["source_id"]
                    for entry in snapshot["documents"]
                    if entry["path"] == relative
                ),
                None,
            ),
            "path": next(
                (
                    entry["source_path"]
                    for entry in snapshot["documents"]
                    if entry["path"] == relative
                ),
                relative,
            ),
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
        source_filter = None
        if ref:
            snapshot, relative = self._document(subject, ref, from_ref)
            paths = [relative]
        else:
            if from_ref:
                self._document(subject, from_ref, None)
                _, _, origin_path = parse_organization_ref(from_ref)
                origin_entry = next(
                    (d for d in snapshot["documents"] if d["path"] == origin_path),
                    None,
                )
                if origin_entry is None:
                    raise SkillGatewayError("invalid_origin", "Unknown search origin")
                source_filter = origin_entry["source_id"]
            paths = [
                d["path"]
                for d in snapshot["documents"]
                if self._visible(d)
                and (source_filter is None or d["source_id"] == source_filter)
            ]
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
                        "source_id": next(
                            (
                                item["source_id"]
                                for item in snapshot["documents"]
                                if item["path"] == path
                            ),
                            None,
                        ),
                        "path": next(
                            (
                                item["source_path"]
                                for item in snapshot["documents"]
                                if item["path"] == path
                            ),
                            path,
                        ),
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
