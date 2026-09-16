#!/usr/bin/env python3
"""Run one policy-guarded, full-agent Crucible sleep ticket."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class GateError(RuntimeError):
    """The live gate could not safely complete."""


@dataclass(frozen=True)
class ClientTarget:
    """One approved Crucible client engine and host."""

    engine_id: int
    host: str


@dataclass(frozen=True)
class GateConfig:
    """Private, host-specific live gate configuration."""

    controller: str
    client: ClientTarget
    ssh_user: str
    ssh_key_path: str
    seconds: int = 5
    samples: int = 1
    poll_seconds: float = 2.0
    timeout_seconds: int = 1800

    @classmethod
    def from_runtime(
        cls,
        controller: str,
        system_under_test: str,
        runtime: dict[str, Any],
        *,
        seconds: int = 5,
        timeout_seconds: int = 1800,
    ) -> GateConfig:
        if not controller.strip() or not system_under_test.strip():
            raise GateError("controller and system-under-test hostnames are required")
        if controller == system_under_test:
            raise GateError("controller and system under test must be different hosts")
        hostname = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
        if not hostname.fullmatch(controller) or not hostname.fullmatch(
            system_under_test
        ):
            raise GateError("hostnames contain unsupported characters")
        ssh_key = runtime.get("ssh_key_path") or runtime.get("ssh_key")
        if not ssh_key:
            raise GateError("the instance config does not define an SSH key path")
        config = cls(
            controller=controller,
            client=ClientTarget(engine_id=1, host=system_under_test),
            ssh_user=str(runtime.get("ssh_user", "root")),
            ssh_key_path=str(ssh_key),
            seconds=seconds,
            timeout_seconds=timeout_seconds,
        )
        if not 1 <= config.seconds <= 30:
            raise GateError("seconds must be between 1 and 30")
        if config.samples != 1:
            raise GateError("the live gate requires exactly one sample")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", config.ssh_user):
            raise GateError("SSH user contains unsupported characters")
        return config


def _ids(value: Any) -> set[int]:
    if isinstance(value, int):
        return {value}
    if isinstance(value, list):
        return {int(item) for item in value}
    if isinstance(value, str):
        result: set[int] = set()
        for part in value.split(","):
            part = part.strip()
            if re.fullmatch(r"[0-9]+-[0-9]+", part):
                start, end = (int(item) for item in part.split("-", 1))
                result.update(range(start, end + 1))
            elif re.fullmatch(r"[0-9]+", part):
                result.add(int(part))
            else:
                raise GateError(f"unsupported engine ID expression: {value!r}")
        return result
    raise GateError(f"unsupported engine ID value: {value!r}")


def _benchmark_params(benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    mv_params = benchmark.get("mv-params", {})
    params: list[dict[str, Any]] = []
    for option in mv_params.get("global-options", []):
        params.extend(option.get("params", []))
    for item in mv_params.get("sets", []):
        params.extend(item.get("params", []))
    return params


def _validate_endpoint_settings(settings: dict[str, Any], location: str) -> None:
    allowed_settings = {
        "user",
        "userenv",
        "osruntime",
        "disable-tools",
        "cpu-partitioning",
    }
    unknown_settings = set(settings) - allowed_settings
    if unknown_settings:
        raise GateError(f"unapproved {location} settings: {sorted(unknown_settings)}")
    if settings.get("osruntime") not in (None, "podman", "chroot"):
        raise GateError(f"{location} osruntime must be podman or chroot")
    if "disable-tools" in settings and settings["disable-tools"] is not True:
        raise GateError(f"{location} disable-tools must be boolean true")
    if "cpu-partitioning" in settings and settings["cpu-partitioning"] is not False:
        raise GateError(f"{location} cpu-partitioning must be boolean false")


def validate_run_file(run_file: dict[str, Any], config: GateConfig) -> None:
    """Fail closed unless *run_file* is the approved one-client sleep run."""

    benchmarks = run_file.get("benchmarks")
    if not isinstance(benchmarks, list) or len(benchmarks) != 1:
        raise GateError("run file must contain exactly one benchmark")
    benchmark = benchmarks[0]
    if benchmark.get("name") != "sleep":
        raise GateError("benchmark must be exactly 'sleep'")
    if _ids(benchmark.get("ids")) != {1}:
        raise GateError("sleep benchmark must use exactly ID 1")

    params = _benchmark_params(benchmark)
    if not params:
        raise GateError("sleep benchmark must set seconds explicitly")
    for param in params:
        if param.get("arg") != "seconds":
            raise GateError(f"unapproved benchmark parameter: {param.get('arg')!r}")
        if param.get("role") not in (None, "client"):
            raise GateError("seconds may apply only to the client role")
        vals = param.get("vals")
        if not isinstance(vals, list) or {int(value) for value in vals} != {
            config.seconds
        }:
            raise GateError("sleep duration differs from the approved value")

    endpoint_groups = run_file.get("endpoints")
    if not isinstance(endpoint_groups, list) or len(endpoint_groups) != 1:
        raise GateError("run file must contain one remotehosts endpoint group")
    group = endpoint_groups[0]
    if group.get("type") != "remotehosts":
        raise GateError("endpoint type must be remotehosts")
    for settings in (group.get("settings", {}),):
        _validate_endpoint_settings(settings, "endpoint")
    remotes = group.get("remotes")
    if not isinstance(remotes, list) or len(remotes) != 1:
        raise GateError("run file must contain exactly one remote host")

    expected = {config.client.host: config.client.engine_id}
    seen: dict[str, int] = {}
    for remote in remotes:
        remote_config = remote.get("config", {})
        host = remote_config.get("host")
        if host not in expected:
            raise GateError(f"unapproved remote host: {host!r}")
        settings = remote_config.get("settings", {})
        _validate_endpoint_settings(settings, "remote")
        engines = remote.get("engines")
        if not isinstance(engines, list) or len(engines) != 1:
            raise GateError("each host must have exactly one engine declaration")
        engine = engines[0]
        if engine.get("role") != "client":
            raise GateError("only client engines are approved")
        engine_ids = _ids(engine.get("ids"))
        if engine_ids != {expected[host]}:
            raise GateError(f"host {host!r} has the wrong client ID")
        seen[host] = next(iter(engine_ids))
    if seen != expected:
        raise GateError("the approved host-to-ID mapping is incomplete")

    tools = run_file.get("tool-params", [])
    if not isinstance(tools, list):
        raise GateError("tool-params must be a list")
    for tool in tools:
        if str(tool.get("enabled", "yes")).lower() not in {"no", "false", "0"}:
            raise GateError(f"tool must be explicitly disabled: {tool.get('tool')!r}")

    run_params = run_file.get("run-params", {})
    if int(run_params.get("num-samples", 1)) != config.samples:
        raise GateError("run file must use exactly one sample")
    unknown_run_params = set(run_params) - {
        "num-samples",
        "max-sample-failures",
        "test-order",
    }
    if unknown_run_params:
        raise GateError(f"unapproved run parameters: {sorted(unknown_run_params)}")


def _extract_run_file(ticket: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    fields = ticket.get("custom_fields", {})
    for key in ("validated_run_file", "benchmark_validation"):
        value = fields.get(key)
        if isinstance(value, dict):
            run_file = value.get("run_file", value)
            if isinstance(run_file, dict) and "benchmarks" in run_file:
                controller = value.get("controller")
                if isinstance(controller, str):
                    return run_file, controller
    for comment in reversed(ticket.get("comments", [])):
        match = re.search(r"```json\s*(\{.*?\})\s*```", comment.get("body", ""), re.S)
        if match:
            value = json.loads(match.group(1))
            if isinstance(value, dict):
                return value, ""
    return None


def _pending_approval(ticket: dict[str, Any]) -> dict[str, Any]:
    raw = ticket.get("custom_fields", {}).get("approval_requests", {})
    pending = (
        [
            value
            for value in raw.values()
            if isinstance(value, dict) and value.get("status") == "pending"
        ]
        if isinstance(raw, dict)
        else []
    )
    if len(pending) != 1:
        raise GateError(
            f"approval pause must contain exactly one pending request, found {len(pending)}"
        )
    return pending[0]


def _description(config: GateConfig) -> str:
    return f"""Live black-box agentic-perf gate.

