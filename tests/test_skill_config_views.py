from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.skill_gateway import (
    organization_manages_harness,
    skill_config_view,
    skill_context_gateway,
)
from providers.skills.config_views import project_config_view
from providers.skills.gateway import OrganizationSkillResolver, SkillGatewayError
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider
from tests.conftest import MockSkillProvider


class _ConfigProvider:
    def __init__(self, config: dict):
        self.config = config
        self.organization_resolver = OrganizationSkillResolver.from_instance_config({})
        self.project_context_source = None

    async def get_all_private_config(self, _harness: str) -> dict:
        return self.config


def _write_docs_only_crucible(repository: Path) -> None:
    package = repository / "skills" / "harness" / "crucible"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: crucible\ndescription: Test\nmetadata:\n"
        "  subject: harness/crucible\n---\n\nGuidance.\n"
    )
    (package / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subject": "harness/crucible",
                "documents": [{"path": "SKILL.md", "entrypoint": True}],
            }
        )
    )


def _ticket_resolver(
    repository: Path, snapshot_root: Path, *, ticket_id: str, attempt_id: str
) -> OrganizationSkillResolver:
    resolver = OrganizationSkillResolver.from_instance_config(
        {
            "skill_gateway": {
                "organization": {"source": {"kind": "path", "path": str(repository)}}
            }
        },
        snapshot_root=snapshot_root,
    ).for_attempt(ticket_id, attempt_id, "benchmark")
    resolver.audit_emit = lambda _event: None
    return resolver


def _aggregate_provider(
    resolver: OrganizationSkillResolver,
    legacy_root: Path,
    defaults: dict,
) -> MultiHarnessSkillProvider:
    class DefaultsProvider(MockSkillProvider):
        async def get_default_config(self) -> dict:
            return defaults

    return MultiHarnessSkillProvider(
        {"crucible": DefaultsProvider()},
        PrivateSkillProvider(legacy_root, resolver=resolver),
    )


def _config() -> dict:
    return {
        "constraints": {
            "supported_os": ["rhel9", "rhel9"],
            "controller_os_must_match": True,
        },
        "provisioning": {
            "method": "public_install",
            "install_method": "public_install",
            "on_existing_install": "skip",
            "install_target_path": "/opt/crucible",
            "options_on_existing": [
                {"action": "skip", "command": "never expose"},
                {"action": "update", "secret": "never expose"},
            ],
            "install_command": "private command",
        },
        "platform_contract": {
            "supported_os": ["rhel9"],
            "required_packages": ["podman", "git", "podman"],
            "verify_command": "private command",
        },
        "execution": {
            "controller_required": True,
            "endpoint_type": "remotehosts",
            "endpoint_user": "root",
            "default_osruntime": "podman",
            "default_userenv": "fedora42",
            "run_file_format": "json",
            "run_file_location": "/var/lib/crucible/run.json",
            "results_dir_pattern": "/var/lib/crucible/run/*",
            "run_command": "private command",
            "secret_token": "never expose",
            "kube": {
                "min_root_volume_gb": 32,
                "self_ssh_required": False,
                "selinux": "enforcing",
                "tool_params_required": True,
                "secret": "never expose",
            },
        },
        "review": {
            "method": "cdm",
            "results_method": "workspace",
            "cdm_port": 8080,
            "result_summary_path": "/var/lib/crucible/summary.json",
            "result_summary_file": "summary.json",
            "read_command": "private command",
        },
        "firewall": {"disable": False, "disable_firewall": True, "policy": "managed"},
        "secrets": {"client_server_auth": "never expose"},
    }


