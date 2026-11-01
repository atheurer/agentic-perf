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


def _write_crucible_source(path: Any, catalog: dict[str, Any] | None = None) -> str:
    path.mkdir(parents=True)
    (path / "config").mkdir()
    (path / "config" / "repos.json").write_text(
        json.dumps(catalog if catalog is not None else {}), encoding="utf-8"
    )
    return str(path)


def test_configured_crucible_source_repo_reads_instance_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    source_repo = _write_crucible_source(tmp_path / "configured")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": source_repo}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    assert _configured_crucible_source_repo() == source_repo


def test_crucible_source_repo_environment_overrides_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    configured = _write_crucible_source(tmp_path / "configured")
    environment = _write_crucible_source(tmp_path / "environment")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": configured}), encoding="utf-8"
    )
    monkeypatch.setenv("CRUCIBLE_SOURCE_REPO", environment)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    assert _configured_crucible_source_repo() == environment


def test_invalid_crucible_source_repo_catalog_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    source_repo = tmp_path / "invalid"
    source_repo.mkdir()
    (source_repo / "config").mkdir()
    (source_repo / "config" / "repos.json").write_text("{invalid", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": str(source_repo)}), encoding="utf-8"
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="no valid config/repos.json"):
        _configured_crucible_source_repo()


def test_build_skill_provider_passes_configured_crucible_source_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    source_repo = _write_crucible_source(tmp_path / "configured")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": source_repo}),
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

    assert captured["source_repo"] == source_repo
    assert "source_epoch" not in captured


def test_explicit_crucible_source_repo_overrides_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    configured = _write_crucible_source(tmp_path / "configured")
    explicit = _write_crucible_source(tmp_path / "explicit")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": configured}),
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

    build_skill_provider(catalog_only=True, source_repo=explicit)

    assert captured["source_repo"] == explicit


def test_build_skill_provider_reads_configured_crucible_source_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {"harness_repos": {"crucible": "https://example.test/crucible.git"}}
        ),
        encoding="utf-8",
    )
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

    assert captured["source_url"] == "https://example.test/crucible.git"


def test_missing_configured_crucible_source_repo_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crucible_source_repo": str(tmp_path / "missing")}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CRUCIBLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="is not a directory"):
        build_skill_provider(catalog_only=True)


async def test_catalog_only_uses_local_repo_with_ticket_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from providers.skills.crucible import CrucibleCatalogFetcher

    source_repo = _write_crucible_source(
        tmp_path / "crucible",
        {
            "official": [
                {
                    "name": "local-only",
                    "type": "benchmark",
                    "repository": "https://github.com/example/bench-local-only.git",
                    "checkout": {"target": "main"},
                }
            ]
        },
    )
    benchmark = tmp_path / "crucible" / "subprojects" / "benchmarks" / "local-only"
    benchmark.mkdir(parents=True)
    (benchmark / "multiplex.json").write_text(
        json.dumps({"params": {"ifname": {"role": "all"}}}), encoding="utf-8"
    )
    (benchmark / "rickshaw.json").write_text(
        json.dumps({"client": {}, "server": {}}), encoding="utf-8"
    )

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("paths.CONFIG_PATH", config_path)
    monkeypatch.setattr("paths.SKILL_SOURCE_EPOCH_DIR", tmp_path / "epochs")
    monkeypatch.setenv("TICKET_ID", "PERF-LOCAL-CATALOG")
    monkeypatch.setattr(
        "providers.skills.private.PrivateSkillProvider", _PrivateSkillProviderStub
    )

    def fail_remote_fetch(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("catalog-only discovery must use the configured checkout")

    monkeypatch.setattr(CrucibleCatalogFetcher, "read_json", fail_remote_fetch)

    provider = build_skill_provider(
        crucible_home=str(tmp_path / "controller"),
        catalog_only=True,
        source_repo=source_repo,
    )
    benchmarks = await provider.get_provider("crucible").list_benchmarks()

    assert [benchmark.name for benchmark in benchmarks] == ["local-only"]
    assert benchmarks[0].supported_params == {"params": {"ifname": {"role": "all"}}}
    assert set(benchmarks[0].roles) == {"client", "server"}
