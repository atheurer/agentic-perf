from __future__ import annotations

import json
from typing import Any

import pytest

from agents.server_utils import _configured_crucible_source_repo, build_skill_provider


class _PrivateSkillProviderStub:
    def __init__(self, resolver: Any | None = None) -> None:
        self.resolver = resolver

    def bind_current_attempt(self, phase: str) -> None:
        self.phase = phase

    def _load_config(self, suite_name: str) -> dict[str, Any]:
        return {}


def test_configured_crucible_source_repo_reads_instance_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": "/configured/crucible"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    assert _configured_crucible_source_repo() == "/configured/crucible"


def test_crucible_source_repo_environment_overrides_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": "/configured/crucible"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CRUCIBLE_SOURCE_REPO", "/environment/crucible")
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    assert _configured_crucible_source_repo() == "/environment/crucible"


def test_invalid_crucible_source_repo_config_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{invalid", encoding="utf-8")
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    assert _configured_crucible_source_repo() is None


def test_build_skill_provider_passes_configured_crucible_source_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": "/configured/crucible"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setenv("TICKET_ID", "PERF-CATALOG-EPOCH")
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)
    monkeypatch.setattr(
        "providers.skills.private.PrivateSkillProvider", _PrivateSkillProviderStub
    )

    captured: dict[str, Any] = {}

    def fake_gateway(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "agents.server_utils.build_crucible_context_gateway", fake_gateway
    )

    build_skill_provider(catalog_only=True)

    assert captured["source_repo"] == "/configured/crucible"
    assert "source_epoch" not in captured


def test_explicit_crucible_source_repo_overrides_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": "/configured/crucible"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)
    monkeypatch.setattr(
        "providers.skills.private.PrivateSkillProvider", _PrivateSkillProviderStub
    )

    captured: dict[str, Any] = {}

    def fake_gateway(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "agents.server_utils.build_crucible_context_gateway", fake_gateway
    )

    build_skill_provider(catalog_only=True, source_repo="/explicit/crucible")

    assert captured["source_repo"] == "/explicit/crucible"