def test_registered_config_views_normalize_and_drop_non_view_values() -> None:
    config = _config()

    provisioning = project_config_view(
        "crucible", "provisioning", "provisioning", config
    )
    execution = project_config_view("crucible", "execution", "benchmark", config)
    review = project_config_view("crucible", "review", "review", config)
    platform = project_config_view(
        "crucible", "platform_contract", "provisioning", config
    )

    assert provisioning == {
        "method": "public_install",
        "install_method": "public_install",
        "on_existing_install": "skip",
        "install_target_path": "/opt/crucible",
        "options_on_existing": [{"action": "skip"}, {"action": "update"}],
    }
    assert execution == {
        "controller_required": True,
        "endpoint_type": "remotehosts",
        "endpoint_user": "root",
        "default_osruntime": "podman",
        "default_userenv": "fedora42",
        "run_file_format": "json",
        "run_file_location": "/var/lib/crucible/run.json",
        "results_dir_pattern": "/var/lib/crucible/run/*",
        "kube": {
            "min_root_volume_gb": 32,
            "self_ssh_required": False,
            "selinux": "enforcing",
            "tool_params_required": True,
        },
    }
    assert review == {
        "method": "cdm",
        "results_method": "workspace",
        "cdm_port": 8080,
        "result_summary_path": "/var/lib/crucible/summary.json",
        "result_summary_file": "summary.json",
    }
    assert platform == {
        "supported_os": ["rhel9"],
        "required_packages": ["podman", "git"],
    }
    serialized = json.dumps([provisioning, execution, review, platform])
    assert "private command" not in serialized
    assert "never expose" not in serialized


def test_safe_projection_accepts_ordinary_authselect_values() -> None:
    projected = project_config_view(
        "crucible",
        "execution",
        "benchmark",
        {
            "execution": {
                "endpoint_user": "authselect",
                "default_userenv": "quay.io/authselect/client-server:latest",
            }
        },
    )

    assert projected == {
        "endpoint_user": "authselect",
        "default_userenv": "quay.io/authselect/client-server:latest",
    }


@pytest.mark.parametrize(
    ("view", "phase", "config", "field"),
    [
        (
            "execution",
            "benchmark",
            {"execution": {"controller_required": "yes"}},
            "controller_required",
        ),
        (
            "execution",
            "benchmark",
            {"execution": {"run_file_location": "/root/secret-token"}},
            "run_file_location",
        ),
        ("review", "review", {"review": {"cdm_port": 70000}}, "cdm_port"),
        (
            "provisioning",
            "provisioning",
            {"provisioning": {"on_existing_install": "run rm"}},
            "on_existing_install",
        ),
    ],
)
def test_invalid_view_values_fail_closed(
    view: str, phase: str, config: dict, field: str
) -> None:
    with pytest.raises(SkillGatewayError, match=f"{field} cannot be safely projected"):
        project_config_view("crucible", view, phase, config)


def test_document_discovery_does_not_register_or_select_runtime_config(
    tmp_path: Path,
) -> None:
    package = tmp_path / "org" / "skills" / "harness" / "crucible"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: crucible\ndescription: Test\nmetadata:\n  subject: harness/crucible\n---\n\nGuidance.\n"
    )
    (package / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subject": "harness/crucible",
                "documents": [{"path": "SKILL.md", "entrypoint": True}],
            }
        )
    )
    resolver = OrganizationSkillResolver.from_instance_config(
        {
            "skill_gateway": {
                "organization": {
                    "source": {"kind": "path", "path": str(tmp_path / "org")}
                }
            }
        }
    )
    provider = _ConfigProvider({"execution": {"endpoint_type": "remotehosts"}})
    provider.organization_resolver = resolver

    assert resolver.has_subject("harness/crucible")
    assert not resolver.uses_organization_config("harness/crucible")
    assert organization_manages_harness(provider, "crucible")
    assert asyncio.run(
        skill_config_view(provider, "crucible", "execution", "benchmark")
    ) == {"endpoint_type": "remotehosts"}