Controller: {config.controller}
System under test (client ID 1): {config.client.host}

Use resource provider user_provided, SSH user {config.ssh_user}, and SSH key
{config.ssh_key_path}. Use Crucible with remotehosts. Run exactly one `sleep`
benchmark with benchmark ID 1 bound only to the system under test. Set
seconds={config.seconds} and
num-samples={config.samples}. Disable every profiling and collection tool. You
may set endpoint setting `disable-tools` only to the JSON boolean `true`.
Express benchmark ID 1 only as `benchmarks[].ids: "1"`, and express client ID 1
only as the client engine's `ids: [1]`. Never add `benchmark-id` or `client-id`
to `mv-params`; they are structural IDs, not sleep benchmark parameters.

You may install only prerequisite packages required for this Crucible benchmark
on either supplied host. Do not modify network interfaces, addresses, MTUs,
queues, IRQ affinity, irqbalance, firewall, sysctls, storage, or other
operating-system settings.
Never include a `host-mounts` key anywhere in the run file, including an empty
list: absence of the key is the only allowed representation of no mounts.
Do not mount `/proc`, `/sys`, or any other host path into the endpoint.
The hosts and controller are already prepared; verify access without tuning or
updating them. Require user approval of the validated run file before execution.
Do not substitute hosts or add roles. Set skip_teardown=true.
"""


def _ticket_payload(config: GateConfig) -> dict[str, Any]:
    """Build the ticket with gate-critical policy represented structurally."""
    return {
        "summary": "Live E2E gate: one-client Crucible sleep benchmark",
        "description": _description(config),
        "custom_fields": {"directives": {"no_host_mounts": True}},
    }


def _client(home: Path, store_url: str) -> httpx.Client:
    token_path = home / "secrets" / "api-token"
    deadline = time.monotonic() + 20
    while not token_path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not token_path.exists():
        raise GateError(f"state-store token was not created under {home}")
    token = token_path.read_text().strip()
    return httpx.Client(
        base_url=store_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def _save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _contains_in_order(values: list[str], required: tuple[str, ...]) -> bool:
    remaining = iter(values)
    return all(any(value == expected for value in remaining) for expected in required)


def _validate_completed_ticket(ticket: dict[str, Any]) -> tuple[str, list[str]]:
    trail = ticket.get("status_trail", [])
    required_trail = (
        "new",
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
        "awaiting_customer_guidance",
        "awaiting_review",
        "awaiting_teardown",
        "retrospective_pending",
        "closed",
    )
    if not _contains_in_order(trail, required_trail):
        raise GateError(f"ticket did not traverse the required lifecycle: {trail}")
    fields = ticket.get("custom_fields", {})
    if fields.get("benchmark_status") != "completed":
        raise GateError("ticket closed without a completed benchmark")
    run_id = str(fields.get("run_id", ""))
    if not run_id:
        raise GateError("ticket closed without a Crucible run ID")
    if fields.get("claim") or fields.get("dispatch_claim"):
        raise GateError("ticket closed with a stale dispatch claim")
    plan = fields.get("execution_plan", {})
    steps = plan.get("steps", [])
    completed_agents = {
        step.get("agent_type") for step in steps if step.get("status") == "completed"
    }
    required_agents = {"resource", "provision", "benchmark", "review", "teardown"}
    missing_agents = required_agents - completed_agents
    if missing_agents:
        raise GateError(
            f"execution plan has incomplete agents: {sorted(missing_agents)}"
        )
    return run_id, trail


def _controller_command(config: GateConfig, command: str) -> str:
    key = Path(config.ssh_key_path).expanduser()
    if not key.is_file():
        raise GateError(f"configured SSH key does not exist: {key}")
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-i",
            str(key),
            f"{config.ssh_user}@{config.controller}",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise GateError(
            f"controller preflight failed ({result.returncode}): "
            f"{result.stderr.strip()[-500:]}"
        )
    return result.stdout


def _controller_run_inventory(config: GateConfig) -> set[str]:
    output = _controller_command(
        config,
        "find /var/lib/crucible/run -mindepth 1 -maxdepth 1 -type d "
        "-printf '%f\\n' 2>/dev/null",
    )
    return {line.strip() for line in output.splitlines() if line.strip()}


def _preflight_controller(config: GateConfig) -> set[str]:
    containers = _controller_command(
        config,
        "podman ps --format '{{.Names}}' 2>/dev/null",
    )
    active = [
        name
        for name in containers.splitlines()
        if "crucible-rickshaw-run" in name.strip()
    ]
    if active:
        raise GateError(f"controller has an active Crucible run: {active}")
    return _controller_run_inventory(config)


def run_gate(config: GateConfig, artifacts: Path, manage_services: bool) -> str:
    """Submit, approve, and observe one live ticket."""

    repo = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get("AGENTIC_PERF_HOME", "")).expanduser()
    if not str(home) or not home.is_dir():
        raise GateError("AGENTIC_PERF_HOME must name a prepared isolated instance")
    runtime_config = json.loads((home / "config.json").read_text())
    store_url = os.environ.get("STATE_STORE_URL", runtime_config["state_store"]["url"])
    artifacts.mkdir(parents=True, exist_ok=False)
    artifacts.chmod(0o700)
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _save_json(
        artifacts / "metadata.json", {"git_sha": git_sha, "store_url": store_url}
    )
    runs_before = _preflight_controller(config)
    _save_json(artifacts / "controller-runs-before.json", sorted(runs_before))

    service_script = repo / "scripts" / "start-bg.sh"
    if manage_services:
        subprocess.run([str(service_script)], cwd=repo, check=True)
    ticket_id = ""
    try:
        with _client(home, store_url) as client:
            existing = _request(client, "GET", "/api/v1/tickets")
            active = [
                ticket["id"] for ticket in existing if ticket["status"] != "closed"
            ]
            if active:
                raise GateError(
                    f"isolated instance already has active tickets: {active}"
                )
            ticket = _request(
                client,
                "POST",
                "/api/v1/tickets",
                json=_ticket_payload(config),
            )
            ticket_id = ticket["id"]
            _request(
                client,
                "POST",
                f"/api/v1/tickets/{ticket_id}/transition",
                json={"status": "triage_pending"},
            )
            deadline = time.monotonic() + config.timeout_seconds
            approved = False
            statuses: list[str] = []
            while time.monotonic() < deadline:
                ticket = _request(client, "GET", f"/api/v1/tickets/{ticket_id}")
                status = ticket["status"]
                if not statuses or statuses[-1] != status:
                    statuses.append(status)
                    _save_json(artifacts / "progress.json", {"statuses": statuses})
                _save_json(artifacts / "ticket.json", ticket)
                if status == "closed":
                    run_id, trail = _validate_completed_ticket(ticket)
                    runs_after = _controller_run_inventory(config)
                    new_runs = runs_after - runs_before
                    _save_json(
                        artifacts / "controller-runs-after.json",
                        {
                            "all": sorted(runs_after),
                            "new": sorted(new_runs),
                        },
                    )
                    if len(new_runs) != 1:
                        raise GateError(
                            "live ticket did not create exactly one external run: "
                            f"{sorted(new_runs)}"
                        )
                    if run_id not in next(iter(new_runs)):
                        raise GateError(
                            "ticket run ID does not match the new controller run"
                        )
                    _save_json(
                        artifacts / "ticket-result.json",
                        {
                            "status": "ticket_completed",
                            "ticket_id": ticket_id,
                            "run_id": run_id,
                            "external_run": next(iter(new_runs)),
                            "status_trail": trail,
                        },
                    )
                    return ticket_id
                if status == "awaiting_customer_guidance":
                    validation = _extract_run_file(ticket)
                    if approved:
                        time.sleep(config.poll_seconds)
                        continue
                    if validation is None:
                        raise GateError("ticket requested unrecognized human guidance")
                    run_file, controller = validation
                    if controller != config.controller:
                        raise GateError(
                            "validated run does not identify the approved controller"
                        )
                    validate_run_file(run_file, config)
                    _save_json(artifacts / "approved-run-file.json", run_file)
                    approval = _pending_approval(ticket)
                    approval_id = approval.get("approval_request_id")
                    if not isinstance(approval_id, str) or not approval_id:
                        raise GateError("pending approval has no approval_request_id")
                    _request(
                        client,
                        "POST",
                        f"/api/v1/tickets/{ticket_id}/approvals/{approval_id}/resolve",
                        json={
                            "decision": "approved",
                            "comment": "Approved by live gate policy.",
                            "validation_id": approval.get("validation_id"),
                            "presented_run_file_digest": approval.get(
                                "presented_run_file_digest"
                            ),
                            "execution_intent_digest": approval.get(
                                "execution_intent_digest"
                            ),
                        },
                    )
                    approved = True
                time.sleep(config.poll_seconds)
            raise GateError(f"ticket {ticket_id} exceeded the total timeout")
    finally:
        if manage_services:
            subprocess.run([str(service_script), "stop"], cwd=repo, check=False)
        for source in (
            home / "logs" / "orchestrator.log",
            home / "logs" / "state-store.log",
        ):
            if source.exists():
                (artifacts / source.name).write_bytes(source.read_bytes())


_RECOVERABLE_SSH_CONTEXT_RETRY = re.compile(
    r"Error calling tool '(?:verify_ssh_path|list_controller_userenvs)'.{0,6000}?"
    r"MCPToolCallError: Error calling tool '(?:verify_ssh_path|list_controller_userenvs)': "
    r"SSH context not set\. Call set_ssh_context\(\) first\."
    r".*?intentional_agent_retry",
    re.S,
)


def _fatal_log_signatures(text: str) -> list[str]:
    """Return fatal signatures after removing the known retryable MCP sequence."""

    text = _RECOVERABLE_SSH_CONTEXT_RETRY.sub("", text)
    fatal_patterns = (
        "Traceback (most recent call last):",
        "TraceDeliveryError",
        "cannot start a transaction within a transaction",
        "Task was destroyed but it is pending",
    )
    return [pattern for pattern in fatal_patterns if pattern in text]


def _validate_managed_service_shutdown(home: Path, repo: Path) -> None:
    status = subprocess.run(
        [str(repo / "scripts" / "start-bg.sh"), "status"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "State store:  STOPPED" not in status or "Orchestrator: STOPPED" not in status:
        raise GateError(f"managed services did not stop cleanly:\n{status}")
    for name in ("orchestrator.log", "state-store.log"):
        path = home / "logs" / name
        if not path.exists():
            raise GateError(f"expected service log is missing: {path}")
        text = path.read_text(errors="replace")
        found = _fatal_log_signatures(text)
        if found:
            raise GateError(f"{name} contains fatal signatures: {found}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("controller", help="Crucible controller hostname")
    parser.add_argument("system_under_test", help="single remote system hostname")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--services-already-running",
        action="store_true",
        help="do not start and stop this instance's services",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="required acknowledgement that the gate uses live systems",
    )
    args = parser.parse_args()
    try:
        home_value = os.environ.get("AGENTIC_PERF_HOME")
        if not home_value:
            raise GateError("enter a prepared development-instance shell first")
        home = Path(home_value).expanduser()
        runtime = json.loads((home / "config.json").read_text())
        config = GateConfig.from_runtime(
            args.controller,
            args.system_under_test,
            runtime,
            seconds=args.seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if not args.live:
            raise GateError("live execution requires the explicit opt-in flag")
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        artifacts = args.artifacts or home / "live-gate-artifacts" / timestamp
        lock_path = (
            Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
            / "agentic-perf-live-gate.lock"
        )
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GateError("another live gate is already running") from error
            manage_services = not args.services_already_running
            ticket_id = run_gate(
                config,
                artifacts,
                manage_services=manage_services,
            )
            if manage_services:
                _validate_managed_service_shutdown(
                    home, Path(__file__).resolve().parents[1]
                )
            _save_json(
                artifacts / "result.json",
                {"status": "passed", "ticket_id": ticket_id},
            )
        print(f"Live gate passed: {ticket_id}")
        return 0
    except (GateError, httpx.HTTPError, OSError, ValueError) as error:
        print(f"LIVE GATE FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
