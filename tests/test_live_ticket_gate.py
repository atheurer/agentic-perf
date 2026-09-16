from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "live-ticket-gate.py"
SPEC = importlib.util.spec_from_file_location("live_ticket_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def config() -> object:
    return gate.GateConfig(
        controller="controller.invalid",
        client=gate.ClientTarget(1, "client-one.invalid"),
        ssh_user="root",
        ssh_key_path="/private/key",
        seconds=5,
    )


def test_config_uses_runtime_ssh_settings() -> None:
    result = gate.GateConfig.from_runtime(
        "controller.invalid",
        "client-one.invalid",
        {"ssh_user": "admin", "ssh_key": "/private/key"},
    )
    assert result.client == gate.ClientTarget(1, "client-one.invalid")
    assert result.ssh_user == "admin"
    assert result.ssh_key_path == "/private/key"


def test_config_rejects_hostnames_that_could_be_ssh_options() -> None:
    with pytest.raises(gate.GateError, match="unsupported characters"):
        gate.GateConfig.from_runtime(
            "-oProxyCommand=bad",
            "client-one.invalid",
            {"ssh_key": "/private/key"},
        )


def test_ticket_direction_forbids_host_mounts_entirely() -> None:
    description = gate._description(config())
    assert "Never include a `host-mounts` key" in description
    assert "including an empty" in description
    assert "absence of the key is the only allowed representation" in description
    assert "Do not mount `/proc`, `/sys`" in description


def test_completed_ticket_requires_full_lifecycle_and_agents() -> None:
    ticket = {
        "status_trail": [
            "new",
            "triage_pending",
            "awaiting_hardware",
            "preparing_platform",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_customer_guidance",
            "executing_benchmark",
            "awaiting_review",
            "awaiting_teardown",
            "retrospective_pending",
            "closed",
        ],
        "custom_fields": {
            "benchmark_status": "completed",
            "run_id": "00000000-0000-0000-0000-000000000001",
            "execution_plan": {
                "steps": [
                    {"agent_type": agent, "status": "completed"}
                    for agent in (
                        "resource",
                        "provision",
                        "benchmark",
                        "review",
                        "teardown",
                    )
                ]
            },
        },
    }
    run_id, trail = gate._validate_completed_ticket(ticket)
    assert run_id.endswith("1")
    assert trail[-1] == "closed"


def test_completed_ticket_rejects_missing_review() -> None:
    ticket = {
        "status_trail": [
            "new",
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_customer_guidance",
            "awaiting_teardown",
            "retrospective_pending",
            "closed",
        ],
        "custom_fields": {"benchmark_status": "completed", "run_id": "run"},
    }
    with pytest.raises(gate.GateError, match="required lifecycle"):
        gate._validate_completed_ticket(ticket)


def safe_run_file() -> dict:
    return {
        "benchmarks": [
            {
                "name": "sleep",
                "ids": "1",
                "mv-params": {
                    "global-options": [
                        {
                            "name": "global",
                            "params": [
                                {"arg": "seconds", "vals": ["5"], "role": "client"}
                            ],
                        }
                    ],
                    "sets": [{"include": "global", "params": []}],
                },
            }
        ],
        "endpoints": [
            {
                "type": "remotehosts",
                "remotes": [
                    {
                        "engines": [{"role": "client", "ids": "1"}],
                        "config": {"host": "client-one.invalid"},
                    },
                ],
            }
        ],
        "run-params": {"num-samples": 1},
        "tool-params": [],
    }


def test_policy_accepts_exact_one_client_sleep_run() -> None:
    gate.validate_run_file(safe_run_file(), config())


@pytest.mark.parametrize("location", ["endpoint", "remote"])
def test_policy_accepts_explicit_disable_tools_true(location: str) -> None:
    run_file = safe_run_file()
    if location == "endpoint":
        run_file["endpoints"][0]["settings"] = {"disable-tools": True}
    else:
        remote_config = run_file["endpoints"][0]["remotes"][0]["config"]
        remote_config["settings"] = {"disable-tools": True}
    gate.validate_run_file(run_file, config())


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_policy_rejects_disable_tools_unless_boolean_true(value: object) -> None:
    run_file = safe_run_file()
    run_file["endpoints"][0]["settings"] = {"disable-tools": value}
    with pytest.raises(gate.GateError, match="must be boolean true"):
        gate.validate_run_file(run_file, config())


@pytest.mark.parametrize("location", ["endpoint", "remote"])
def test_policy_accepts_explicit_cpu_partitioning_false(location: str) -> None:
    run_file = safe_run_file()
    if location == "endpoint":
        run_file["endpoints"][0]["settings"] = {"cpu-partitioning": False}
    else:
        remote_config = run_file["endpoints"][0]["remotes"][0]["config"]
        remote_config["settings"] = {"cpu-partitioning": False}
    gate.validate_run_file(run_file, config())


@pytest.mark.parametrize("value", [True, "false", 0, None])
def test_policy_rejects_cpu_partitioning_unless_boolean_false(value: object) -> None:
    run_file = safe_run_file()
    run_file["endpoints"][0]["settings"] = {"cpu-partitioning": value}
    with pytest.raises(gate.GateError, match="must be boolean false"):
        gate.validate_run_file(run_file, config())


def test_log_scan_ignores_recoverable_ssh_context_retry() -> None:
    log = """Error calling tool 'verify_ssh_path'
Traceback (most recent call last):
RuntimeError: SSH context not set. Call set_ssh_context() first.
Traceback (most recent call last):
MCPToolCallError: Error calling tool 'verify_ssh_path': SSH context not set. Call set_ssh_context() first.
intentional_agent_retry"""
    assert gate._fatal_log_signatures(log) == []


def test_log_scan_keeps_unrelated_traceback_fatal() -> None:
    assert gate._fatal_log_signatures("Traceback (most recent call last): boom") == [
        "Traceback (most recent call last):"
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda run: run["benchmarks"][0].update(name="uperf"), "benchmark"),
        (lambda run: run["benchmarks"][0].update(ids="2"), "exactly ID 1"),
        (
            lambda run: run["endpoints"][0]["remotes"][0]["config"].update(
                host="other.invalid"
            ),
            "unapproved remote",
        ),
        (
            lambda run: run["endpoints"][0]["remotes"][0]["engines"][0].update(ids="2"),
            "wrong client ID",
        ),
        (
            lambda run: run["tool-params"].append(
                {"tool": "sysstat", "enabled": "yes"}
            ),
            "explicitly disabled",
        ),
        (
            lambda run: run["endpoints"][0].update(settings={"host-mount": "/danger"}),
            "unapproved endpoint settings",
        ),
    ],
)
def test_policy_rejects_unsafe_variants(mutation, message: str) -> None:
    run_file = safe_run_file()
    mutation(run_file)
    with pytest.raises(gate.GateError, match=message):
        gate.validate_run_file(run_file, config())