def test_pinned_canonical_config_stays_selected_after_file_disappears(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_docs_only_crucible(repository)
    service_config = repository / "service-config" / "harness" / "crucible.json"
    service_config.parent.mkdir(parents=True)
    canonical = {"execution": {"endpoint_type": "kube"}}
    service_config.write_text(json.dumps(canonical))
    snapshot_root = tmp_path / "snapshots"
    defaults = {"execution": {"endpoint_type": "remotehosts"}}

    first = _aggregate_provider(
        _ticket_resolver(
            repository,
            snapshot_root,
            ticket_id="PERF-PIN-CANONICAL",
            attempt_id="attempt-1",
        ),
        tmp_path / "legacy",
        defaults,
    )
    assert asyncio.run(first.get_all_private_config("crucible")) == canonical
    service_config.unlink()

    rediscovered_resolver = _ticket_resolver(
        repository,
        snapshot_root,
        ticket_id="PERF-PIN-CANONICAL",
        attempt_id="attempt-1",
    )
    rediscovered = _aggregate_provider(
        rediscovered_resolver, tmp_path / "legacy", defaults
    )

    assert not rediscovered_resolver.uses_legacy_config("harness/crucible")
    assert rediscovered_resolver.uses_organization_config("harness/crucible")
    assert asyncio.run(rediscovered.get_all_private_config("crucible")) == canonical
    assert asyncio.run(
        skill_config_view(rediscovered, "crucible", "execution", "benchmark")
    ) == {"endpoint_type": "kube"}


def test_docs_only_pin_does_not_switch_to_later_config_file(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_docs_only_crucible(repository)
    snapshot_root = tmp_path / "snapshots"
    defaults = {"execution": {"endpoint_type": "remotehosts"}}
    initial_resolver = _ticket_resolver(
        repository,
        snapshot_root,
        ticket_id="PERF-PIN-DOCS-ONLY",
        attempt_id="attempt-1",
    )
    assert initial_resolver.bootstrap("harness/crucible")["status"] == "available"
    initial = _aggregate_provider(initial_resolver, tmp_path / "legacy", defaults)
    assert not initial_resolver.uses_organization_config("harness/crucible")
    assert asyncio.run(initial.get_all_private_config("crucible")) == defaults

    service_config = repository / "service-config" / "harness" / "crucible.json"
    service_config.parent.mkdir(parents=True)
    service_config.write_text(json.dumps({"execution": {"endpoint_type": "kube"}}))
    rediscovered_resolver = _ticket_resolver(
        repository,
        snapshot_root,
        ticket_id="PERF-PIN-DOCS-ONLY",
        attempt_id="attempt-1",
    )
    rediscovered = _aggregate_provider(
        rediscovered_resolver, tmp_path / "legacy", defaults
    )

    assert not rediscovered_resolver.uses_organization_config("harness/crucible")
    assert rediscovered_resolver.get_runtime_config("harness/crucible") is None
    assert asyncio.run(rediscovered.get_all_private_config("crucible")) == defaults


@pytest.mark.asyncio
async def test_gateway_and_compatibility_tools_share_registered_projection(
    monkeypatch,
) -> None:
    import agents.benchmark.server as benchmark_server
    import agents.provisioning.server as provisioning_server
    import agents.review.server as review_server

    config = _config()
    provider = _ConfigProvider(config)
    monkeypatch.setattr(benchmark_server, "_skill_provider", provider)
    monkeypatch.setattr(benchmark_server, "_initialized", True)
    monkeypatch.setattr(provisioning_server, "_skill_provider", provider)
    monkeypatch.setattr(provisioning_server, "_initialized", True)
    monkeypatch.setattr(review_server, "_skill_provider", provider)
    monkeypatch.setattr(review_server, "_initialized", True)

    async def controller_context_gateway(**_kwargs) -> str:
        return json.dumps({"found": False, "documents": []})

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway", controller_context_gateway
    )
    bootstrap = json.loads(
        await skill_context_gateway(
            provider,
            ticket_id="PERF-CONFIG-VIEW",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/crucible",
        )
    )
    execution_ref = next(
        view["ref"]
        for view in bootstrap["configuration_views"]
        if view["name"] == "execution"
    )
    gateway_execution = json.loads(
        await skill_context_gateway(
            provider,
            ticket_id="PERF-CONFIG-VIEW",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/crucible",
            operation="read",
            ref=execution_ref,
        )
    )["document"]["content"]

    benchmark = json.loads(await benchmark_server.get_execution_config("crucible"))
    provisioning = json.loads(
        await provisioning_server.get_private_config("crucible", "provisioning")
    )
    review = json.loads(await review_server.get_review_config("crucible"))

    assert json.loads(gateway_execution) == project_config_view(
        "crucible", "execution", "benchmark", config
    )
    assert {
        key: value
        for key, value in benchmark.items()
        if key not in {"found", "harness"}
    } == project_config_view("crucible", "execution", "benchmark", config)
    assert provisioning["value"] == project_config_view(
        "crucible", "provisioning", "provisioning", config
    )
    assert review["review_config"] == project_config_view(
        "crucible", "review", "review", config
    )


def _resolver_for_docs_only_zathras(tmp_path: Path, *, legacy: bool):
    repository = tmp_path / "org"
    package = repository / "skills" / "harness" / "zathras"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: zathras\ndescription: Test\nmetadata:\n"
        "  subject: harness/zathras\n---\n\nGuidance.\n"
    )
    (package / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subject": "harness/zathras",
                "documents": [{"path": "SKILL.md", "entrypoint": True}],
            }
        )
    )
    organization = {
        "source": {"kind": "path", "path": str(repository)},
    }
    if legacy:
        organization["subjects"] = {"harness/zathras": {"legacy_config": True}}
    return OrganizationSkillResolver.from_instance_config(
        {"skill_gateway": {"organization": organization}}
    )


@pytest.mark.parametrize("legacy", [False, True], ids=["docs-only", "legacy-opt-in"])
@pytest.mark.asyncio
async def test_non_crucible_unregistered_views_never_return_raw_config(
    monkeypatch, tmp_path: Path, legacy: bool
) -> None:
    import agents.benchmark.server as benchmark_server
    import agents.provisioning.server as provisioning_server
    import agents.review.server as review_server

    provider = _ConfigProvider(
        {
            "execution": {"run_command": "COMMAND_SENTINEL"},
            "provisioning": {"install_command": "COMMAND_SENTINEL"},
            "review": {"read_command": "COMMAND_SENTINEL"},
            "secrets": {"api_token": "SECRET_SENTINEL"},
        }
    )
    provider.organization_resolver = _resolver_for_docs_only_zathras(
        tmp_path, legacy=legacy
    )
    assert provider.organization_resolver.has_subject("harness/zathras")
    assert organization_manages_harness(provider, "zathras")

    monkeypatch.setattr(benchmark_server, "_skill_provider", provider)
    monkeypatch.setattr(benchmark_server, "_initialized", True)
    monkeypatch.setattr(provisioning_server, "_skill_provider", provider)
    monkeypatch.setattr(provisioning_server, "_initialized", True)
    monkeypatch.setattr(review_server, "_skill_provider", provider)
    monkeypatch.setattr(review_server, "_initialized", True)

    outputs = [
        json.loads(await benchmark_server.get_execution_config("zathras")),
        json.loads(
            await provisioning_server.get_private_config("zathras", "provisioning")
        ),
        json.loads(await provisioning_server.get_private_config("zathras", "secrets")),
        json.loads(await review_server.get_review_config("zathras")),
    ]
    serialized = json.dumps(outputs)

    assert "COMMAND_SENTINEL" not in serialized
    assert "SECRET_SENTINEL" not in serialized
    assert outputs[0]["reason"] == "configuration_view_unavailable"
    assert outputs[1]["reason"] == "configuration_view_unavailable"
    assert outputs[2]["reason"] == "configuration_view_unavailable"
    assert outputs[3]["reason"] == "configuration_view_unavailable"
