"""FastMCP server for benchmark agent tools.

Exposes benchmark execution tools (skill docs, config, SSH operations)
over stdio.  The SkillProvider, SSHExecutor, and RepoCache are
constructed lazily on first tool call from environment variables and
ticket data, so credentials and provider internals never cross the LLM
boundary.

Run directly:  python agents/benchmark/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.mcp_audit import create_ticket_mcp
from agents.server_utils import (
    _emit_context_audit_event,
    _public_context_document,
    _public_context_result,
    build_crucible_context_gateway,
    build_repo_cache,
    build_skill_provider,
    build_ssh_from_ticket,
    controller_context_gateway,
    read_skill_documents,
    tool_progress,
)
from providers.execution import (
    AuditedFilesystem,
    AuditedSubprocessRunner,
    RootedPath,
    durable_filesystem_emitter,
)

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("benchmark-agent")

CONTROLLER_KEY_COMMENT = "agentic-perf-controller-key"

_CRUCIBLE_ROOT = "/opt/crucible"


def _write_ticket_staging_file(
    ticket_id: str, content: str
) -> tuple[AuditedFilesystem, str, str]:
    """Create an audited ticket or explicit system-context SCP staging file."""
    if ticket_id:
        filesystem = AuditedFilesystem(
            RootedPath(tempfile.gettempdir(), "workspace", logical_prefix="transport"),
            ticket_id=ticket_id,
            emit=durable_filesystem_emitter(),
            critical=True,
        )
    else:
        filesystem = AuditedFilesystem.system(Path(tempfile.gettempdir()))
    name = f"agentic-perf-{ticket_id}-{uuid.uuid4().hex}.json"
    return filesystem, name, str(filesystem.write(name, content))


def _compute_params_fingerprint(cf: dict[str, Any]) -> str:
    """SHA-256 fingerprint of the current execution plan step's mv_params."""
    plan = cf.get("execution_plan")
    if not plan:
        return "no-plan"
    steps = plan.get("steps", [])
    idx = plan.get("current_step", 0)
    if idx >= len(steps):
        return "no-plan"
    mv_params = steps[idx].get("params", {}).get("mv_params")
    if not mv_params:
        return "no-mv-params"
    return hashlib.sha256(json.dumps(mv_params, sort_keys=True).encode()).hexdigest()


def _apply_runfile_safety_directives(run_file: dict[str, Any]) -> dict[str, Any]:
    """Return a run-file that honours non-negotiable ticket safety directives.

    Some Crucible schemas describe optional ``host-mounts`` as an array with a
    minimum length.  An empty value is therefore invalid *and*, when the ticket
    forbids host mounts, violates the request just as a populated value would.
    Remove the key rather than relying on the model to infer that distinction.
    """
    directives = _ticket.get("custom_fields", {}).get("directives", {})
    if not isinstance(directives, dict) or not directives.get("no_host_mounts"):
        return run_file

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: sanitize(item)
                for key, item in value.items()
                if key != "host-mounts"
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(run_file)


def _runfile_fingerprint(run_file: dict[str, Any]) -> str:
    """Return the canonical digest that approval and execution bind to."""
    encoded = json.dumps(run_file, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _execution_plan_fingerprint(cf: dict[str, Any]) -> str:
    """Digest every current-step parameter, not only mv_params."""
    plan = cf.get("execution_plan", {})
    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    index = plan.get("current_step", 0) if isinstance(plan, dict) else 0
    params = (
        steps[index].get("params", {})
        if isinstance(index, int) and index < len(steps)
        else {}
    )
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _execution_intent_digest(
    runfile_digest: str,
    params_digest: str,
    harness: str,
    controller: str,
    run_command: str,
) -> str:
    payload = {
        "runfile": runfile_digest,
        "params": params_digest,
        "harness": harness,
        "controller": controller,
        "run_command": run_command,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validation_creator() -> dict[str, Any]:
    """Capture the causal caller and MCP server identity with the record."""
    from providers.tracing import current_trace_context

    context = current_trace_context()
    return {
        "agent_id": context.agent_id if context else "",
        "invocation_id": str(context.invocation_id) if context else "",
        "action_id": context.action_id if context else "",
        "request_id": (context.mcp_correlation_request_id if context else "")
        or os.environ.get("AGENTIC_PERF_REQUEST_ID", ""),
        "server_pid": os.getpid(),
        "server_host": socket.gethostname(),
    }


def _validation_identity_headers(creator: dict[str, Any]) -> dict[str, str]:
    """Propagate the trace identity required to spend a validation capability."""
    headers = {
        "X-Agentic-Perf-Validation-Agent-Id": str(
            creator.get("agent_id") or "benchmark-agent"
        ),
        "X-Agentic-Perf-Validation-Invocation-Id": str(
            creator.get("invocation_id", "")
        ),
        "X-Agentic-Perf-Validation-Action-Id": str(creator.get("action_id", "")),
        "X-Agentic-Perf-Validation-Request-Id": str(creator.get("request_id", "")),
    }
    for key, header in (
        ("session_id", "X-Agentic-Perf-Validation-Session-Id"),
        ("session_epoch", "X-Agentic-Perf-Validation-Session-Epoch"),
    ):
        if creator.get(key):
            headers[header] = str(creator[key])
    return {key: value for key, value in headers.items() if value}


def _validation_output_descriptor(ticket_id: str, output: str) -> dict[str, Any]:
    """Return the only validation-output representation safe to persist.

    Controller diagnostics can contain credentials and are often unbounded.
    Keep their content behind the same redaction, size-bound, and
    content-addressed-blob policy used by trace producers.
    """
    from providers.redaction import get_shared_redactor
    from providers.tracing import PayloadBuilder

    return (
        PayloadBuilder(get_shared_redactor())
        .build(
            ticket_id,
            output,
            media_type="text/plain",
        )
        .model_dump(mode="json")
    )


async def _persist_validated_runfile(
    run_file: dict[str, Any],
    harness: str,
    controller: str,
    params_fingerprint: str,
    execution_plan_fingerprint: str,
    run_command: str,
    validator_command: str,
    validation_output: str,
    validator_version: str,
) -> str | None:
    """Persist a runfile validation record and return its opaque ID.

    The record is retained in memory for test-mode calls and persisted onto
    the ticket when running under an agentic-perf ticket.  Execution must use
    this ID rather than supplying an independent runfile.
    """
    from providers.execution import AuditedAsyncHTTPClient

    ticket_id = os.environ.get("TICKET_ID", "")
    validation_id = f"val-{uuid.uuid4().hex}"
    runfile_digest = _runfile_fingerprint(run_file)
    record = {
        "validation_id": validation_id,
        "attempt_id": validation_id,
        "execution_intent_id": validation_id,
        "execution_plan_step_id": str(
            (os.environ.get("EXECUTION_PLAN_STEP_ID") or "current")
        ),
        "run_file": run_file,
        "runfile_fingerprint": runfile_digest,
        "harness": harness,
        "controller": controller,
        "params_fingerprint": params_fingerprint,
        "execution_plan_fingerprint": execution_plan_fingerprint,
        "execution_intent_digest": _execution_intent_digest(
            runfile_digest, execution_plan_fingerprint, harness, controller, run_command
        ),
        "run_command": run_command,
        "validator_command": validator_command,
        "validator_version": validator_version,
        "validation_output": _validation_output_descriptor(
            ticket_id, validation_output
        ),
    }
    creator = _validation_creator()
    _validation_records[validation_id] = record | {
        "state": "executable",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "creator": creator,
    }

    state_store_url = os.environ.get(
        "STATE_STORE_URL",
        "http://localhost:8090",
    )
    if not ticket_id:
        return validation_id

    try:
        from agents.server_utils import ticket_state_headers

        headers = ticket_state_headers()
        async with AuditedAsyncHTTPClient(
            timeout=10.0,
            headers=headers,
        ) as client:
            ticket_response = await client.get(
                f"{state_store_url}/api/v1/tickets/{ticket_id}",
            )
            ticket_response.raise_for_status()
            capability_response = await client.post(
                f"{state_store_url}/api/v1/tickets/{ticket_id}/validations/capability",
                headers={
                    "X-Agentic-Perf-Benchmark-Validator": os.environ.get(
                        "AGENTIC_PERF_BENCHMARK_VALIDATOR_TOKEN", ""
                    ),
                    **_validation_identity_headers(creator),
                },
            )
            capability_response.raise_for_status()
            capability = capability_response.json()["capability"]
            for _ in range(3):
                manifest = (
                    ticket_response.json()
                    .get("custom_fields", {})
                    .get("benchmark_validations", {})
                )
                response = await client.post(
                    f"{state_store_url}/api/v1/tickets/{ticket_id}/validations",
                    json={
                        "record": record,
                        "expected_version": manifest.get("version", 0),
                    },
                    headers={
                        "X-Agentic-Perf-Validation-Capability": capability,
                        **_validation_identity_headers(creator),
                    },
                )
                if response.status_code != 409:
                    response.raise_for_status()
                    break
                ticket_response = await client.get(
                    f"{state_store_url}/api/v1/tickets/{ticket_id}"
                )
                ticket_response.raise_for_status()
            else:
                response.raise_for_status()
        logger.info(
            "[benchmark] Persisted benchmark validation %s for %s (%s)",
            validation_id,
            ticket_id,
            harness,
        )
        return validation_id
    except Exception:
        logger.debug(
            "Failed to persist benchmark validation for %s",
            ticket_id,
            exc_info=True,
        )
        _validation_records.pop(validation_id, None)
        return None


_HARNESS_ALLOWED_BINARIES: dict[str, frozenset[str]] = {
    "crucible": frozenset({"crucible"}),
    "zathras": frozenset({"burden"}),
    "kube-burner": frozenset({"kube-burner"}),
    "vstorm": frozenset({"vstorm"}),
    "forge": frozenset({"run_cli"}),
    "clusterbuster": frozenset({"clusterbuster"}),
    "k8s-netperf": frozenset({"k8s-netperf"}),
}

_SHELL_INJECTION_RE = re.compile(
    r";|&&|\|\||`|\$\(|\|",
)

# Arcaflow plugin discovery is deliberately limited to the published
# Arcaflow plugin namespace. Besides preventing accidental execution of an
# unrelated image, this means the image can never contribute shell syntax to
# the remote command used by the schema probe.
_ARCAFLOW_PLUGIN_IMAGE_RE = re.compile(
    r"^quay\.io/arcalot/arcaflow-plugin-[a-z0-9][a-z0-9._-]*"
    r"(?::[a-zA-Z0-9][a-zA-Z0-9._-]*|@sha256:[0-9a-fA-F]{64})?$"
)


def _validate_run_command(
    run_command: str,
    harness: str,
) -> tuple[bool, str]:
    """Validate that run_command is a known harness binary, not arbitrary shell."""
    allowed = _HARNESS_ALLOWED_BINARIES.get(harness)
    if allowed is None:
        return False, f"Unknown harness {harness!r}"

    if _SHELL_INJECTION_RE.search(run_command):
        return False, (f"run_command contains shell metacharacters: {run_command!r}")

    try:
        tokens = shlex.split(run_command)
    except ValueError:
        tokens = run_command.split()

    binary = ""
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue
        binary = Path(token).name
        break

    if not binary:
        return False, "run_command is empty after parsing"

    if binary not in allowed:
        return False, (
            f"Binary {binary!r} not allowed for harness {harness!r} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )

    return True, "OK"


def _validate_plugin_image(plugin_image: str) -> tuple[bool, str]:
    """Validate an Arcaflow plugin image before using it in a remote command."""
    if not isinstance(plugin_image, str) or not plugin_image.strip():
        return False, "plugin_image must be a non-empty string"
    if not _ARCAFLOW_PLUGIN_IMAGE_RE.fullmatch(plugin_image):
        return (
            False,
            "plugin_image must be a quay.io/arcalot/arcaflow-plugin image "
            "with an optional tag or sha256 digest",
        )
    return True, "OK"


# Hosts that must never be passed as a reboot target.
# boot-timings-test.sh reboots the SUT — hitting localhost
# would kill the orchestrator.
_FORBIDDEN_REBOOT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _is_self_host(host: str) -> bool:
    """Return True if *host* resolves to the orchestrator itself."""
    import socket

    if host.lower() in _FORBIDDEN_REBOOT_HOSTS:
        return True
    try:
        own_hostname = socket.gethostname()
        if host.lower() == own_hostname.lower():
            return True
        own_fqdn = socket.getfqdn()
        if host.lower() == own_fqdn.lower():
            return True
    except Exception:
        pass
    return False


SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Module-level globals — lazily initialized by _ensure_init()
_initialized = False
_boot_time_executed = False
_ssh = None
_skill_provider = None
_crucible_context = None
_repo_cache = None
_ticket: dict[str, Any] = {}
_validation_records: dict[str, dict[str, Any]] = {}


def _benchmark_intent_identity(
    ticket_id: str,
    validation_id: str,
    record: dict[str, Any],
    controller: str,
    harness: str,
    run_command: str,
) -> tuple[str, str, dict[str, Any]]:
    """Return the immutable attempt, operation key, and request hash.

    A validation record is the lifecycle authority for an execution intent.
    Consequently a replay keeps the same attempt and operation identity while
    a new validation (the explicit rerun boundary) gets a new one.
    """
    attempt_id = str(record.get("attempt_id") or validation_id)
    intent_id = str(record.get("execution_intent_id") or validation_id)
    operation_key = f"benchmark-execution:{ticket_id or 'external'}:{intent_id}"
    immutable = {
        "ticket_id": ticket_id,
        "execution_plan_step_id": record.get("execution_plan_step_id", "current"),
        "attempt_id": attempt_id,
        "execution_intent_id": intent_id,
        "validation_id": validation_id,
        "validation_fingerprint": record.get("runfile_fingerprint"),
        "harness": harness,
        "controller": controller,
        "run_command": run_command,
    }
    request_hash = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return operation_key, request_hash, immutable


class _BenchmarkOperation:
    """Small async adapter over the shared fenced operation registry."""

    def __init__(self, key: str, request_hash: str, owner: str) -> None:
        self.key = key
        self.request_hash = request_hash
        self.owner = owner
        self._client = None
        self._local = None

    async def acquire(self) -> tuple[dict[str, Any], str]:
        from providers.tracing.client import TraceClient

        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
        if token:
            self._client = TraceClient(url, token)
            try:
                result = await asyncio.to_thread(
                    self._client.operation_acquire, self.key, self.request_hash, 900
                )
            except Exception as exc:
                # Only a conflict proves that another worker owns the intent.
                # Authentication, transport, and 5xx failures must fail closed.
                cause = exc
                while cause is not None:
                    response = getattr(cause, "response", None)
                    status_code = getattr(response, "status_code", None)
                    if status_code is not None:
                        if status_code == 409:
                            return {}, "in_progress"
                        return {}, "unavailable"
                    cause = cause.__cause__
                return {}, "unavailable"
            return result.get("operation", {}), result.get("status", "")
        from paths import TRACE_DB_PATH
        from state_store.trace_store import (
            OperationLeaseError,
            OperationTransitionError,
            TraceStore,
        )

        self._local = TraceStore(TRACE_DB_PATH)
        try:
            operation, status = await asyncio.to_thread(
                self._local.acquire_operation_result,
                self.key,
                self.request_hash,
                self.owner,
                900,
            )
        except (OperationLeaseError, OperationTransitionError):
            operation = self._local.get_operation(self.key)
            return (operation.__dict__ if operation else {}), "in_progress"
        return operation.__dict__, status

    async def transition(
        self, action: str, operation: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        token = int(operation["fencing_generation"])
        if self._client is not None:
            result = await asyncio.to_thread(
                self._client.operation_transition,
                self.key,
                action,
                token,
                **kwargs,
            )
            return result.get("operation", {})
        method = {
            "prepared": "mark_prepared",
            "side-effect-started": "mark_side_effect_started",
            "external-id": "attach_external_id",
            "complete": "complete",
            "fail": "fail",
            "indeterminate": "mark_indeterminate",
        }[action]
        call_kwargs = {"descriptor": kwargs.get("descriptor", {})}
        if action in {"prepared", "side-effect-started"}:
            call_kwargs = {}
        elif action == "external-id":
            call_kwargs = {"external_ids": kwargs.get("external_ids", {})}
        result = await asyncio.to_thread(
            getattr(self._local, method), self.key, self.owner, token, **call_kwargs
        )
        return result.__dict__

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
        if self._local is not None:
            self._local.close()

    async def terminalize(
        self,
        operation: dict[str, Any],
        outcome: str,
        descriptor: dict[str, Any],
    ) -> bool:
        """Persist a terminal outcome; retry ambiguous writes as indeterminate."""
        try:
            await self.transition(outcome, operation, descriptor=descriptor)
            return True
        except Exception:
            logger.exception("benchmark operation terminal write failed")
            if outcome != "indeterminate":
                try:
                    await self.transition(
                        "indeterminate",
                        operation,
                        descriptor={
                            "outcome": "indeterminate",
                            "terminal_write_failed": True,
                        },
                    )
                    return True
                except Exception:
                    logger.exception("benchmark operation indeterminate write failed")
            return False


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _skill_provider, _crucible_context, _repo_cache, _ticket
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    _skill_provider = build_skill_provider()
    _crucible_context = build_crucible_context_gateway(catalog_only=False)
    try:
        _repo_cache = await build_repo_cache()
    except Exception:
        _repo_cache = None
    _initialized = True


def _get_validated_runfile(
    validation_id: str,
    controller: str,
    harness: str,
    ticket: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the exact runfile previously validated for this ticket."""
    manifest = ticket.get("custom_fields", {}).get("benchmark_validations", {})
    if isinstance(manifest, dict) and isinstance(manifest.get("records"), dict):
        records = manifest.get("records", {}) if isinstance(manifest, dict) else {}
        record = records.get(validation_id)
        if isinstance(record, dict) and record.get("state") == "legacy_unapproved":
            return None, "validation token is legacy/unapproved"
        if (
            not isinstance(record, dict)
            or record.get("record_type", "validation") != "validation"
        ):
            return None, "unknown validation token"
        if record.get("state") != "executable":
            return None, "validation token is legacy/unapproved"
        supersession = next(
            (
                item
                for item in records.values()
                if isinstance(item, dict)
                and item.get("record_type") == "supersession"
                and item.get("supersedes_validation_id") == validation_id
            ),
            None,
        )
        if supersession:
            replacement = supersession.get("replacement_validation_id")
            return None, (
                "validation token is superseded"
                + (f"; replacement_validation_id={replacement}" if replacement else "")
            )
        if record.get("params_fingerprint") != _compute_params_fingerprint(
            ticket.get("custom_fields", {})
        ):
            return None, "validation token is fingerprint-mismatched"
        if record.get("execution_plan_fingerprint") != _execution_plan_fingerprint(
            ticket.get("custom_fields", {})
        ):
            return None, "validation token is execution-intent-mismatched"
    else:
        record = _validation_records.get(validation_id)

    if not isinstance(record, dict):
        return None, "unknown validation token"
    if record.get("validation_id") != validation_id:
        return None, "unknown validation token"
    if record.get("harness") != harness:
        return None, "Validation was performed for a different harness"
    if record.get("controller") != controller:
        return None, "Validation was performed for a different controller"
    run_file = record.get("run_file")
    if not isinstance(run_file, dict):
        return None, "Validation record does not contain a runfile"
    if record.get("runfile_fingerprint") != _runfile_fingerprint(run_file):
        return None, "validation token has invalid runfile fingerprint"
    return run_file, None


def _controller_host() -> str | None:
    """Return the ticket's explicit Crucible controller host."""
    fields = _ticket.get("custom_fields", {}) if _ticket else {}
    context = fields.get("crucible_controller_context")
    if isinstance(context, dict):
        for key in ("host", "controller"):
            if isinstance(context.get(key), str) and context[key].strip():
                return context[key].strip()
    assigned = fields.get("assigned_hardware_ips")
    if isinstance(assigned, dict) and isinstance(assigned.get("controller"), str):
        return assigned["controller"].strip() or None
    return None


def _authorized_plugin_hosts() -> set[str]:
    """Return host identities assigned to this ticket for schema discovery."""
    fields = _ticket.get("custom_fields", {}) if _ticket else {}
    authorized: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            authorized.add(value.strip().rstrip(".").lower())

    add(_controller_host())
    assigned = fields.get("assigned_hardware_ips", {})
    if isinstance(assigned, dict):
        add(assigned.get("controller"))
        targets = assigned.get("targets", [])
        if isinstance(targets, (list, tuple, set)):
            for target in targets:
                add(target)
        else:
            add(targets)

    inventory = fields.get("host_inventory", {})
    if isinstance(inventory, dict):
        for host in inventory:
            add(host)
    return authorized


def _validate_plugin_host(host: str) -> tuple[bool, str]:
    """Check that a schema target is an assigned controller/inventory host."""
    if not isinstance(host, str) or not host.strip():
        return False, "No target host available"
    normalized = host.strip().rstrip(".").lower()
    if normalized not in _authorized_plugin_hosts():
        return False, "Target host is not assigned to this ticket"
    return True, "OK"


async def _read_controller_file(host: str, path: str) -> str | None:
    """Read one allowlisted controller context file through the ticket SSH."""
    if _ssh is None:
        return None
    result = await _ssh.run(
        host,
        f"head -c 262144 {shlex.quote(path)}",
        timeout=30,
    )
    if result.exit_code != 0:
        return None
    return result.stdout


async def _find_controller_path(
    host: str, candidates: list[str], *, directory: bool = False
) -> str | None:
    """Return the first existing path from a bounded controller candidate list."""
    test_flag = "-d" if directory else "-f"
    quoted = " ".join(shlex.quote(path) for path in candidates)
    result = await _ssh.run(
        host,
        f"""for path in {quoted}; do if test {test_flag} "$path"; then printf '%s\\n' "$path"; break; fi; done""",
        timeout=30,
    )
    if result.exit_code != 0:
        return None
    value = result.stdout.strip().splitlines()
    return value[0].strip() if value else None


async def _find_controller_repository(
    host: str, entry: dict[str, Any], *, root: str = _CRUCIBLE_ROOT
) -> str | None:
    """Resolve a catalog entry against the installed controller tree.

    Crucible installs a graph of repositories and does not guarantee one
    directory naming convention.  The catalog gives us the repository name;
    the bounded filesystem search handles the installation-specific layout.
    """
    name = str(entry.get("name", ""))
    repository = str(entry.get("repository", ""))
    repo_name = repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_type = str(entry.get("type", "core"))
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return None
    candidates = [
        f"{root}/subprojects/{repo_type}s/{repo_name}",
        f"{root}/subprojects/{repo_type}/{repo_name}",
        f"{root}/subprojects/{repo_type}s/{name}",
        f"{root}/subprojects/{repo_type}/{name}",
        f"{root}/subprojects/benchmarks/bench-{name}",
        f"{root}/subprojects/benchmarks/{name}",
        f"{root}/repos/{repo_name}",
        f"{root}/repos/{name}",
        f"{root}/repos/{repo_type}-{name}",
    ]
    found = await _find_controller_path(host, candidates, directory=True)
    if found:
        return found
    # The catalog determines the names we search for; this does not expose a
    # general arbitrary-path reader to the agent.
    names = [value for value in (repo_name, name, f"{repo_type}-{name}") if value]
    # Crucible stores clones below repos/ using the full remote URL as the
    # directory name (for example
    # ``https:github.com:perftool-incubator/bench-perftest``).  The basename
    # therefore does not equal repo_name; match a bounded suffix as well.
    patterns = list(dict.fromkeys(names + ([f"*{repo_name}"] if repo_name else [])))
    quoted = " ".join(shlex.quote(value) for value in patterns)
    result = await _ssh.run(
        host,
        f"find {shlex.quote(root)}/subprojects {shlex.quote(root)}/repos "
        f"-maxdepth 6 -type d \\\\( -name {quoted.replace(' ', ' -o -name ')} \\\\) "
        "-print -quit 2>/dev/null",
        timeout=30,
    )
    value = result.stdout.strip().splitlines() if result.exit_code == 0 else []
    return value[0].strip() if value else None


async def _refresh_controller_bootstrap(manager: Any) -> dict[str, Any]:
    """Read the single controller document needed to bootstrap discovery."""
    host = _controller_host()
    if not host or _ssh is None:
        return {"available": False, "reason": "controller_not_identified"}

    bootstrap_path = await _find_controller_path(
        host,
        [
            f"{_CRUCIBLE_ROOT}/AGENTS.md",
            f"{_CRUCIBLE_ROOT}/subprojects/core/AGENTS.md",
        ],
    )
    if not bootstrap_path:
        return {
            "available": False,
            "reason": "controller_bootstrap_document_unavailable",
            "host": host,
        }
    content = await _read_controller_file(host, bootstrap_path)
    if content is None:
        return {
            "available": False,
            "reason": "controller_bootstrap_document_unreadable",
            "host": host,
            "path": bootstrap_path,
        }

    provenance = {
        "effective_source": "controller",
        "source_reason": "controller_bootstrap_document",
        "controller": host,
        "crucible_root": _CRUCIBLE_ROOT,
        "bootstrap_path": bootstrap_path,
    }
    saved = manager.save_source_snapshot(
        "controller", provenance, {"AGENTS.md": content}
    )
    document = {
        "namespace": "core",
        "path": "core/AGENTS.md",
        "ref": "core/AGENTS.md",
        "uri": "crucible://core/AGENTS.md",
        "source_path": "AGENTS.md",
        "source": "controller",
        "authority": "effective",
        "provenance": provenance,
        "entrypoint": True,
        "content": content,
        "workspace_ref": saved.get("files", {}).get("AGENTS.md"),
    }
    manager.index_context_documents([document])
    return {
        "available": True,
        "reason": "controller_bootstrap_succeeded",
        "host": host,
        "provenance": provenance,
        "document": document,
    }


async def _refresh_controller_context(
    benchmark: str, manager: Any, *, namespace: str = "all"
) -> dict[str, Any]:
    """Snapshot Crucible context from the designated controller.

    The context gateway owns this refresh so source selection is based on the
    same controller that will execute the run.  Only documentation, catalog,
    and benchmark metadata paths are read; no arbitrary controller command is
    exposed through the gateway.
    """
    host = _controller_host()
    if not host or _ssh is None:
        return {"available": False, "reason": "controller_not_identified"}

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", benchmark or ""):
        return {"available": False, "reason": "invalid_benchmark_name", "host": host}

    catalog_path = await _find_controller_path(
        host,
        [
            f"{_CRUCIBLE_ROOT}/config/repos.json",
            f"{_CRUCIBLE_ROOT}/subprojects/core/config/repos.json",
        ],
    )
    if not catalog_path:
        return {"available": False, "reason": "crucible_not_installed", "host": host}

    # The top-level catalog describes the repository graph; it is not
    # necessarily the root of the active core repository.  Discover core docs
    # and schemas independently from the catalog location.
    core_root = f"{_CRUCIBLE_ROOT}/subprojects/core"
    core_docs_root = await _find_controller_path(
        host,
        [f"{_CRUCIBLE_ROOT}/docs", f"{core_root}/docs"],
        directory=True,
    )
    listing = await _ssh.run(
        host,
        f"find {shlex.quote(core_docs_root or core_root)} -type f "
        "\\( -name '*.md' -o -name '*.markdown' -o -name '*.rst' "
        "-o -name '*.txt' -o -name '*.json' -o -name '*.yaml' "
        "-o -name '*.yml' -o -name '*.toml' \\) -print",
        timeout=30,
    )
    core_paths: list[tuple[str, str]] = [(catalog_path, "config/repos.json")]
    bootstrap_path = await _find_controller_path(
        host,
        [f"{_CRUCIBLE_ROOT}/AGENTS.md", f"{core_root}/AGENTS.md"],
    )
    if bootstrap_path:
        core_paths.append((bootstrap_path, "AGENTS.md"))
    # Run-file and tool schemas are controller runtime metadata, not static
    # agentic-perf skills.  Snapshot the installed controller copies so the
    # context gateway can serve them to benchmark and review agents.
    for name in ("run-file.json", "tool-params.json", "remotehosts.json"):
        schema_path = await _find_controller_path(
            host,
            [
                f"{core_root}/rickshaw/schema/{name}",
                f"{_CRUCIBLE_ROOT}/rickshaw/schema/{name}",
            ],
        )
        if schema_path:
            core_paths.append((schema_path, f"rickshaw/schema/{name}"))
    docs_root = core_docs_root or core_root
    if listing.exit_code == 0:
        core_paths.extend(
            (line.strip(), f"docs/{Path(line.strip()).relative_to(docs_root)}")
            for line in listing.stdout.splitlines()
            if line.strip().startswith(docs_root + "/")
        )
    core_files: dict[str, str] = {}
    # Keep controller reads sequential. A Crucible controller commonly has a
    # conservative SSH MaxStartups setting; a bulk gather here can cause the
    # controller to reset handshakes and make an otherwise healthy context
    # source appear empty.
    for path, relative in core_paths:
        content = await _read_controller_file(host, path)
        if content is None:
            continue
        core_files[relative] = content

    try:
        catalog = json.loads(core_files["config/repos.json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        catalog = {}
    catalog_entries = {
        entry.get("name"): entry
        for group in ("official", "unofficial")
        for entry in catalog.get(group, [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    needs_benchmark = namespace == "all" or namespace.startswith("benchmark/")
    benchmark_entry = catalog_entries.get(benchmark)
    benchmark_root = None
    if needs_benchmark:
        if not benchmark_entry or benchmark_entry.get("type") != "benchmark":
            return {
                "available": False,
                "reason": "benchmark_not_in_controller_catalog",
                "host": host,
                "benchmark": benchmark,
            }
        benchmark_root = await _find_controller_repository(host, benchmark_entry)
        if not benchmark_root:
            return {
                "available": False,
                "reason": "controller_benchmark_checkout_unavailable",
                "host": host,
                "benchmark": benchmark,
                "catalog_path": catalog_path,
            }

    from providers.skills.crucible import CrucibleContextGateway

    # Discovery is intentionally lazy. The bootstrap AGENTS.md tells the
    # caller how to ask for other namespaces; a request for one benchmark must
    # not trigger SSH probes and file reads for every repo in repos.json.
    repository_roots: dict[str, str] = {}
    if benchmark_root:
        repository_roots[f"benchmark/{benchmark}"] = benchmark_root

    async def read_repository(namespace: str, repository_root: str) -> dict[str, str]:
        listing = await _ssh.run(
            host,
            f"find {shlex.quote(repository_root)} -type f "
            "\\( -name '*.md' -o -name '*.markdown' -o -name '*.rst' "
            "-o -name '*.txt' -o -name '*.json' -o -name '*.yaml' "
            "-o -name '*.yml' -o -name '*.toml' \\) -print",
            timeout=30,
        )
        paths: list[str] = []
        if listing.exit_code == 0:
            for value in listing.stdout.splitlines():
                path = value.strip()
                if not path.startswith(repository_root + "/"):
                    continue
                relative = Path(path.removeprefix(repository_root + "/"))
                if CrucibleContextGateway._safe_context_file(
                    Path(repository_root),
                    relative,
                    benchmark_namespace=namespace.startswith("benchmark/"),
                ):
                    paths.append(path)
        files: dict[str, str] = {}
        for path in paths:
            content = await _read_controller_file(host, path)
            if content is not None:
                files[
                    f"repositories/{namespace}/{path.removeprefix(repository_root + '/')}"
                ] = content
        return files

    repository_results = []
    for repository_namespace, repository_root in repository_roots.items():
        repository_results.append(
            await read_repository(repository_namespace, repository_root)
        )
    repository_files = {
        key: value for result in repository_results for key, value in result.items()
    }
    benchmark_files = {
        key.removeprefix(f"repositories/benchmark/{benchmark}/"): value
        for key, value in repository_files.items()
        if key.startswith(f"repositories/benchmark/{benchmark}/")
    }

    if not core_files or (needs_benchmark and not benchmark_files):
        return {
            "available": False,
            "reason": "controller_context_incomplete",
            "host": host,
            "core_files": len(core_files),
            "benchmark_files": len(benchmark_files),
            "repositories": sorted(repository_roots),
        }

    provenance = {
        "effective_source": "controller",
        "source_reason": "controller_refresh_succeeded",
        "controller": host,
        "crucible_root": _CRUCIBLE_ROOT,
        "catalog_path": catalog_path,
        "core_docs_root": core_docs_root,
        "benchmark_root": benchmark_root,
        "repository_roots": repository_roots,
    }
    manager.save_source_snapshot(
        "controller", provenance, {**core_files, **repository_files}
    )
    if benchmark_files:
        manager.save_source_snapshot(
            "controller", provenance, benchmark_files, benchmark=benchmark
        )
    return {
        "available": True,
        "reason": "controller_refresh_succeeded",
        "host": host,
        "core_files": len(core_files),
        "benchmark_files": len(benchmark_files),
        "provenance": provenance,
    }


def _controller_snapshot_documents(
    manager: Any,
    *,
    benchmark: str,
    namespace: str,
    subject_area: str | list[str],
    provenance: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the effective inventory from the remote controller snapshot."""
    from providers.skills.crucible import CrucibleContextGateway

    documents: list[dict[str, Any]] = []
    missing_namespaces: list[str] = []
    snapshot = manager.load_source_snapshot("controller")
    if snapshot and snapshot.get("files"):
        owners: dict[str, dict[str, str]] = {}
        for relative, content in snapshot.get("files", {}).items():
            if relative.startswith("repositories/"):
                parts = relative.split("/", 3)
                if len(parts) < 4:
                    continue
                owner = f"{parts[1]}/{parts[2]}"
                source_path = parts[3]
            else:
                owner, source_path = "core", relative
            owners.setdefault(owner, {})[relative] = content
        requested_owners = set(owners)
        if namespace == "core":
            requested_owners = {"core"}
        elif namespace != "all":
            requested_owners = {namespace}
        elif benchmark:
            requested_owners = {
                owner
                for owner in requested_owners
                if owner == "core"
                or owner == f"benchmark/{benchmark}"
                or owner.startswith(("core/", "tool/", "doc/"))
            }
        for owner in sorted(requested_owners):
            files = owners.get(owner, {})
            if not files:
                missing_namespaces.append(owner)
                continue
            for snapshot_path, content in sorted(files.items()):
                source_path = (
                    snapshot_path.split("/", 3)[3]
                    if snapshot_path.startswith("repositories/")
                    else snapshot_path
                )
                logical_ref = f"{owner}/{source_path}"
                documents.append(
                    {
                        "namespace": owner,
                        "path": logical_ref,
                        "ref": logical_ref,
                        "uri": f"crucible://{logical_ref}",
                        "source_path": snapshot_path,
                        "source": "controller",
                        "authority": "effective",
                        "provenance": provenance,
                        "entrypoint": source_path
                        in CrucibleContextGateway._REPOSITORY_ENTRYPOINT_FILES,
                        "subject_areas": CrucibleContextGateway._subject_tags(
                            source_path
                        ),
                        "subject_match": CrucibleContextGateway._subject_matches(
                            source_path, subject_area
                        ),
                        "content": content,
                    }
                )
    else:
        missing_namespaces.append("core")
    documents.sort(key=lambda item: item["path"])
    return documents, {
        "complete": not missing_namespaces,
        "discovered": len(documents),
        "returned": len(documents),
        "subject_matches": sum(bool(item.get("subject_match")) for item in documents),
        "excluded": 0,
        "exclusions": [],
        "missing_namespaces": missing_namespaces,
    }


# ---------------------------------------------------------------------------
# Skill / Doc tools (no SSH)
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read local skill documents for harnesses that still use the legacy fallback."""
    await _ensure_init()
    return json.dumps(read_skill_documents(SKILLS_DIR, docs))


@mcp.tool()
async def list_harness_docs(harness: str) -> str:
    """List documentation files available for a benchmark harness. Returns file paths and sizes. Use this to discover what reference material is available before constructing a run file."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"docs": [], "message": "No repo cache configured"})
    docs = _repo_cache.list_docs(harness, subdirs=["docs", "config"])
    if not docs:
        return json.dumps(
            {"docs": [], "message": f"No docs found for harness '{harness}'"}
        )
    return json.dumps({"docs": docs, "count": len(docs)})


@mcp.tool()
async def read_harness_doc(harness: str, doc_path: str) -> str:
    """Read a documentation file from a benchmark harness repository (e.g. harness='crucible', doc_path='docs/how-run-files-work.md'). Use this to learn about run-file format, endpoint structure, benchmark parameters, or any other harness-specific details. Call list_harness_docs first to see available files."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"found": False, "message": "No repo cache configured"})
    if not harness and "/" in doc_path:
        harness, doc_path = doc_path.strip().lstrip("/").split("/", 1)
    content = _repo_cache.read_file(harness, doc_path)
    if content is None:
        return json.dumps(
            {"found": False, "message": f"File not found: {harness}/{doc_path}"}
        )
    return json.dumps({"found": True, "path": doc_path, "content": content})


# ---------------------------------------------------------------------------
# Config tools (no SSH)
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_execution_config(harness_name: str) -> str:
    """Get the benchmark harness's execution configuration from private skills. Returns controller requirements, pre-run steps, run command, endpoint type, run file format, and defaults. The harness_name should be the harness that owns the benchmark (e.g., 'crucible' or 'zathras')."""
    await _ensure_init()
    if harness_name == "crucible":
        return json.dumps(
            {
                "harness": "crucible",
                "found": False,
                "message": (
                    "Crucible execution guidance is controller-sourced context. "
                    "Use get_crucible_benchmark_context; use action tools for "
                    "runtime discovery, validation, and execution."
                ),
            }
        )
    # Arcaflow plugins are self-contained containers — no private
    # execution config or harness installation is needed.
    if harness_name == "arcaflow-plugins":
        return json.dumps(
            {
                "harness": harness_name,
                "found": True,
                "controller_required": False,
                "run_command": "podman run",
                "endpoint_type": "remotehosts",
                "endpoint_user": "root",
                "run_file_format": "yaml",
                "results_dir_pattern": "",
                "default_image_registry": "quay.io/arcalot",
                "image_naming": (
                    "quay.io/arcalot/arcaflow-plugin-<workload> "
                    "(community plugins; third-party plugins "
                    "may use different registries)"
                ),
                "workflow": [
                    "1. Call get_plugin_schema with the plugin image to discover available steps and input parameters",
                    "2. Build the input YAML based on the schema and ticket parameters",
                    "3. Call execute_benchmark with "
                    "run_file containing: plugin_image, "
                    "plugin_step (the step name, e.g. "
                    "'uperf' or 'workload'), and input "
                    "(the YAML parameters as a dict). "
                    "The tool handles podman run, -s flag, "
                    "stdin piping, and result collection.",
                ],
                "run_file_keys": {
                    "plugin_image": "required — full container image ref",
                    "plugin_step": "required — step name (e.g. 'workload', 'uperf')",
                    "input": "required — plugin input parameters as a dict",
                },
                "note": (
                    "Arcaflow plugins are containers. "
                    "Community plugins from quay.io/arcalot "
                    "are typically multi-arch (amd64 + arm64). "
                    "Do NOT manually "
                    "pull or run containers — use "
                    "get_runfile_schema and "
                    "execute_benchmark which handle "
                    "image resolution and execution. "
                    "Do NOT try to install workload "
                    "binaries (uperf, fio, etc.) on the "
                    "host — they run inside containers."
                ),
            }
        )

    config = await _skill_provider.get_all_private_config(harness_name)
    execution = config.get("execution", {})
    if not execution:
        return json.dumps(
            {
                "harness": harness_name,
                "found": False,
                "message": f"No execution config found for harness '{harness_name}'",
            }
        )
    result = {
        "harness": harness_name,
        "found": True,
        "controller_required": execution.get("controller_required", False),
        "run_command": execution.get("run_command", ""),
        "endpoint_type": execution.get("endpoint_type", "remotehosts"),
        "endpoint_user": execution.get("endpoint_user", "root"),
        "default_osruntime": execution.get("default_osruntime", "podman"),
        "pre_run": execution.get("pre_run", []),
        "run_file_format": execution.get("run_file_format", "json"),
        "results_dir_pattern": execution.get("results_dir_pattern", ""),
    }
    if harness_name == "crucible":
        # Userenv availability belongs to the running Crucible controller.
        # Never propagate a stale/static value from private metadata.
        result["userenv_discovery"] = execution.get(
            "userenv_discovery",
            {
                "required": True,
                "command": "crucible userenvs list",
                "source": "running_controller",
            },
        )
    elif "default_userenv" in execution:
        result["default_userenv"] = execution["default_userenv"]
    return json.dumps(result)


@mcp.tool()
async def get_runfile_schema(harness: str = "crucible") -> str:
    """Get the JSON schema that defines the structure of a valid run-file. Use this to understand what top-level keys, benchmark objects, endpoint structures, and mv-params formats are allowed. The schema enforces additionalProperties: false, so only documented keys are permitted."""
    await _ensure_init()
    harness_name = harness or "crucible"
    if harness_name == "crucible":
        return json.dumps(
            {
                "found": False,
                "harness": "crucible",
                "message": (
                    "The Crucible run-file schema is controller-sourced context. "
                    'Use get_crucible_benchmark_context(operation="list" or '
                    '"read") to retrieve it.'
                ),
            }
        )
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        schema = await provider.get_runfile_schema() if provider else None
    else:
        schema = await _skill_provider.get_runfile_schema()
    if schema is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No run-file schema for harness '{harness_name}'",
            }
        )
    return json.dumps({"found": True, "harness": harness_name, "schema": schema})


@mcp.tool()
async def get_plugin_schema(
    plugin_image: str,
    host: str = "",
) -> str:
    """Query an Arcaflow plugin container for its input schema.

    Runs the plugin with --json-schema input on the target host
    via podman. Returns the JSON schema describing the plugin's
    available steps and their input parameters.

    Args:
        plugin_image: Full container image ref
            (e.g., quay.io/arcalot/arcaflow-plugin-fio:0.5.0)
        host: Target host IP. Uses the ticket's controller
            if not specified.
    """
    await _ensure_init()
    if _ssh is None:
        return json.dumps({"error": "SSH not initialized"})

    image_valid, image_error = _validate_plugin_image(plugin_image)
    if not image_valid:
        return json.dumps({"error": image_error})

    target = host or _controller_host() or ""
    host_valid, host_error = _validate_plugin_host(target)
    if not host_valid:
        return json.dumps({"error": host_error})

    cmd = shlex.join(["podman", "run", "--rm", plugin_image, "--json-schema", "input"])
    result = await _ssh.run(target, cmd, timeout=60)

    if result.exit_code != 0:
        # Try --schema as fallback (returns full schema)
        cmd_full = shlex.join(["podman", "run", "--rm", plugin_image, "--schema"])
        result = await _ssh.run(target, cmd_full, timeout=60)

    if result.exit_code != 0:
        return json.dumps(
            {
                "error": f"Failed to query plugin schema (exit {result.exit_code})",
                "stderr": result.stderr[:500] if result.stderr else "",
                "hint": (
                    "The plugin image may not exist or may not "
                    "support --json-schema. Check the image ref."
                ),
            }
        )

    # Parse and return the schema
    try:
        schema = json.loads(result.stdout)
        return json.dumps(
            {
                "plugin_image": plugin_image,
                "schema": schema,
            }
        )
    except json.JSONDecodeError:
        return json.dumps(
            {
                "plugin_image": plugin_image,
                "raw_output": result.stdout[:2000],
                "note": "Output was not valid JSON",
            }
        )


@mcp.tool()
async def get_benchmark_params(benchmark: str, harness: str = "crucible") -> str:
    """Get the parameter definitions (multiplex.json) for a specific benchmark. Returns presets (named parameter sets like 'basic', 'default') and validations (regex patterns for allowed values per argument). Use this to understand what mv-params arguments are valid and what values they accept."""
    await _ensure_init()
    harness_name = harness or "crucible"
    if harness_name == "crucible":
        return json.dumps(
            {
                "found": False,
                "benchmark": benchmark,
                "harness": "crucible",
                "message": (
                    "Crucible benchmark metadata is controller-sourced context. "
                    "Use get_crucible_benchmark_context to read multiplex.json "
                    "and related files."
                ),
            }
        )
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        params = await provider.get_benchmark_params(benchmark) if provider else None
    else:
        params = await _skill_provider.get_benchmark_params(benchmark)
    if params is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No parameter definitions for '{benchmark}' in '{harness_name}'",
            }
        )
    return json.dumps(
        {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "params": params,
        }
    )


@mcp.tool(name="get_crucible_benchmark_context")
async def _get_crucible_benchmark_context_tool(
    operation: str = "bootstrap",
    path: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Use generic context primitives for the designated Crucible controller.

    ``bootstrap`` returns the controller's entrypoint document. ``read`` reads
    a caller-selected path with bounded byte paging. ``search`` searches
    controller paths and file contents, returning grouped candidates and sizes.
    Source selection, phase policy, and provenance are server-managed.
    """
    await _ensure_init()
    if operation not in {"bootstrap", "read", "search"}:
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "unsupported_operation",
                "guidance": "Use bootstrap, read, or search.",
            }
        )
    controller_host = _controller_host()
    if _ssh is None or not controller_host:
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "controller_not_identified",
            }
        )
    return await controller_context_gateway(
        ssh=_ssh,
        controller_host=controller_host,
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="benchmark-agent",
        phase="benchmark",
        operation=operation,
        path=path,
        query=query,
        benchmark="",
        include_alternates=False,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
    )


async def _legacy_get_crucible_benchmark_context(
    benchmark: str = "",
    phase: str = "benchmark",
    operation: str = "context",
    namespace: str = "all",
    path: str = "",
    subject_area: str | list[str] = "all",
    include_alternates: bool = False,
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Compatibility implementation for pre-gateway internal callers."""
    await _ensure_init()
    if (
        operation in {"bootstrap", "list", "read", "search"}
        and _ssh is not None
        and _controller_host()
    ):
        return await controller_context_gateway(
            ssh=_ssh,
            controller_host=_controller_host(),
            ticket_id=os.environ.get("TICKET_ID", ""),
            agent_name="benchmark-agent",
            phase=phase,
            benchmark=benchmark,
            operation=operation,
            path=path,
            query=query,
            include_alternates=include_alternates,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )
    if operation == "context" and _ssh is not None and _controller_host():
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "unsupported_operation_use_bootstrap_read_or_search",
                "guidance": (
                    "Read controller AGENTS.md first, then request the documented "
                    "controller-relative paths with operation=read."
                ),
            }
        )
    if operation == "bootstrap":
        ticket_id = os.environ.get("TICKET_ID", "")
        if not ticket_id:
            return json.dumps(
                {
                    "found": False,
                    "operation": "bootstrap",
                    "reason": "ticket_required_for_controller_context",
                }
            )
        from providers.workspace.manager import WorkspaceManager

        manager = WorkspaceManager(
            ticket_id=ticket_id, agent_name="benchmark-agent", phase=phase
        )
        bootstrap = await _refresh_controller_bootstrap(manager)
        if not bootstrap.get("available"):
            return json.dumps(
                {
                    "found": False,
                    "operation": "bootstrap",
                    **{
                        key: value
                        for key, value in bootstrap.items()
                        if key != "document"
                    },
                }
            )
        document = bootstrap["document"]
        result = {
            "found": True,
            "operation": "bootstrap",
            "documents": [document],
            "document": document,
            "workspace": {
                "effective_context": manager.save_effective_context(
                    {
                        "schema_version": 1,
                        "policy": "controller_bootstrap",
                        "namespace": "core",
                        "subject_area": "all",
                        "documents": [_public_context_document(document)],
                    }
                )
            },
        }
        _emit_context_audit_event(
            ticket_id,
            agent_name="benchmark-agent",
            phase=phase,
            benchmark="",
            operation="bootstrap",
            namespace="core",
            result=result,
        )
        return json.dumps(
            _public_context_result(
                result,
                phase=phase,
                audience="benchmark-agent",
                benchmark="",
                namespace="core",
                subject_area="all",
            )
        )
    provider = _crucible_context
    if provider is None or not hasattr(provider, "get_benchmark_context"):
        return json.dumps(
            {
                "found": False,
                "benchmark": benchmark,
                "reason": "crucible_provider_unavailable",
            }
        )
    if operation in {"list", "read", "search"} and hasattr(
        provider, "get_crucible_context"
    ):
        requested_operation = operation
        provider_operation = "list" if operation == "search" else operation
        cf = _ticket.get("custom_fields", {}) if _ticket else {}
        controller = cf.get("crucible_controller_context", {})
        controller = dict(controller) if isinstance(controller, dict) else {}
        ticket_id = os.environ.get("TICKET_ID", "")
        manager = None
        refresh = {"available": False, "reason": "workspace_unavailable"}
        if ticket_id:
            from providers.workspace.manager import WorkspaceManager

            manager = WorkspaceManager(
                ticket_id=ticket_id, agent_name="benchmark-agent", phase="benchmark"
            )
            if operation == "read" and path:
                cached = manager.read_document(
                    path, include_alternates=include_alternates
                )
                if cached.get("status") == "ok":
                    return json.dumps(
                        _public_context_result(
                            {
                                "found": True,
                                "operation": "read",
                                "document": cached,
                                "documents": [cached],
                            },
                            phase=phase,
                            audience=manager.audience,
                            benchmark=benchmark,
                            namespace=namespace,
                            subject_area=subject_area,
                            manifest_documents=manager.context_manifest(namespace).get(
                                "documents", []
                            ),
                        )
                    )
            if operation == "search" and manager.context_scope_indexed(namespace):
                return json.dumps(
                    _public_context_result(
                        manager.search_documents(
                            query,
                            namespace=namespace if namespace != "all" else "",
                            include_alternates=include_alternates,
                        ),
                        phase=phase,
                        audience=manager.audience,
                        benchmark=benchmark,
                        namespace=namespace,
                        subject_area=subject_area,
                        manifest_documents=manager.context_manifest(namespace).get(
                            "documents", []
                        ),
                    )
                )
            refresh = await _refresh_controller_context(
                benchmark, manager, namespace=namespace
            )
            existing = manager.load_source_snapshot("controller", benchmark)
            if existing and not refresh.get("available"):
                refresh = {
                    "available": True,
                    "reason": "controller_snapshot_already_present",
                    "provenance": existing.get("provenance", {}),
                }
            controller.update(
                {
                    "identified": controller.get("identified") is True
                    or bool(_controller_host()),
                    "reachable": controller.get("reachable") is True
                    or refresh.get("reason") != "controller_not_identified",
                    "crucible_installed": controller.get("crucible_installed") is True
                    or refresh.get("reason")
                    not in {"controller_not_identified", "crucible_not_installed"},
                    "snapshot_available": refresh.get("available", False),
                }
            )
        policy = cf.get("crucible_update_policy")
        if not policy:
            policy = cf.get("directives", {}).get("update_harness")
        from providers.skills.crucible import select_crucible_context

        policy_selection = select_crucible_context(
            phase=phase, controller=controller, update_policy=policy
        )
        result = await provider.get_crucible_context(
            benchmark or None,
            operation=provider_operation,
            namespace=namespace,
            path=path,
            subject_area=subject_area,
            include_alternates=include_alternates,
            query=query,
            phase=phase,
            controller=controller,
            update_policy=policy,
            agent="benchmark-agent",
            include_content=True,
        )
        # A controller snapshot is authoritative even when the agentic-perf
        # host has no matching Crucible checkout.  Do not require the local
        # provider to find a document before using the successfully refreshed
        # controller source.
        if (
            manager
            and not result.get("found")
            and refresh.get("available")
            and controller.get("snapshot_available")
            and policy_selection["effective_source"] == "controller"
        ):
            controller_documents, controller_inventory = _controller_snapshot_documents(
                manager,
                benchmark=benchmark,
                namespace=namespace,
                subject_area=subject_area,
                provenance=refresh["provenance"],
            )
            result.update(
                {
                    "found": bool(controller_documents),
                    "effective_source": "controller",
                    "source": "controller",
                    "selection": policy_selection,
                    "documents": controller_documents,
                    "inventory": controller_inventory,
                }
            )
        if manager and result.get("found"):
            if (
                refresh.get("available")
                and controller.get("snapshot_available")
                and policy_selection["effective_source"] == "controller"
            ):
                result["effective_source"] = "controller"
                result["source"] = "controller"
                result["selection"] = policy_selection
                controller_documents, controller_inventory = (
                    _controller_snapshot_documents(
                        manager,
                        benchmark=benchmark,
                        namespace=namespace,
                        subject_area=subject_area,
                        provenance=refresh["provenance"],
                    )
                )
                local_documents = [
                    item
                    for item in result.get("documents", [])
                    if item.get("source") == "local"
                ]
                result["documents"] = sorted(
                    controller_documents + local_documents,
                    key=lambda item: item["path"],
                )
                result["found"] = bool(result["documents"])
                result["inventory"] = controller_inventory
            indexed_documents: list[dict[str, Any]] = []
            grouped_files: dict[tuple[str, str | None], dict[str, str]] = {}
            for document in result.get("documents", []):
                content = document.get("content")
                if content is None:
                    read_result = await provider.get_crucible_context(
                        benchmark or None,
                        operation="read",
                        namespace=document["namespace"],
                        path=document.get("ref", document["path"]),
                        subject_area="all",
                        include_alternates=include_alternates,
                        phase=phase,
                        controller=controller,
                        update_policy=policy,
                        agent="benchmark-agent",
                    )
                    read_document = read_result.get("document")
                    if isinstance(read_document, dict):
                        content = read_document.get("content")
                if content is None:
                    continue
                doc_namespace = document["namespace"]
                doc_benchmark = None
                if doc_namespace.startswith("benchmark/"):
                    _, doc_benchmark = doc_namespace.split("/", 1)
                elif document.get("source") == "local" and document.get("benchmark"):
                    doc_benchmark = document["benchmark"]
                source = document.get(
                    "source", result.get("effective_source", "github")
                )
                grouped_files.setdefault((source, doc_benchmark), {})[
                    document["source_path"]
                ] = content

            refs: list[str] = []
            saved_groups: dict[tuple[str, str | None], dict[str, Any]] = {}
            for (source, doc_benchmark), files in grouped_files.items():
                provenance = next(
                    (
                        item.get("provenance", {})
                        for item in result.get("documents", [])
                        if item.get("source", result.get("effective_source")) == source
                    ),
                    {},
                )
                snapshot = manager.save_source_snapshot(
                    source, provenance, files, benchmark=doc_benchmark
                )
                saved_groups[(source, doc_benchmark)] = snapshot
                refs.extend(snapshot["files"].values())

            for document in result.get("documents", []):
                doc_benchmark = None
                if document["namespace"].startswith("benchmark/"):
                    _, doc_benchmark = document["namespace"].split("/", 1)
                elif document.get("source") == "local":
                    doc_benchmark = document.get("benchmark")
                source = document.get(
                    "source", result.get("effective_source", "github")
                )
                snapshot = saved_groups.get((source, doc_benchmark), {})
                workspace_ref = snapshot.get("files", {}).get(document["source_path"])
                if not workspace_ref:
                    continue
                document["workspace_ref"] = workspace_ref
                indexed_documents.append(dict(document))

            index_ref = manager.index_context_documents(indexed_documents)
            previous = manager.read_effective_context() or {}
            if previous.get("phase") == phase and previous.get(
                "effective_source"
            ) == result.get("effective_source"):
                refs = list(dict.fromkeys(previous.get("workspace_refs", []) + refs))
            manifest = {
                "schema_version": 1,
                "policy": "phase_owned_source_with_local_supplements",
                "namespace": namespace,
                "subject_area": subject_area,
                "documents": [
                    {
                        key: value
                        for key, value in document.items()
                        if key
                        not in {
                            "source",
                            "provenance",
                            "workspace_ref",
                            "source_path",
                            "content",
                        }
                    }
                    for document in result.get("documents", [])
                ],
            }
            result["workspace"] = {
                "effective_context": manager.save_effective_context(manifest),
                "effective_source": result.get("effective_source"),
                "document_index": index_ref,
            }
            if requested_operation == "read":
                workspace_document = manager.read_document(
                    path, include_alternates=include_alternates
                )
                if workspace_document.get("status") == "ok":
                    metadata = next(
                        (
                            item
                            for item in result.get("documents", [])
                            if item.get("workspace_ref")
                            == workspace_document.get("workspace_ref")
                        ),
                        {},
                    )
                    result["document"] = {
                        **metadata,
                        "content": workspace_document["content"],
                    }
            elif requested_operation == "search":
                result = {
                    **manager.search_documents(
                        query,
                        namespace=namespace if namespace != "all" else "",
                        include_alternates=include_alternates,
                    ),
                    "workspace": result["workspace"],
                    "effective_source": result.get("effective_source"),
                }
            elif requested_operation == "list":
                for document in result.get("documents", []):
                    document.pop("content", None)
        _emit_context_audit_event(
            ticket_id,
            agent_name="benchmark-agent",
            phase=phase,
            benchmark=benchmark,
            operation=requested_operation,
            namespace=namespace,
            result=result,
        )
        return json.dumps(
            _public_context_result(
                result,
                phase=phase,
                audience="benchmark-agent",
                benchmark=benchmark,
                namespace=namespace,
                subject_area=subject_area,
            )
        )
    result = await provider.get_benchmark_context(benchmark)
    ticket_id = os.environ.get("TICKET_ID", "")
    if result.get("found") and ticket_id:
        from providers.skills.crucible import select_crucible_context
        from providers.workspace.manager import WorkspaceManager

        manager = WorkspaceManager(
            ticket_id=ticket_id, agent_name="benchmark-agent", phase="benchmark"
        )
        refresh = await _refresh_controller_context(benchmark, manager)
        github_provenance = {
            key: result.get(key)
            for key in (
                "effective_source",
                "source_reason",
                "source_assumption",
                "repository",
                "ref",
                "commit",
            )
        }
        _ = manager.save_source_snapshot(
            "github",
            github_provenance,
            result.get("context", {}),
            benchmark=benchmark,
        )
        _, _ = manager.save_file(
            "context/sources/github/harnesses/crucible/source.json",
            json.dumps(github_provenance, indent=2) + "\n",
        )
        controller_snapshot = manager.load_source_snapshot("controller", benchmark)
        controller_snapshot_available = controller_snapshot is not None
        if controller_snapshot_available and not refresh.get("available"):
            refresh = {
                "available": True,
                "reason": "controller_snapshot_already_present",
                "provenance": controller_snapshot.get("provenance", {}),
            }
        cf = _ticket.get("custom_fields", {}) if _ticket else {}
        controller = cf.get("crucible_controller_context", {})
        controller = dict(controller) if isinstance(controller, dict) else {}
        controller.update(
            {
                "identified": controller.get("identified") is True
                or bool(_controller_host()),
                "reachable": controller.get("reachable") is True
                or refresh.get("reason") != "controller_not_identified",
                "crucible_installed": controller.get("crucible_installed") is True
                or refresh.get("reason")
                not in {"controller_not_identified", "crucible_not_installed"},
                "snapshot_available": controller_snapshot_available,
            }
        )
        controller["snapshot_available"] = controller_snapshot_available
        policy = cf.get("crucible_update_policy")
        if not policy:
            policy = cf.get("directives", {}).get("update_harness")
        selection = select_crucible_context(
            phase=phase, controller=controller, update_policy=policy
        )
        if selection["effective_source"] == "controller" and controller_snapshot:
            result["context"] = controller_snapshot["files"]
            result["files"] = list(controller_snapshot["files"])
            result["effective_source"] = "controller"
        manifest = {
            "schema_version": 1,
            "phase": "benchmark",
            "policy": "phase_owned_source_with_local_supplements",
            "documents": [],
        }
        result["workspace"] = {
            "effective_context": manager.save_effective_context(manifest),
            "effective_source": selection["effective_source"],
        }
    _emit_context_audit_event(
        ticket_id,
        agent_name="benchmark-agent",
        phase=phase,
        benchmark=benchmark,
        operation="context",
        namespace="all",
        result=result,
    )
    return json.dumps(
        _public_context_result(
            result,
            phase=phase,
            audience="benchmark-agent",
            benchmark=benchmark,
            namespace="all",
            subject_area="all",
        )
    )


async def get_crucible_benchmark_context(*args, **kwargs) -> str:
    """Compatibility wrapper for direct in-process callers only.

    This wrapper is intentionally not registered with MCP. Agents receive the
    smaller generic schema from ``_get_crucible_benchmark_context_tool``.
    """
    return await _legacy_get_crucible_benchmark_context(*args, **kwargs)


@mcp.tool()
async def get_tool_params(tool: str, harness: str = "crucible") -> str:
    """Get parameter definitions (multiplex.json) and metadata for a performance profiling tool (e.g. 'sysstat', 'procstat', 'ethtool', 'forkstat'). Returns presets (default arguments) and validations (regex patterns for allowed argument values) along with tool description and CDM metric info. Use this to construct valid 'tool-params' entries in the run-file."""
    await _ensure_init()
    harness_name = harness or "crucible"
    if harness_name == "crucible":
        return json.dumps(
            {
                "found": False,
                "tool": tool,
                "harness": "crucible",
                "message": (
                    "Crucible tool metadata is controller-sourced context. "
                    "Use get_crucible_benchmark_context to read the tool "
                    "metadata and parameter files."
                ),
            }
        )
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        params = await provider.get_tool_params(tool) if provider else None
        metadata = (
            await provider.get_tool_metadata(tool)
            if provider and hasattr(provider, "get_tool_metadata")
            else None
        )
    else:
        params = await _skill_provider.get_tool_params(tool)
        metadata = (
            await _skill_provider.get_tool_metadata(tool)
            if hasattr(_skill_provider, "get_tool_metadata")
            else None
        )

    if params is None and metadata is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No parameter definitions or metadata for tool '{tool}' in '{harness_name}'",
            }
        )

    response: dict[str, Any] = {
        "found": True,
        "tool": tool,
        "harness": harness_name,
    }
    if params is not None:
        response["params"] = params
    if metadata is not None:
        response["metadata"] = metadata
    return json.dumps(response)


@mcp.tool()
async def get_example_runfile(
    benchmark: str, harness: str = "crucible", endpoint_type: str = "remotehosts"
) -> str:
    """Get an example run-file for a benchmark. Use this as a structural reference when constructing your own run-file. The example shows the correct format for endpoints, mv-params, and benchmark configuration."""
    await _ensure_init()
    harness_name = harness or "crucible"
    ep_type = endpoint_type or "remotehosts"
    if harness_name == "crucible":
        return json.dumps(
            {
                "found": False,
                "benchmark": benchmark,
                "harness": "crucible",
                "endpoint_type": ep_type,
                "message": (
                    "Crucible examples are controller-sourced context. Use "
                    "get_crucible_benchmark_context to discover and read them."
                ),
            }
        )
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        example = (
            await provider.get_example_runfile(benchmark, endpoint_type=ep_type)
            if provider
            else None
        )
    else:
        example = await _skill_provider.get_example_runfile(
            benchmark, endpoint_type=ep_type
        )
    if example is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No example run-file for '{benchmark}' ({ep_type}) in '{harness_name}'",
            }
        )
    return json.dumps(
        {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "endpoint_type": ep_type,
            "run_file": example,
        }
    )


@mcp.tool()
async def setup_passwordless_ssh(
    source: str,
    targets: list[str],
    target_ssh_hosts: list[str] | None = None,
    ssh_user: str = "root",
) -> str:
    """Set up passwordless SSH from a source host to target hosts.

    Generates a key pair on the source if needed and copies the public
    key to each target's authorized_keys. Safe to call multiple times —
    existing keys are deduplicated by comment tag.

    In cloud environments, targets may only be reachable via public IPs
    from the agent machine, but the source host reaches them via private
    IPs. Pass target_ssh_hosts with the SSH-reachable (public) IPs for
    key injection, while targets contains the internal IPs that the
    source uses to connect after setup.
    """
    await _ensure_init()
    user = ssh_user
    ssh_hosts = target_ssh_hosts or targets
    if len(ssh_hosts) != len(targets):
        return json.dumps(
            {
                "status": "failed",
                "message": (
                    f"target_ssh_hosts length ({len(ssh_hosts)}) must match "
                    f"targets length ({len(targets)})"
                ),
            }
        )
    logger.info(f"[benchmark] Setting up SSH keys: {source} -> {targets}")

    pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub 2>/dev/null")
    if pubkey_result.exit_code != 0 or not pubkey_result.stdout.strip():
        keygen_result = await _ssh.run(
            source,
            f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
        )
        if keygen_result.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "message": f"Key generation failed: {keygen_result.stderr}",
                }
            )
        pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub")

    pubkey = pubkey_result.stdout.strip()
    if CONTROLLER_KEY_COMMENT not in pubkey:
        await _ssh.run(
            source,
            "rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && "
            f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
        )
        pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub")
        pubkey = pubkey_result.stdout.strip()

    results = {}

    for target, ssh_host in zip(targets, ssh_hosts):
        check = await _ssh.run(
            source,
            f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"{user}@{target} hostname",
        )
        if check.exit_code == 0:
            results[target] = {
                "status": "already_accessible",
                "hostname": check.stdout.strip(),
            }
            continue

        inject = await _ssh.run(
            ssh_host,
            f"mkdir -p /root/.ssh && "
            f'sed -i "/{CONTROLLER_KEY_COMMENT}/d" /root/.ssh/authorized_keys 2>/dev/null; '
            f'echo "{pubkey}" >> /root/.ssh/authorized_keys && '
            f"chmod 600 /root/.ssh/authorized_keys",
        )
        if inject.exit_code != 0:
            results[target] = {
                "status": "failed",
                "message": inject.stderr,
            }
            continue

        verify = await _ssh.run(
            source,
            f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"{user}@{target} hostname",
        )
        results[target] = {
            "status": "configured" if verify.exit_code == 0 else "failed",
            "hostname": verify.stdout.strip() if verify.exit_code == 0 else "",
            "message": verify.stderr if verify.exit_code != 0 else "",
        }

    all_ok = all(
        r["status"] in ("already_accessible", "configured") for r in results.values()
    )
    return json.dumps(
        {
            "status": "success" if all_ok else "partial_failure",
            "results": results,
            "message": "All targets accessible from source"
            if all_ok
            else "Some targets failed SSH setup",
        }
    )


@mcp.tool()
async def execute_benchmark(
    controller: str,
    validation_id: str | None = None,
    approval_request_id: str | None = None,
    harness: str | None = None,
    run_command: str | None = None,
    run_file: dict | None = None,
) -> str:
    """Execute a previously validated benchmark configuration.

    Crucible execution is intentionally token-based: the caller must provide
    the validation ID returned by ``validate_benchmark``.  The runfile is
    loaded from that ticket-bound record, so a caller cannot substitute a
    different runfile between validation and execution.  Other harnesses keep
    the legacy runfile argument until they gain the same validation contract.
    """
    await _ensure_init()

    from agents.server_utils import assert_ticket_active

    active_check = await assert_ticket_active(
        expected_status="executing_benchmark",
    )
    if active_check.get("status") == "rejected":
        # A terminal replay is safe even after the ticket has advanced or
        # paused. Resolve it before enforcing the current execution status so
        # reconnects can retrieve the durable result without relaunching.
        ticket_id = os.environ.get("TICKET_ID", "")
        fields = _ticket.get("custom_fields", {}) if _ticket else {}
        records = fields.get("benchmark_validations", {}).get("records", {})
        replay_record = records.get(validation_id, {}) if validation_id else {}
        if validation_id and isinstance(replay_record, dict):
            replay_key, replay_hash, _ = _benchmark_intent_identity(
                ticket_id,
                validation_id,
                replay_record,
                controller,
                harness or "crucible",
                replay_record.get("run_command", "crucible run"),
            )
            replay_guard = _BenchmarkOperation(
                replay_key,
                replay_hash,
                os.environ.get("AGENTIC_PERF_INSTANCE_NAME", socket.gethostname()),
            )
            try:
                replay_operation, replay_status = await replay_guard.acquire()
                if replay_status == "terminal":
                    descriptor = replay_operation.get("result_descriptor") or {}
                    cached = descriptor.get("benchmark_result") or descriptor.get(
                        "result"
                    )
                    if isinstance(cached, dict):
                        return json.dumps(
                            {
                                **cached,
                                "operation_id": replay_key,
                                "existing_operation": True,
                            }
                        )
            finally:
                await replay_guard.close()
        return json.dumps(active_check)
    if (
        active_check.get("id") or os.environ.get("TICKET_ID")
    ) and not approval_request_id:
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "approval_required",
                "message": "ticket-scoped benchmark execution requires approval_request_id",
            }
        )

    run_uuid = uuid.uuid4().hex[:8]
    harness_name = harness or "crucible"

    async def pause_for_reconciliation(reason: str) -> None:
        """Put ambiguous executions in the canonical human-guidance state."""
        ticket_id = active_check.get("id") or os.environ.get("TICKET_ID", "")
        if not ticket_id:
            return
        try:
            from providers.execution import AuditedAsyncHTTPClient

            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
                response = await client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                    json={
                        "status": "awaiting_customer_guidance",
                        "comment": f"Benchmark execution is indeterminate: {reason}",
                    },
                )
                if response.status_code >= 300:
                    logger.warning(
                        "Could not pause ticket %s for reconciliation", ticket_id
                    )
        except Exception:
            logger.exception("Failed to pause ticket %s for reconciliation", ticket_id)

    if harness_name == "crucible":
        if run_file is not None:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "message": (
                        "Crucible execution does not accept a runfile. "
                        "Call validate_benchmark and pass its validation_id."
                    ),
                }
            )
        if not validation_id:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "message": (
                        "Crucible execution requires validation_id from a "
                        "successful validate_benchmark call."
                    ),
                }
            )
        run_file, validation_error = _get_validated_runfile(
            validation_id,
            controller,
            harness_name,
            active_check,
        )
        if validation_error:
            reason_code = (
                "superseded"
                if "superseded" in validation_error
                else "legacy_unapproved"
                if "legacy" in validation_error
                else "fingerprint_mismatch"
                if "mismatched" in validation_error
                else "unknown"
            )
            rejection = {
                "status": "rejected",
                "harness": harness_name,
                "validation_id": validation_id,
                "reason_code": reason_code,
                "message": validation_error,
            }
            records = (
                active_check.get("custom_fields", {})
                .get("benchmark_validations", {})
                .get("records", {})
            )
            supersession = next(
                (
                    item
                    for item in records.values()
                    if isinstance(item, dict)
                    and item.get("supersedes_validation_id") == validation_id
                ),
                None,
            )
            if supersession:
                replacement_id = supersession.get("replacement_validation_id")
                replacement = records.get(replacement_id, {})
                rejection.update(
                    {
                        "invalidated_at": supersession.get("created_at"),
                        "invalidated_by": supersession.get("writer"),
                        "invalidation_reason": supersession.get("reason"),
                        "replacement_validation_id": replacement_id,
                        "replacement": {
                            key: replacement.get(key)
                            for key in (
                                "validation_id",
                                "harness",
                                "controller",
                                "runfile_fingerprint",
                            )
                        },
                    }
                )
            _emit_context_audit_event(
                os.environ.get("TICKET_ID", ""),
                agent_name="benchmark-agent",
                phase="validation_rejected",
                benchmark=str(run_file.get("benchmark", ""))
                if isinstance(run_file, dict)
                else "",
                operation=validation_id,
                namespace="benchmark-execution",
                result=rejection,
            )
            return json.dumps(rejection)
        record = active_check.get("custom_fields", {}).get(
            "benchmark_validations", {}
        ).get("records", {}).get(validation_id) or _validation_records.get(
            validation_id, {}
        )
        stored_command = record.get("run_command")
        if run_command is not None and run_command != stored_command:
            return json.dumps(
                {
                    "status": "rejected",
                    "validation_id": validation_id,
                    "reason_code": "execution_intent_mismatch",
                    "message": "run_command differs from approved execution intent",
                }
            )
        run_command = stored_command
    elif _skill_provider:
        if run_file is None:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "message": "A runfile is required for this harness",
                }
            )
        validation = await _skill_provider.validate_runfile(run_file, harness_name)
        if not validation.get("valid", True):
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "message": (
                        "Run-file failed schema validation: "
                        f"{validation.get('errors', [])}"
                    ),
                }
            )

    if run_file is None:
        return json.dumps(
            {
                "status": "rejected",
                "harness": harness_name,
                "message": "A runfile is required for this harness",
            }
        )

    if run_command is not None:
        valid, reason = _validate_run_command(run_command, harness_name)
        if not valid:
            logger.warning("[benchmark] run_command rejected: %s", reason)
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "message": (
                        f"run_command rejected: {reason}. "
                        f"Only known harness binaries are allowed."
                    ),
                }
            )

    # Claim the durable intent before consuming the one-shot approval. A
    # reconnect that already consumed approval must still receive its cached
    # operation result rather than failing approval binding with 409.
    operation_guard: _BenchmarkOperation | None = None
    operation_record: dict[str, Any] | None = None
    operation_owned = False
    if validation_id and approval_request_id:
        ticket_id = os.environ.get("TICKET_ID", "")
        preclaim_record = record
        intent_key, intent_hash, immutable_intent = _benchmark_intent_identity(
            ticket_id,
            validation_id,
            preclaim_record,
            controller,
            harness_name,
            run_command or "crucible run",
        )
        operation_guard = _BenchmarkOperation(
            intent_key,
            intent_hash,
            os.environ.get("AGENTIC_PERF_INSTANCE_NAME", socket.gethostname()),
        )
        operation_record, acquisition = await operation_guard.acquire()
        if acquisition == "terminal":
            cached = (operation_record.get("result_descriptor") or {}).get(
                "benchmark_result"
            )
            await operation_guard.close()
            if isinstance(cached, dict):
                _emit_context_audit_event(
                    ticket_id,
                    agent_name="benchmark-agent",
                    phase="duplicate",
                    benchmark=str(run_file.get("benchmark", "")),
                    operation=intent_key,
                    namespace="benchmark-execution",
                    result={"status": "existing_operation"},
                )
                return json.dumps(
                    {**cached, "operation_id": intent_key, "existing_operation": True}
                )
            _emit_context_audit_event(
                ticket_id,
                agent_name="benchmark-agent",
                phase="duplicate",
                benchmark=str(run_file.get("benchmark", "")),
                operation=intent_key,
                namespace="benchmark-execution",
                result={"status": "indeterminate"},
            )
            return json.dumps(
                {
                    "status": operation_record.get("terminal_outcome", "indeterminate"),
                    "reason_code": operation_record.get(
                        "terminal_outcome", "indeterminate"
                    ),
                    "operation_id": intent_key,
                    "operation": operation_record,
                    "existing_operation": True,
                }
            )
        if acquisition != "acquired":
            await operation_guard.close()
            _emit_context_audit_event(
                ticket_id,
                agent_name="benchmark-agent",
                phase="duplicate",
                benchmark=str(run_file.get("benchmark", "")),
                operation=intent_key,
                namespace="benchmark-execution",
                result={"status": "existing_operation"},
            )
            return json.dumps(
                {
                    "status": "rejected",
                    "reason_code": "operation_registry_unavailable"
                    if acquisition == "unavailable"
                    else "existing_operation",
                    "operation_id": intent_key,
                }
            )
        operation_owned = True
        try:
            await operation_guard.transition(
                "prepared", operation_record, descriptor={"intent": immutable_intent}
            )
        except Exception:
            await operation_guard.terminalize(
                operation_record, "indeterminate", {"outcome": "indeterminate"}
            )
            await operation_guard.close()
            await pause_for_reconciliation("execution intent preparation failed")
            return json.dumps({"status": "indeterminate", "operation_id": intent_key})

    if approval_request_id:
        # #798 owns the approval capability boundary.  #788 may add richer
        # operation claims later; this optional token is deliberately checked
        # immediately before any benchmark-side effect.
        ticket_id = active_check.get("id") or os.environ.get("TICKET_ID", "")
        record = (
            (
                active_check.get("custom_fields", {})
                .get("benchmark_validations", {})
                .get("records", {})
                .get(validation_id, {})
            )
            if validation_id
            else {}
        )
        if not ticket_id or not validation_id or not isinstance(record, dict):
            if operation_guard and operation_owned:
                await operation_guard.terminalize(
                    operation_record, "fail", {"outcome": "approval_binding_missing"}
                )
                await operation_guard.close()
            return json.dumps(
                {"status": "rejected", "reason_code": "approval_binding_missing"}
            )
        try:
            from providers.execution import AuditedAsyncHTTPClient

            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            from agents.server_utils import ticket_state_headers

            headers = ticket_state_headers()
            async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
                consumed = await client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/approvals/{approval_request_id}/consume",
                    json={
                        "validation_id": validation_id,
                        "presented_run_file_digest": record.get("runfile_fingerprint"),
                        "execution_intent_digest": record.get(
                            "execution_intent_digest"
                        ),
                        "session_id": os.environ.get(
                            "AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"
                        ),
                        "session_epoch": os.environ.get(
                            "AGENTIC_PERF_ORCHESTRATOR_EPOCH"
                        ),
                        "ticket_attempt": (
                            os.environ.get("AGENTIC_PERF_CLAIM_ID")
                            or (
                                active_check.get("custom_fields", {}).get("claim") or {}
                            ).get("claim_id")
                        ),
                        "claim_id": (
                            os.environ.get("AGENTIC_PERF_CLAIM_ID")
                            or (
                                active_check.get("custom_fields", {}).get("claim") or {}
                            ).get("claim_id")
                        ),
                    },
                )
            if consumed.status_code >= 300:
                if operation_guard and operation_owned:
                    await operation_guard.terminalize(
                        operation_record, "fail", {"outcome": "rejected"}
                    )
                    await operation_guard.close()
                return json.dumps(
                    {"status": "rejected", "reason_code": "approval_not_approved"}
                )
        except Exception:
            logger.exception("[benchmark] approval capability consumption failed")
            if operation_guard and operation_owned:
                await operation_guard.terminalize(
                    operation_record, "fail", {"outcome": "rejected"}
                )
                await operation_guard.close()
            return json.dumps(
                {"status": "rejected", "reason_code": "approval_check_failed"}
            )

    async def _benchmark_progress(output_line: str, elapsed: int) -> None:
        minutes = elapsed // 60
        await tool_progress(
            f"[{minutes}m] {output_line}",
            "execute_benchmark",
        )

    # The validated Crucible path below is the only path with a durable
    # validation lifecycle and controller-side reconciliation identity.  The
    # legacy harness branches still perform side effects directly; they are
    # intentionally not advertised as idempotent until their own validation
    # records and safe external identity queries exist.  MCP middleware still
    # prevents duplicate delivery, but cannot safely replay a lost launch.
    if harness_name == "kube-burner":
        try:
            import yaml

            yaml_dump = yaml.dump
        except ImportError:
            yaml_dump = None

        config = run_file.get("config", {})
        templates = run_file.get("templates", {})

        template_dir = f"/tmp/kb-{run_uuid}"
        config_path = f"{template_dir}/config.yml"

        await _ssh.run(controller, f"mkdir -p {template_dir}")

        if yaml_dump:
            config_content = yaml_dump(config, default_flow_style=False)
        else:
            config_content = json.dumps(config, indent=2)

        await _ssh.run(
            controller,
            f"cat > {config_path} << 'KBEOF'\n{config_content}\nKBEOF",
        )

        for tpl_name, tpl_content in templates.items():
            tpl_path = f"{template_dir}/{tpl_name}"
            await _ssh.run(
                controller,
                f"cat > {tpl_path} << 'KBEOF'\n{tpl_content}\nKBEOF",
            )

        kb_cmd = run_command or "kube-burner init"
        cmd = f"cd {template_dir} && {kb_cmd} -c {config_path} --uuid {run_uuid} 2>&1"
        logger.info(f"[benchmark] Executing kube-burner: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"kube-burner-{run_uuid}",
            "harness": "kube-burner",
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0:
            metrics_result = await _ssh.run(
                controller,
                f"cat {template_dir}/collected-metrics/*.json 2>/dev/null | head -c 3000",
                timeout=30,
            )
            if metrics_result.exit_code == 0 and metrics_result.stdout:
                try:
                    response["result_summary"] = json.loads(metrics_result.stdout)
                except json.JSONDecodeError:
                    response["result_summary"] = metrics_result.stdout[:3000]
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "benchmark-runner":
        env_vars = dict(run_file.get("env_vars", {}))
        container_image = run_file.get(
            "container_image", "quay.io/benchmark-runner/benchmark-runner:latest"
        )
        artifacts_dir = run_file.get(
            "artifacts_dir", "/tmp/benchmark-runner-run-artifacts"
        )
        kubeconfig_path = run_file.get("kubeconfig_path", "/root/.kube/config")

        if "KUBEADMIN_PASSWORD" not in env_vars:
            password_path = run_file.get("kubeadmin_password_path", "")
            if password_path:
                pw_result = await _ssh.run(
                    controller, f"cat {password_path} 2>/dev/null"
                )
                if pw_result.exit_code == 0 and pw_result.stdout.strip():
                    env_vars["KUBEADMIN_PASSWORD"] = pw_result.stdout.strip()

        for k, v in env_vars.items():
            if "\n" in k or "\r" in k or "\n" in v or "\r" in v:
                return json.dumps(
                    {
                        "status": "rejected",
                        "harness": "benchmark-runner",
                        "message": (
                            f"env var {k!r} contains newline/CR — "
                            "rejected to prevent env-file injection"
                        ),
                    }
                )

        env_tmpdir_result = await _ssh.run(
            controller,
            "mktemp -d /tmp/.benchmark-env-XXXXXXXX",
            timeout=10,
        )
        if env_tmpdir_result.exit_code != 0 or not env_tmpdir_result.stdout.strip():
            return json.dumps(
                {
                    "status": "failed",
                    "harness": "benchmark-runner",
                    "message": "Failed to create secure env temp dir on controller",
                }
            )
        env_dir = env_tmpdir_result.stdout.strip()
        env_file_path = f"{env_dir}/env"

        env_file_content = "\n".join(f"{k}={v}" for k, v in env_vars.items())
        write_result = await _ssh.run(
            controller,
            f"cat > {env_file_path} && chmod 600 {env_file_path}",
            stdin_data=env_file_content.encode(),
        )
        if write_result.exit_code != 0:
            await _ssh.run(controller, f"rm -rf {env_dir}")
            return json.dumps(
                {
                    "status": "failed",
                    "harness": "benchmark-runner",
                    "message": "Failed to write env file on controller",
                }
            )

        await _ssh.run(controller, f"mkdir -p {artifacts_dir}")

        cmd = (
            f"podman run --rm --env-file {env_file_path} "
            f"-v {kubeconfig_path}:/root/.kube/config "
            f"-v {artifacts_dir}:{artifacts_dir} "
            f"--privileged "
            f"{container_image} 2>&1"
        )
        logger.info(
            "[benchmark] Executing benchmark-runner: %s (env-file: %d vars)",
            container_image,
            len(env_vars),
        )
        try:
            result = await _ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )
        finally:
            await _ssh.run(controller, f"rm -rf {env_dir}")

        artifacts_cmd = f"ls {artifacts_dir}/ 2>/dev/null | tail -1"
        artifacts_result = await _ssh.run(controller, artifacts_cmd)
        run_dir = (
            artifacts_result.stdout.strip() if artifacts_result.exit_code == 0 else ""
        )

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"benchmark-runner-{run_uuid}",
            "run_dir": f"{artifacts_dir}/{run_dir}" if run_dir else "",
            "harness": "benchmark-runner",
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0 and run_dir:
            full_dir = f"{artifacts_dir}/{run_dir}"
            ls_result = await _ssh.run(
                controller,
                f"ls -la {full_dir}/ 2>/dev/null | head -30",
                timeout=30,
            )
            if ls_result.exit_code == 0 and ls_result.stdout:
                response["result_summary"] = ls_result.stdout.strip()
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "zathras":
        scenario = run_file.get("scenario", {})
        if not scenario and ("global" in run_file or "systems" in run_file):
            scenario = {
                k: v
                for k, v in run_file.items()
                if k not in ("harness", "local_config", "host_config_name", "tags")
            }
        local_config = run_file.get("local_config")
        host_config_name = run_file.get("host_config_name", "")

        if local_config and host_config_name:
            config_content = "\n".join(f"{k}: {v}" for k, v in local_config.items())
            await _ssh.run(
                controller,
                f"mkdir -p /opt/zathras/local_configs && cat > /opt/zathras/local_configs/{host_config_name}.config << 'ZEOF'\n{config_content}\nZEOF",
            )

        ZATHRAS_NO_ARG_FLAGS = {
            "no_clean_up",
            "no_packages",
            "no_pip_packages",
            "no_system_packages",
            "no_spot_recover",
            "persistent_log",
            "preflight_check",
            "run_chronicler",
            "run_chronicler_strict",
            "skip_test_version_check",
            "ignore_repo_errors",
            "create_only",
            "force_upload",
            "verbose",
        }
        for section in ("global", "systems"):
            if section not in scenario:
                continue
            if section == "global":
                items = scenario["global"]
                for key in list(items.keys()):
                    if key in ZATHRAS_NO_ARG_FLAGS and items[key] in (
                        True,
                        "true",
                        "True",
                        "yes",
                    ):
                        items[key] = ""
                    if (
                        key == "ssh_key_file"
                        and isinstance(items[key], str)
                        and items[key].startswith("~")
                    ):
                        items[key] = "/root" + items[key][1:]
            else:
                for sys_name, sys_conf in scenario["systems"].items():
                    if not isinstance(sys_conf, dict):
                        continue
                    if (
                        "ssh_key_file" in sys_conf
                        and isinstance(sys_conf["ssh_key_file"], str)
                        and sys_conf["ssh_key_file"].startswith("~")
                    ):
                        sys_conf["ssh_key_file"] = (
                            "/root" + sys_conf["ssh_key_file"][1:]
                        )

        try:
            import yaml

            scenario_yaml = yaml.dump(scenario, default_flow_style=False)
        except ImportError:
            scenario_yaml = json.dumps(scenario, indent=2)

        scenario_path = f"/tmp/scenario-{run_uuid}.yml"
        await _ssh.run(
            controller,
            f"cat > {scenario_path} << 'ZEOF'\n{scenario_yaml}\nZEOF",
        )

        burden_cmd = run_command or "/opt/zathras/bin/burden"

        preflight_cmd = f"cd /opt/zathras && {burden_cmd} --preflight_check --scenario {scenario_path}"
        logger.info(f"[benchmark] Running zathras preflight: {preflight_cmd}")
        preflight = await _ssh.run(controller, preflight_cmd, timeout=120)
        if preflight.exit_code != 0:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": "zathras",
                    "message": (
                        "Scenario failed zathras preflight_check and was NOT executed. "
                        "Fix the scenario and try again.\n"
                        + (preflight.stdout or "")
                        + (preflight.stderr or "")
                    ),
                }
            )

        cmd = f"cd /opt/zathras && {burden_cmd} --scenario {scenario_path}"
        logger.info(f"[benchmark] Executing zathras: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        run_dir = ""
        run_dir_re = re.compile(r"Results stored in:\s*(\S+)")
        for line in result.stdout.split("\n"):
            m = run_dir_re.search(line)
            if m:
                run_dir = m.group(1)
                break

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_dir": run_dir,
            "run_id": run_dir.rstrip("/").split("/")[-1]
            if run_dir
            else f"zathras-{run_uuid}",
            "harness": "zathras",
            "message": "Benchmark completed"
            if result.exit_code == 0
            else f"Benchmark failed (exit {result.exit_code})",
        }
        if result.exit_code == 0 and run_dir:
            ls_result = await _ssh.run(
                controller,
                f"ls -la {run_dir}/ 2>/dev/null | head -30",
                timeout=30,
            )
            if ls_result.exit_code == 0 and ls_result.stdout:
                response["result_summary"] = ls_result.stdout.strip()
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "ioscale":
        try:
            import yaml

            yaml_dump = yaml.dump
        except ImportError:
            yaml_dump = None

        test_type = run_file.get("test_type", "fio")
        vm_config = run_file.get("vm_config", {})
        test_config = run_file.get("test_config", {})
        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

        template_dir = f"/tmp/ioscale-{run_uuid}"
        await _ssh.run(controller, f"mkdir -p {template_dir}")

        kc = f"KUBECONFIG={kubeconfig}"

        storage_class = vm_config.get("storage_class", "")
        if not storage_class:
            sc_result = await _ssh.run(
                controller,
                f"{kc} oc get sc -o jsonpath='{{.items[0].metadata.name}}'",
            )
            storage_class = sc_result.stdout.strip()
            if not storage_class:
                return json.dumps(
                    {
                        "status": "failed",
                        "harness": "ioscale",
                        "message": "No StorageClass found on cluster",
                    }
                )

        vm_name = f"ioscale-vm-{run_uuid}"
        ns = "default"
        cores = vm_config.get("cores", 4)
        memory = vm_config.get("memory", "8Gi")
        storage_size = vm_config.get("storage_size", "100Gi")
        vm_config.get(
            "image_url",
            "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/"
            "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2",
        )

        await _ssh.run(
            controller,
            f"{kc} ssh-keygen -t rsa -f {template_dir}/vm-key -N '' -q 2>/dev/null;"
            f" {kc} oc create secret generic vmkeyroot"
            f" --from-file=key={template_dir}/vm-key.pub"
            f" -n {ns} --dry-run=client -o yaml | {kc} oc apply -f -",
        )

        tpl_file = "geniotest.yml" if test_type == "fio" else "vmdbtest.yml"
        data_dv = f"data-{run_uuid}"
        vm_yaml_cmd = (
            f"sed"
            f" -e 's/ocs-storagecluster-ceph-rbd/{storage_class}/g'"
            f" -e 's/vm-test-io/{vm_name}/g'"
            f" -e 's/vm-test-db/{vm_name}/g'"
            f" -e 's/dataiotest/{data_dv}/g'"
            f" -e 's/datavolumedb/{data_dv}/g'"
            f" -e 's/vm-testvm/{vm_name}/g'"
            f" -e 's/vm-dataiotest/vm-{data_dv}/g'"
            f" -e 's/vm-datavolumedb/vm-{data_dv}/g'"
            f" -e 's/storage: 100Gi/storage: {storage_size}/g'"
            f" -e 's/cores: 4/cores: {cores}/g'"
            f" -e 's/sockets: 2/sockets: 1/g'"
            f" -e 's/memory: 8Gi/memory: {memory}/g'"
            f" /opt/ioscale/templates/{tpl_file}"
            f" > {template_dir}/vm.yaml"
        )
        await _ssh.run(controller, vm_yaml_cmd)

        logger.info(f"[benchmark] Creating ioscale VM: {vm_name}")
        await _ssh.run(
            controller,
            f"{kc} oc apply -f {template_dir}/vm.yaml -n {ns}",
        )

        for i in range(60):
            check = await _ssh.run(
                controller,
                f"{kc} oc get vmi {vm_name} -n {ns}"
                f" -o jsonpath='{{.status.phase}}' 2>/dev/null",
            )
            if check.stdout.strip() == "Running":
                break
            await _ssh.run(controller, "sleep 10")
        else:
            return json.dumps(
                {
                    "status": "failed",
                    "harness": "ioscale",
                    "message": f"VM {vm_name} did not reach Running in 10 minutes",
                }
            )

        vm_ip_result = await _ssh.run(
            controller,
            f"{kc} oc get vmi {vm_name} -n {ns}"
            f" -o jsonpath='{{.status.interfaces[0].ipAddress}}'",
        )
        vm_ip = vm_ip_result.stdout.strip()

        if test_type == "fio":
            fio_cfg = test_config.get("fio", {})
            config_dict = {
                "vm": {"hosts": vm_name, "namespace": ns},
                "storage": {
                    "devices": {vm_name: "vdc"},
                    "mount_point": "/root/tests/data",
                    "filesystem": "xfs",
                },
                "fio": {
                    "test_size": fio_cfg.get("test_size", "1G"),
                    "runtime": fio_cfg.get("runtime", 300),
                    "block_sizes": fio_cfg.get("block_sizes", "4k"),
                    "io_patterns": fio_cfg.get("io_patterns", "randread"),
                    "numjobs": fio_cfg.get("numjobs", 1),
                    "iodepth": fio_cfg.get("iodepth", 16),
                    "direct_io": fio_cfg.get("direct_io", 1),
                },
                "output": {
                    "directory": f"/root/fio-results-{run_uuid}",
                    "format": "json+",
                },
                "retry": {"interval": 30, "max_retries": 10},
                "monitoring": {"task_monitor_interval": 60},
                "migrate": {"workloads": "", "interval": 0},
            }
            config_path = f"{template_dir}/fio-config.yaml"
            if yaml_dump:
                content = yaml_dump(config_dict, default_flow_style=False)
            else:
                content = json.dumps(config_dict, indent=2)
            await _ssh.run(
                controller,
                f"cat > {config_path} << 'IOEOF'\n{content}\nIOEOF",
            )
            cmd = (
                f"cd /opt/ioscale/io-generic && "
                f"{kc} python3 fio-tests.py -c {config_path} --yes-i-mean-it 2>&1"
            )
        else:
            db_cfg = test_config.get("database", {})
            config_dict = {
                "description": f"ioscale {test_type} benchmark",
                "storage": {
                    "mount_point": "/perf1",
                    "disk_list": "/dev/vdc",
                    "persistent": False,
                },
                "database": {
                    "hosts": vm_name,
                    "namespace": ns,
                    "warehouse_count": db_cfg.get("warehouse_count", 50),
                    "test_duration": db_cfg.get("test_duration", 15),
                },
                "test": {
                    "user_count": db_cfg.get("user_count", "1 5 10"),
                    "log_level": "INFO",
                },
                "retry": {"interval": 30, "max_retries": 10},
                "monitoring": {"task_monitor_interval": 60},
                "migrate": {"user_counts": "", "interval": 0},
            }
            config_path = f"{template_dir}/{test_type}-config.yaml"
            if yaml_dump:
                content = yaml_dump(config_dict, default_flow_style=False)
            else:
                content = json.dumps(config_dict, indent=2)
            await _ssh.run(
                controller,
                f"cat > {config_path} << 'IOEOF'\n{content}\nIOEOF",
            )
            cmd = (
                f"cd /opt/ioscale/db/{test_type} && "
                f"{kc} python3 {test_type}.py -c {config_path} 2>&1"
            )

        logger.info(f"[benchmark] Executing ioscale {test_type}: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"ioscale-{run_uuid}",
            "harness": "ioscale",
            "vm_name": vm_name,
            "vm_ip": vm_ip,
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0 and result.stdout:
            try:
                response["result_summary"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                response["result_summary"] = result.stdout[:3000]
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "vstorm":
        cli_args = run_file.get("cli_args", [])
        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

        args_str = " ".join(cli_args)
        vs_cmd = run_command or "/opt/vstorm/vstorm"
        cmd = f"KUBECONFIG={kubeconfig} {vs_cmd} {args_str} 2>&1"
        logger.info(f"[benchmark] Executing vstorm: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        batch_id = ""
        for line in (result.stdout or "").split("\n"):
            if "batch" in line.lower():
                import re as _re

                m = _re.search(r"[0-9a-f]{6}", line)
                if m:
                    batch_id = m.group(0)
                    break

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"vstorm-{batch_id or run_uuid}",
            "harness": "vstorm",
            "batch_id": batch_id,
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0 and result.stdout:
            try:
                response["result_summary"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                response["result_summary"] = result.stdout[:3000]
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "forge":
        project = run_file.get("project", "rhaiis")
        presets = run_file.get("presets", [])
        cli_args = run_file.get("cli_args", [])
        run_file.get("config_overrides", {})
        artifacts_dir = run_file.get(
            "artifacts_dir", f"/tmp/forge-artifacts-{run_uuid}"
        )
        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

        forge_cmd = run_command or "/opt/forge/bin/run_cli"
        preset_flags = " ".join(f"--preset {p}" for p in presets)
        args_str = " ".join(cli_args)

        env_prefix = f"KUBECONFIG={kubeconfig} ARTIFACT_DIR={artifacts_dir}"

        await _ssh.run(controller, f"mkdir -p {artifacts_dir}")

        prep_cmd = f"cd /opt/forge && {env_prefix} {forge_cmd} {project} {preset_flags} prepare 2>&1"
        logger.info(f"[benchmark] Forge prepare: {prep_cmd}")
        prep_result = await _ssh.run_with_progress(
            controller,
            prep_cmd,
            progress_callback=_benchmark_progress,
        )

        if prep_result.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": prep_result.exit_code,
                    "phase": "prepare",
                    "run_id": f"forge-{run_uuid}",
                    "harness": "forge",
                    "project": project,
                    "output": prep_result.stdout[-3000:] if prep_result.stdout else "",
                    "error": prep_result.stderr[-1000:] if prep_result.stderr else "",
                    "message": f"Forge prepare failed (exit {prep_result.exit_code})",
                }
            )

        test_cmd = (
            f"cd /opt/forge && {env_prefix} {forge_cmd} {project} {preset_flags} test"
            f"{' ' + args_str if args_str else ''} 2>&1"
        )
        logger.info(f"[benchmark] Forge test: {test_cmd}")
        result = await _ssh.run_with_progress(
            controller,
            test_cmd,
            progress_callback=_benchmark_progress,
        )

        ai_eval = "{}"
        eval_cmd = (
            f"find {artifacts_dir} -name ai_eval_payload.json"
            f" -exec cat {{}} \\; 2>/dev/null | head -1"
        )
        eval_result = await _ssh.run(controller, eval_cmd)
        if eval_result.exit_code == 0 and eval_result.stdout.strip():
            ai_eval = eval_result.stdout.strip()

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"forge-{run_uuid}",
            "harness": "forge",
            "project": project,
            "artifacts_dir": artifacts_dir,
            "ai_eval_payload": ai_eval,
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "clusterbuster":
        try:
            import yaml

            yaml_dump = yaml.dump
        except ImportError:
            yaml_dump = None

        job_file = run_file.get("job_file", {})
        template_dir = f"/tmp/clusterbuster-{run_uuid}"
        job_path = f"{template_dir}/job.yaml"

        await _ssh.run(controller, f"mkdir -p {template_dir}")

        if yaml_dump:
            job_content = yaml_dump(job_file, default_flow_style=False)
        else:
            job_content = json.dumps(job_file, indent=2)

        await _ssh.run(
            controller,
            f"cat > {job_path} << 'CBEOF'\n{job_content}\nCBEOF",
        )

        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")
        cb_cmd = run_command or "clusterbuster"
        cmd = f"KUBECONFIG={kubeconfig} {cb_cmd} -f {job_path} 2>&1"
        logger.info(f"[benchmark] Executing clusterbuster: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"clusterbuster-{run_uuid}",
            "harness": "clusterbuster",
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0 and result.stdout:
            try:
                response["result_summary"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                response["result_summary"] = result.stdout[:3000]
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "k8s-netperf":
        config = run_file.get("config", {})
        cli_flags = run_file.get("cli_flags", [])

        template_dir = f"/tmp/k8s-netperf-{run_uuid}"
        config_path = f"{template_dir}/netperf.yml"

        await _ssh.run(controller, f"mkdir -p {template_dir}")

        # v1 flat-dict YAML: k8s-netperf's Go yaml.v3 parser
        # expects {testName: Config} not {tests: [{testName: Config}]}
        tests = config.get("tests", config)
        lines = ["---"]
        if isinstance(tests, list):
            for test in tests:
                if isinstance(test, dict):
                    for name, params in test.items():
                        lines.append(f"{name}:")
                        if isinstance(params, dict):
                            for k, v in params.items():
                                lines.append(f"  {k}: {json.dumps(v)}")
        elif isinstance(tests, dict):
            for name, params in tests.items():
                lines.append(f"{name}:")
                if isinstance(params, dict):
                    for k, v in params.items():
                        lines.append(f"  {k}: {json.dumps(v)}")
        config_content = "\n".join(lines)

        await _ssh.run(
            controller,
            f"cat > {config_path} << 'NPEOF'\n{config_content}\nNPEOF",
        )

        setup_cmds = [
            "kubectl create ns netperf --dry-run=client -o yaml | kubectl apply -f -",
            "kubectl create sa netperf -n netperf --dry-run=client -o yaml | kubectl apply -f -",
            "kubectl label node --all node-role.kubernetes.io/worker= --overwrite",
            "kubectl delete ns netperf --wait=true --ignore-not-found",
            "kubectl create ns netperf",
            "kubectl create sa netperf -n netperf",
        ]
        for setup_cmd in setup_cmds:
            await _ssh.run(controller, setup_cmd, timeout=60)

        flags_str = " ".join(cli_flags)
        np_cmd = run_command or "k8s-netperf"
        cmd = f"{np_cmd} --config {config_path} {flags_str} --json 2>&1"
        logger.info(f"[benchmark] Executing k8s-netperf: {cmd}")
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_id": f"k8s-netperf-{run_uuid}",
            "harness": "k8s-netperf",
            "message": (
                "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code == 0 and result.stdout:
            try:
                response["result_summary"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                response["result_summary"] = result.stdout[:3000]
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

    if harness_name == "arcaflow-plugins":
        import asyncio as _asyncio
        import shutil

        plugin_image = run_file.get("plugin_image", "")
        plugin_input = run_file.get("input", {})
        # Accept key aliases — the LLM uses various
        # names for the step parameter.
        plugin_step = (
            run_file.get("plugin_step")
            or run_file.get("step")
            or run_file.get("step_name")
            or "workload"
        )

        if not plugin_image:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "harness": "arcaflow-plugins",
                    "output": "",
                    "error": "No plugin_image specified in run file",
                    "message": "Missing plugin_image",
                }
            )

        image_valid, image_error = _validate_plugin_image(plugin_image)
        if not image_valid:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "output": "",
                    "error": image_error,
                    "message": "Invalid Arcaflow plugin image",
                }
            )

        # Determine if we can run locally (no SSH needed)
        is_local = controller in ("localhost", "127.0.0.1", "::1")

        # Serialize input as YAML if pyyaml available, else JSON
        try:
            import yaml

            input_content = yaml.dump(plugin_input, default_flow_style=False)
        except ImportError:
            input_content = json.dumps(plugin_input, indent=2)

        # Build container args: optional -s step, then -f - for stdin
        container_args = []
        container_args += ["-s", plugin_step]
        container_args += ["-f", "-"]

        if is_local:
            logger.info(f"[benchmark] Local execution: podman run {plugin_image}")
            podman_path = shutil.which("podman")
            if not podman_path:
                return json.dumps(
                    {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": "podman not found locally",
                        "message": "Arcaflow plugins require podman",
                    }
                )

            proc = await AuditedSubprocessRunner().start(
                [
                    podman_path,
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    plugin_image,
                    *container_args,
                ],
                stdin=input_content.encode(),
                mutating=True,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            exit_code = proc.returncode or 0
            stdout_str = stdout_bytes.decode(errors="replace")
            stderr_str = stderr_bytes.decode(errors="replace")
        else:
            podman_check = await _ssh.run(controller, "which podman", timeout=10)
            if podman_check.exit_code != 0:
                return json.dumps(
                    {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": "podman not found on target host",
                        "message": "Arcaflow plugins require podman on the target host",
                    }
                )

            input_path = f"/tmp/arcaflow-input-{run_uuid}.yaml"
            await _ssh.run(
                controller,
                f"cat > {input_path} << 'ARCAEOF'\n{input_content}\nARCAEOF",
            )

            podman_args = [
                "podman",
                "run",
                "-i",
                "--rm",
                plugin_image,
                "-s",
                plugin_step,
                "-f",
                "-",
            ]
            cmd = f"cat {shlex.quote(input_path)} | {shlex.join(podman_args)} 2>&1"
            logger.info(f"[benchmark] Executing Arcaflow plugin via SSH: {cmd}")
            result = await _ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )
            exit_code = result.exit_code
            stdout_str = result.stdout or ""
            stderr_str = result.stderr or ""

            await _ssh.run(controller, f"rm -f {input_path}", timeout=10)

        response = {
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "run_id": f"arcaflow-{run_uuid}",
            "harness": "arcaflow-plugins",
            "plugin_image": plugin_image,
            "execution_mode": "local" if is_local else "ssh",
            "message": (
                "Arcaflow plugin completed"
                if exit_code == 0
                else f"Arcaflow plugin failed (exit {exit_code})"
            ),
        }
        if exit_code == 0 and stdout_str:
            try:
                response["result_summary"] = json.loads(stdout_str)
            except json.JSONDecodeError:
                try:
                    import yaml

                    response["result_summary"] = yaml.safe_load(stdout_str)
                except Exception:
                    response["result_summary"] = stdout_str[:3000]
        if exit_code != 0:
            response["output"] = stdout_str[-3000:] if stdout_str else ""
            response["error"] = stderr_str[-1000:] if stderr_str else ""
        return json.dumps(response)

    # Default: crucible (and any unknown harness that uses JSON run-files)
    ticket_id = os.environ.get("TICKET_ID", "")
    if validation_id and operation_guard is None:
        intent_key, intent_hash, immutable_intent = _benchmark_intent_identity(
            ticket_id,
            validation_id,
            record,
            controller,
            harness_name,
            run_command or "crucible run",
        )
        operation_guard = _BenchmarkOperation(
            intent_key,
            intent_hash,
            os.environ.get("AGENTIC_PERF_INSTANCE_NAME", socket.gethostname()),
        )
        operation_record, acquisition = await operation_guard.acquire()
        if acquisition == "terminal":
            cached = operation_record.get("result_descriptor") or {}
            duplicate_result = (
                cached.get("benchmark_result")
                or cached.get("result")
                or {
                    "status": operation_record.get("terminal_outcome", "success"),
                    "operation_id": intent_key,
                    "existing_operation": True,
                }
            )
            if isinstance(duplicate_result, dict):
                duplicate_result = {
                    **duplicate_result,
                    "operation_id": intent_key,
                    "existing_operation": True,
                }
            _emit_context_audit_event(
                ticket_id,
                agent_name="benchmark-agent",
                phase="duplicate",
                benchmark=str(run_file.get("benchmark", "")),
                operation=intent_key,
                namespace="benchmark-execution",
                result={"status": "existing_operation"},
            )
            await operation_guard.close()
            return json.dumps(duplicate_result)
        if acquisition != "acquired":
            await operation_guard.close()
            if acquisition == "unavailable":
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason_code": "operation_registry_unavailable",
                        "message": "Benchmark operation registry unavailable; launch was not attempted",
                    }
                )
            return json.dumps(
                {
                    "status": "existing_operation",
                    "operation_id": intent_key,
                    "operation": operation_record,
                }
            )
        operation_owned = True
        try:
            await operation_guard.transition(
                "prepared", operation_record, descriptor={"intent": immutable_intent}
            )
        except Exception:
            await operation_guard.terminalize(
                operation_record,
                "indeterminate",
                {"outcome": "indeterminate", "prepare_persist_failed": True},
            )
            await operation_guard.close()
            return json.dumps(
                {
                    "status": "indeterminate",
                    "operation_id": intent_key,
                    "message": "Execution intent preparation could not be persisted",
                }
            )

    remote_path = f"/tmp/run-file-{run_uuid}.json"

    async def guarded_maintenance(command: str, **kwargs: Any) -> Any:
        """Convert any post-boundary controller failure into indeterminate."""
        try:
            return await _ssh.run(controller, command, **kwargs)
        except Exception as exc:
            if operation_guard and operation_owned:
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {"outcome": "indeterminate", "error_type": type(exc).__name__},
                )
                await operation_guard.close()
            return None

    async def maintenance_failure() -> str:
        await pause_for_reconciliation("controller maintenance outcome is ambiguous")
        return json.dumps(
            {
                "status": "indeterminate",
                "operation_id": intent_key if operation_guard else None,
                "message": "Controller maintenance outcome is ambiguous; reconciliation required",
            }
        )

    staging, staging_name, local_path = _write_ticket_staging_file(
        ticket_id, json.dumps(run_file, indent=2)
    )

    logger.info(f"[benchmark] SCP run-file to {controller}:{remote_path}")
    try:
        if operation_guard and operation_owned:
            try:
                await operation_guard.transition(
                    "side-effect-started", operation_record
                )
            except Exception:
                await operation_guard.terminalize(
                    operation_record, "indeterminate", {"outcome": "indeterminate"}
                )
                await operation_guard.close()
                await pause_for_reconciliation("launch boundary persistence failed")
                return json.dumps(
                    {
                        "status": "indeterminate",
                        "message": "Operation launch boundary could not be persisted",
                    }
                )
        try:
            scp_result = await _ssh.copy_to(
                controller, local_path, remote_path, mutating=True
            )
        except Exception as exc:
            if operation_guard and operation_owned:
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {"outcome": "indeterminate", "error_type": type(exc).__name__},
                )
                await operation_guard.close()
            await pause_for_reconciliation("run-file copy outcome is ambiguous")
            return json.dumps(
                {
                    "status": "indeterminate",
                    "operation_id": intent_key if operation_guard else None,
                    "message": "Run-file copy outcome is ambiguous; reconciliation required",
                }
            )
    finally:
        staging.unlink(staging_name, missing_ok=True)

    if scp_result.exit_code != 0:
        response = {
            "status": "failed",
            "message": f"Failed to copy run-file: {scp_result.stderr}",
        }
        if operation_guard and operation_owned:
            await operation_guard.terminalize(
                operation_record,
                "fail",
                {"benchmark_result": response},
            )
            await operation_guard.close()
            response["operation_id"] = intent_key
        return json.dumps(response)

    # Stop stale valkey container if no run is active (crucible issue #607)
    valkey_check = await guarded_maintenance(
        "podman ps --format '{{.Names}}' 2>/dev/null | grep -q crucible-valkey"
        " && ! podman ps --format '{{.Names}}' 2>/dev/null | grep -q crucible-rickshaw-run"
        " && podman stop crucible-valkey 2>/dev/null && echo STOPPED || echo OK",
    )
    if valkey_check is None:
        return await maintenance_failure()
    if "STOPPED" in (valkey_check.stdout or ""):
        logger.info(
            f"[benchmark] Stopped stale crucible-valkey container on {controller}"
        )

    # Restart OpenSearch before every run to ensure the CDM server (and its
    # node_modules) is freshly started alongside it. crucible's start_opensearch
    # only calls start_cdm_server when OpenSearch is not already running; if a
    # previous run left OpenSearch up, the CDM server may be absent, causing
    # add-run.sh to fail with "node_modules not found" at indexing time.
    # Stopping first guarantees a clean start_opensearch → start_cdm_server sequence.
    logger.info(
        f"[benchmark] Cycling OpenSearch on {controller} to ensure CDM server is fresh"
    )
    if (
        await guarded_maintenance(
            "crucible stop opensearch 2>/dev/null || true", timeout=60
        )
        is None
    ):
        return await maintenance_failure()
    # Wait up to 30s for the container to be fully gone
    for _ in range(6):
        gone = await guarded_maintenance(
            "podman ps --format '{{.Names}}' 2>/dev/null | grep -q crucible-opensearch"
            " && echo RUNNING || echo GONE",
        )
        if gone is None:
            return await maintenance_failure()
        if "GONE" in (gone.stdout or ""):
            break
        import asyncio as _asyncio

        await _asyncio.sleep(5)
    start_result = await guarded_maintenance(
        "crucible start opensearch 2>&1", timeout=180
    )
    if start_result is None:
        return await maintenance_failure()
    if "Successfully started OpenSearch" not in (start_result.stdout or ""):
        logger.warning(
            f"[benchmark] OpenSearch may not have started cleanly: "
            f"{(start_result.stdout or '')[-200:]}"
        )
    else:
        logger.info(f"[benchmark] OpenSearch and CDM server started on {controller}")

    cmd = f"{run_command or 'crucible run'} {remote_path}"
    logger.info(f"[benchmark] Executing: {cmd}")
    try:
        result = await _ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )
    except Exception as exc:
        if operation_guard and operation_owned:
            await operation_guard.terminalize(
                operation_record,
                "indeterminate",
                descriptor={
                    "outcome": "indeterminate",
                    "error_type": type(exc).__name__,
                },
            )
            await operation_guard.close()
        await pause_for_reconciliation("launch response was lost")
        raise

    run_dir = ""
    run_dir_re = re.compile(r"(/var/lib/crucible/run/[^/\s]+)")
    for line in result.stdout.split("\n"):
        m = run_dir_re.search(line)
        if m:
            run_dir = m.group(1)
            break

    run_id = ""
    if run_dir:
        dirname = run_dir.rstrip("/").split("/")[-1]
        uuid_match = re.search(r"--([0-9a-f-]{36})$", dirname)
        run_id = uuid_match.group(1) if uuid_match else dirname

    if not run_dir or not run_id:
        response = {
            "status": "indeterminate",
            "exit_code": result.exit_code,
            "harness": "crucible",
            "operation_id": intent_key if operation_guard else None,
            "message": "Crucible launch completed without a parseable external run identity; reconciliation required",
        }
        if operation_guard and operation_owned:
            await operation_guard.terminalize(
                operation_record,
                "indeterminate",
                {"benchmark_result": response, "external_id_missing": True},
            )
            await operation_guard.close()
        await pause_for_reconciliation("Crucible run identity was not returned")
        return json.dumps(response)

    # Persist the external identity before reading summaries or logs.  A lost
    # response after this point is therefore reconciled by identity rather
    # than replaying the launch command.
    if operation_guard and operation_owned and (run_id or run_dir):
        try:
            await operation_guard.transition(
                "external-id",
                operation_record,
                external_ids={"run_id": run_id, "run_dir": run_dir},
            )
        except Exception as exc:
            await operation_guard.terminalize(
                operation_record,
                "indeterminate",
                {
                    "outcome": "indeterminate",
                    "external_id_persist_failed": True,
                    "error_type": type(exc).__name__,
                },
            )
            await operation_guard.close()
            await pause_for_reconciliation(
                "external run identity could not be persisted"
            )
            return json.dumps(
                {
                    "status": "indeterminate",
                    "operation_id": intent_key,
                    "message": "External benchmark identity could not be persisted; reconciliation required",
                }
            )

    response = {
        "status": "completed" if result.exit_code == 0 else "failed",
        "exit_code": result.exit_code,
        "validation_id": validation_id,
        "run_dir": run_dir,
        "run_id": run_id or f"unknown-{run_uuid}",
        "harness": "crucible",
        "message": "Benchmark completed"
        if result.exit_code == 0
        else f"Benchmark failed (exit {result.exit_code})",
    }
    if result.exit_code == 0 and run_dir:
        summary_result = await guarded_maintenance(
            f"cat {run_dir}/run/result-summary.json",
            timeout=30,
        )
        if summary_result is None:
            return await maintenance_failure()
        if summary_result.exit_code == 0 and summary_result.stdout:
            try:
                response["result_summary"] = json.loads(summary_result.stdout)
            except json.JSONDecodeError:
                pass
        if "result_summary" not in response:
            response["status"] = "failed"
            response["message"] = (
                "Crucible exited with code 0 but result-summary.json "
                "is missing — the run did not produce results. "
                "Read the run logs with get_run_logs to diagnose."
            )
            log_result = await guarded_maintenance(
                f"test -f {run_dir}/crucible.log.xz"
                f" && xzcat {run_dir}/crucible.log.xz | tail -c 50000"
                f" || cat {run_dir}/crucible.log 2>/dev/null | tail -c 50000",
                timeout=60,
            )
            if log_result is None:
                return await maintenance_failure()
            if log_result.exit_code == 0 and log_result.stdout:
                response["run_log"] = log_result.stdout
    if result.exit_code != 0:
        response["output"] = result.stdout[-3000:] if result.stdout else ""
        response["error"] = result.stderr[-1000:] if result.stderr else ""
    if operation_guard and operation_owned:
        terminal_ok = await operation_guard.terminalize(
            operation_record,
            "complete" if response["status"] == "completed" else "fail",
            {"benchmark_result": response},
        )
        await operation_guard.close()
        response["operation_id"] = intent_key
        if not terminal_ok:
            response["status"] = "indeterminate"
            response["message"] = (
                "Operation result could not be durably recorded; reconciliation required"
            )
    return json.dumps(response)


@mcp.tool()
async def validate_benchmark(
    controller: str,
    run_file: dict,
    harness: str | None = None,
) -> str:
    """Validate a benchmark run-file on its controller without executing it.

    For Crucible, the run-file is copied to the configured controller and
    ``crucible validate`` performs the controller-side schema, endpoint,
    benchmark-parameter, and tool-parameter validation.  This tool never
    starts a benchmark, deploys endpoints, or starts supporting services.
    """
    await _ensure_init()
    run_file = _apply_runfile_safety_directives(run_file)

    harness_name = harness or "crucible"
    if harness_name != "crucible":
        return json.dumps(
            {
                "status": "unsupported",
                "valid": False,
                "harness": harness_name,
                "errors": [
                    f"Controller-side validation is not supported for harness "
                    f"'{harness_name}'"
                ],
            }
        )

    execution = {}
    if _skill_provider:
        execution = (await _skill_provider.get_all_private_config(harness_name)).get(
            "execution", {}
        )
    validation_command = execution.get("validation_command", "crucible validate")
    command_valid, command_reason = _validate_run_command(
        validation_command, harness_name
    )
    if not command_valid:
        return json.dumps(
            {
                "status": "failed",
                "valid": False,
                "harness": harness_name,
                "errors": [f"Configured validation command rejected: {command_reason}"],
            }
        )

    validation_uuid = uuid.uuid4().hex[:8]
    remote_path = f"/tmp/validate-run-file-{validation_uuid}.json"
    local_path = ""
    try:
        ticket_id = os.environ.get("TICKET_ID", "")
        staging = None
        staging_name = ""
        staging, staging_name, local_path = _write_ticket_staging_file(
            ticket_id, json.dumps(run_file, indent=2)
        )

        copied = await _ssh.copy_to(controller, local_path, remote_path, mutating=True)
        if copied.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "valid": False,
                    "harness": harness_name,
                    "controller": controller,
                    "errors": [
                        f"Failed to copy run-file to controller: "
                        f"{copied.stderr or 'unknown error'}"
                    ],
                }
            )

        result = await _ssh.run(
            controller,
            f"{validation_command} {remote_path} 2>&1",
            timeout=180,
        )
        output = (result.stdout or "").strip()
        if result.exit_code == 0:
            fields = _ticket.get("custom_fields", {}) if _ticket else {}
            params_fingerprint = _compute_params_fingerprint(fields)
            execution_plan_fingerprint = _execution_plan_fingerprint(fields)
            run_command = execution.get("run_command", "crucible run")
            command_valid, _ = _validate_run_command(run_command, harness_name)
            if not command_valid:
                return json.dumps(
                    {
                        "status": "failed",
                        "valid": False,
                        "errors": ["Configured execution command rejected"],
                    }
                )
            validation_id = await _persist_validated_runfile(
                run_file,
                harness_name,
                controller,
                params_fingerprint,
                execution_plan_fingerprint,
                run_command,
                validation_command,
                output,
                str(execution.get("validator_version", "unknown")),
            )
            if validation_id is None:
                return json.dumps(
                    {
                        "status": "failed",
                        "valid": False,
                        "harness": harness_name,
                        "controller": controller,
                        "errors": [
                            "Validation succeeded but the validation record "
                            "could not be persisted"
                        ],
                    }
                )
            return json.dumps(
                {
                    "status": "valid",
                    "valid": True,
                    "harness": harness_name,
                    "controller": controller,
                    "validation_id": validation_id,
                    "validation_output": _validation_output_descriptor(
                        ticket_id, output
                    ),
                    "errors": [],
                }
            )

        details = output or (result.stderr or "Validation failed").strip()
        return json.dumps(
            {
                "status": "invalid",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "validation_output": _validation_output_descriptor(ticket_id, output),
                "errors": [details],
                "exit_code": result.exit_code,
            }
        )
    except Exception as exc:
        logger.warning("[benchmark] Controller validation failed", exc_info=True)
        return json.dumps(
            {
                "status": "failed",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "errors": [f"Controller validation request failed: {exc}"],
            }
        )
    finally:
        if local_path:
            if staging:
                staging.unlink(staging_name, missing_ok=True)
        try:
            await _ssh.run(controller, f"rm -f {remote_path}", timeout=10)
        except Exception:
            logger.debug("Failed to remove temporary validation file", exc_info=True)


@mcp.tool()
async def get_run_logs(
    controller: str,
    run_id: str,
    harness: str | None = None,
    results_dir_pattern: str | None = None,
    max_kb: int = 50,
) -> str:
    """Retrieve logs from a benchmark run on the controller. Use this to diagnose failures — especially when execute_benchmark reports a missing result-summary. max_kb controls how much log to return (default 50 KB, max 200 KB)."""
    await _ensure_init()

    byte_limit = min(max_kb, 200) * 1024

    if run_id.startswith("/"):
        run_dir = run_id
    elif harness == "zathras":
        search_pattern = results_dir_pattern or "/tmp/results_*"
        result = await _ssh.run(
            controller,
            f"ls -dt {search_pattern} 2>/dev/null | head -1",
        )
        run_dir = result.stdout.strip()
    else:
        result = await _ssh.run(
            controller,
            f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
        )
        run_dir = result.stdout.strip()

    if not run_dir:
        return json.dumps(
            {"status": "not_found", "message": f"Run directory not found for {run_id}"}
        )

    if harness == "zathras":
        log_result = await _ssh.run(
            controller,
            f"find {run_dir} -name '*.log' -o -name '*.out' | head -5 | xargs tail -50 2>/dev/null",
        )
    else:
        log_result = await _ssh.run(
            controller,
            f"test -f {run_dir}/crucible.log.xz"
            f" && xzcat {run_dir}/crucible.log.xz | tail -c {byte_limit}"
            f" || cat {run_dir}/crucible.log 2>/dev/null | tail -c {byte_limit}",
            timeout=60,
        )

    return json.dumps(
        {
            "run_dir": run_dir,
            "log_lines": log_result.stdout or "" if log_result.stdout else "",
            "status": "ok" if log_result.exit_code == 0 else "error",
        }
    )


@mcp.tool()
async def execute_boot_time_test(
    sut_host: str,
    samples: int = 50,
    kpi_pattern: str = "",
    clean_journal: bool = False,
    description: str = "",
) -> str:
    """Run a boot-time analysis test on a remote System Under Test.

    This reboots the SUT multiple times, collecting boot timing
    metrics (kernel, initrd, userspace, systemd-analyze) per cycle.
    The SUT must have boot-time-analysis-tools installed — the tool
    will attempt to install the package automatically before testing.

    WARNING: This tool reboots the target host. It will NEVER run
    against localhost or the orchestrator host.

    Args:
        sut_host: IP address or hostname of the SUT (NEVER localhost).
        samples: Number of reboot cycles to collect (default 50).
        kpi_pattern: Regex pattern for KPI log matching.
        clean_journal: Delete journal before each reboot cycle.
        description: Human-readable test description.
    """
    await _ensure_init()

    # ── Guardrail: one execution per agent session ───────
    global _boot_time_executed
    if _boot_time_executed:
        return json.dumps(
            {
                "status": "rejected",
                "error": (
                    "Boot-time test already executed in "
                    "this session. Submit your result "
                    "and exit. Do not call this tool "
                    "again."
                ),
            }
        )
    _boot_time_executed = True

    # ── Guardrail: never reboot the orchestrator ──────────────
    if _is_self_host(sut_host):
        return json.dumps(
            {
                "status": "rejected",
                "error": (
                    f"SAFETY: refusing to run boot-time test "
                    f"against '{sut_host}' — this would reboot "
                    f"the orchestrator host."
                ),
            }
        )

    # ── Locate boot-time-analysis-scripts repo ────────────────
    scripts_dir = None
    if _repo_cache is not None:
        scripts_dir = _repo_cache.get_path("boot-time-analysis-scripts")
    if scripts_dir is None:
        return json.dumps(
            {
                "status": "failed",
                "error": (
                    "boot-time-analysis-scripts repo not found "
                    "in skill cache. Ensure it is configured in "
                    "harness_repos."
                ),
            }
        )

    test_script = scripts_dir / "boot-timings-test.sh"
    install_script = scripts_dir / "install-boot-time-analysis-tool.sh"
    merge_script = scripts_dir / "boot-time-merge.py"

    if not test_script.exists():
        return json.dumps(
            {
                "status": "failed",
                "error": (f"boot-timings-test.sh not found at {test_script}"),
            }
        )

    # ── Wait for SSH readiness ─────────────────────────
    # Freshly provisioned boards may not have SSH ready
    # immediately. Wait up to 60s for port 22.  If a
    # Jumpstarter lease is available and SSH fails, power-
    # cycle the board and retry — the board may not have
    # booted cleanly after flashing.
    import asyncio as _asyncio
    import socket as _socket

    _js_lease_id = ""
    if _ticket:
        _js_fields = _ticket.get("custom_fields", {})
        _js_meta = _js_fields.get("resource_provider_metadata", {})
        _js_lease_id = _js_meta.get("lease_id", "")
        if _js_fields.get("resource_provider") != "jumpstarter":
            _js_lease_id = ""

    _MAX_POWER_CYCLES = 3
    _SSH_ATTEMPTS_PER_CYCLE = 12
    _ssh_ready = False
    _power_cycles_attempted = 0
    _ssh_polling_windows = 0

    while True:
        _ssh_polling_windows += 1
        for _attempt in range(_SSH_ATTEMPTS_PER_CYCLE):
            try:
                s = _socket.create_connection((sut_host, 22), timeout=5)
                s.close()
                _ssh_ready = True
                break
            except (OSError, ConnectionRefusedError):
                logger.info(
                    "[boot-time] Waiting for SSH on %s (cycle %d/%d, attempt %d/%d)",
                    sut_host,
                    _power_cycles_attempted + 1,
                    _MAX_POWER_CYCLES + 1,
                    _attempt + 1,
                    _SSH_ATTEMPTS_PER_CYCLE,
                )
                await _asyncio.sleep(5)

        if _ssh_ready:
            break

        if not _js_lease_id:
            # No Jumpstarter lease — cannot power-cycle
            break

        if _power_cycles_attempted >= _MAX_POWER_CYCLES:
            break

        _power_cycles_attempted += 1
        logger.warning(
            "[boot-time] SSH unreachable after %ds,"
            " power-cycling board via Jumpstarter"
            " (lease=%s, power cycle %d/%d)",
            _SSH_ATTEMPTS_PER_CYCLE * 5,
            _js_lease_id,
            _power_cycles_attempted,
            _MAX_POWER_CYCLES,
        )
        try:
            _pc_proc = await AuditedSubprocessRunner().start(
                [
                    "jmp",
                    "shell",
                    "--lease",
                    _js_lease_id,
                    "--",
                    "j",
                    "power",
                    "cycle",
                ],
                mutating=True,
            )
            _pc_out, _pc_err = await asyncio.wait_for(
                _pc_proc.communicate(),
                timeout=60,
            )
            logger.info(
                "[boot-time] Power cycle complete (rc=%d), waiting for SSH",
                _pc_proc.returncode,
            )
        except Exception as _pc_exc:
            logger.warning(
                "[boot-time] Power cycle failed: %s",
                _pc_exc,
            )
            break

    # ── Prep: install boot-time-analysis-tools on SUT ─────────
    if not _ssh_ready:
        return json.dumps(
            {
                "status": "failed",
                "error": (
                    f"SUT {sut_host} not SSH-reachable after"
                    f" {_power_cycles_attempted} Jumpstarter power-cycle attempt(s)"
                    f" across {_ssh_polling_windows} SSH polling window(s)."
                    f" Board may need manual intervention."
                ),
            }
        )

    ssh_user = "root"
    ssh_password = "password"
    if _ticket:
        fields = _ticket.get("custom_fields", {})
        ssh_user = fields.get("ssh_user", ssh_user)
        ssh_password = fields.get("ssh_password", ssh_password)

    if install_script.exists():
        logger.info(f"[boot-time] Installing boot-time-analysis-tools on {sut_host}")
        import asyncio as _asyncio

        # Security: password on argv is visible in /proc/pid/cmdline.
        # The external scripts require --password= on the command line;
        # upstream fix: accept --password-file or SSHPASS env var.
        install_proc = await AuditedSubprocessRunner().start(
            [
                str(install_script),
                sut_host,
                f"--username={ssh_user}",
                f"--password={ssh_password}",
            ],
            cwd=str(scripts_dir),
            mutating=True,
        )
        install_out, install_err = await install_proc.communicate()
        if install_proc.returncode != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "error": (
                        f"Failed to install boot-time-analysis-tools on {sut_host}"
                    ),
                    "output": install_out.decode(errors="replace")[-2000:],
                    "stderr": install_err.decode(errors="replace")[-1000:],
                }
            )
        logger.info(f"[boot-time] boot-time-analysis-tools installed on {sut_host}")

    # ── Build command ─────────────────────────────────────────
    import asyncio as _asyncio

    run_uuid = uuid.uuid4().hex[:8]

    # Use ticket-ID-based artifact directory so artifacts
    # are discoverable by ticket ID without querying the
    # state store. Configurable via AGENTIC_PERF_ARTIFACTS.
    from paths import create_artifact_dir

    _ticket_id = os.environ.get("TICKET_ID", "")
    output_dir = create_artifact_dir(_ticket_id, run_uuid)
    artifact_filesystem = (
        AuditedFilesystem(
            RootedPath(
                output_dir, "artifact", logical_prefix=f"{_ticket_id}/{run_uuid}"
            ),
            ticket_id=_ticket_id,
            emit=durable_filesystem_emitter(),
            critical=True,
        )
        if _ticket_id
        else AuditedFilesystem.system(output_dir)
    )
    artifact_file_mode = 0o600 if _ticket_id else 0o644

    # Security: password on argv — see comment at install_proc above.
    cmd = [
        str(test_script),
        sut_host,
        str(samples),
        f"--username={ssh_user}",
        f"--password={ssh_password}",
        "--folder-prefix=results",
    ]
    if clean_journal:
        cmd.append("--clean-journal=true")

    # Auto-enable Jumpstarter serial capture when a
    # Jumpstarter lease is active. Always use the lease
    # name so the harness creates its own scoped connection.
    # Do NOT search for existing sockets in /tmp/ — with
    # concurrent tickets, a socket from another ticket's
    # platform agent could be found and then go stale.
    jumpstarter_env: dict[str, str] = {}
    if _ticket:
        fields = _ticket.get("custom_fields", {})
        metadata = fields.get("resource_provider_metadata", {})
        lease_id = metadata.get("lease_id", "")
        directives = fields.get("directives", {})
        serial_enabled = directives.get("jumpstarter_serial", False)
        is_jumpstarter = lease_id and fields.get("resource_provider") == "jumpstarter"
        if is_jumpstarter and serial_enabled:
            cmd.append("--jumpstarter-serial")
            cmd.append(f"--jumpstarter-lease-name={lease_id}")
            logger.info(
                f"[boot-time] Using Jumpstarter lease {lease_id} for serial capture"
            )
        elif is_jumpstarter:
            # Enable Jumpstarter power control even without
            # serial capture so cold boots use hardware
            # power cycling instead of SSH reboots.
            cmd.append(f"--jumpstarter-lease-name={lease_id}")
            logger.info(
                f"[boot-time] Using Jumpstarter lease {lease_id} for power control"
            )

        # Pass power-off delay from directives
        power_off_delay = directives.get("power_off_delay") or directives.get(
            "jumpstarter_power_off_delay"
        )
        if power_off_delay is not None:
            cmd.append(f"--power-off-delay={power_off_delay}")

    # Separator for boot-time-analysis-tools arguments
    cmd.append("--")
    cmd.extend(["--max-time", "0"])
    if kpi_pattern:
        cmd.extend(["--kpi-re-pattern", kpi_pattern])
    if description:
        cmd.extend(["--description", description])

    safe_cmd = [a for a in cmd[:6] if not a.startswith("--password=")]
    logger.info(
        "[boot-time] Executing: %s ... (%d samples)",
        " ".join(safe_cmd),
        samples,
    )

    # ── Passive serial capture ───────────────────────────
    # Run `jmp shell --lease <id> -- j serial pipe` in the
    # background to capture serial output during SSH-based
    # reboots. Non-blocking — if serial fails, the benchmark
    # continues. Captures firmware/watchdog/kernel messages
    # that aren't available via SSH or journal.
    import os as _os

    serial_proc = None
    serial_log_fh = None
    serial_log_path = output_dir / "serial-capture.log"
    if _ticket:
        _fields = _ticket.get("custom_fields", {})
        _metadata = _fields.get(
            "resource_provider_metadata",
            {},
        )
        _lease_id = _metadata.get("lease_id", "")
        _directives = _fields.get("directives", {})
        _passive_serial = _directives.get(
            "serial_capture",
            False,
        )
        # Don't run passive serial alongside active serial
        _serial_active = _directives.get(
            "jumpstarter_serial",
            False,
        )
        if (
            _lease_id
            and _fields.get("resource_provider") == "jumpstarter"
            and _passive_serial
            and not _serial_active
        ):
            try:
                serial_log_fh = artifact_filesystem.open_stream(
                    "serial-capture.log", mode=artifact_file_mode
                )
                serial_proc = await AuditedSubprocessRunner().start(
                    [
                        "jmp",
                        "shell",
                        f"--lease={_lease_id}",
                        "--",
                        "j",
                        "serial",
                        "pipe",
                    ],
                    stdout=serial_log_fh,
                    stderr=_asyncio.subprocess.DEVNULL,
                    mutating=True,
                )
                logger.info(
                    f"[boot-time] Passive serial capture "
                    f"started (lease={_lease_id}, "
                    f"pid={serial_proc.pid})"
                )
            except Exception as e:
                logger.warning(
                    f"[boot-time] Failed to start passive serial capture: {e}"
                )
                serial_proc = None
                if serial_log_fh:
                    serial_log_fh.close()
                    serial_log_fh = None

    # ── Execute ───────────────────────────────────────────
    # Run from output_dir so boot-timings-test.sh creates
    # its results folder (and SCPs files) here, not relative
    # to the scripts repo.
    # Merge Jumpstarter env vars with current environment
    run_env = {**_os.environ, **jumpstarter_env} if jumpstarter_env else None
    # Timeout: samples × ~90s per sample + 15min overhead.
    # Prevents orphaned jmp shell subprocesses from keeping
    # the pipe open indefinitely (observed: 4hr hang when
    # capture-boot timed out but jmp shell child lingered).
    benchmark_timeout = (samples * 90) + 900

    proc = await AuditedSubprocessRunner().start(
        cmd,
        cwd=str(output_dir),
        env=run_env,
        mutating=True,
    )
    _STALL_CHECK_INTERVAL = 60
    _STALL_TIMEOUT = 300

    # Keep communicate() running for the whole lifetime of the process so
    # stdout/stderr pipes are drained while we poll for artifact progress.
    # Waiting on proc.wait() with PIPEs can deadlock when the child produces
    # more output than the pipe buffer can hold.
    communicate_task = _asyncio.create_task(proc.communicate())
    loop = _asyncio.get_running_loop()
    start_time = loop.time()
    deadline = start_time + benchmark_timeout
    try:
        last_file_count = sum(1 for path in output_dir.rglob("*") if path.is_file())
    except OSError:
        last_file_count = 0
    last_progress_time = start_time
    stall_killed = False

    while not communicate_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "[boot-time] Subprocess timed out after %ds, killing",
                benchmark_timeout,
            )
            proc.kill()
            break

        try:
            await _asyncio.wait_for(
                _asyncio.shield(communicate_task),
                timeout=min(_STALL_CHECK_INTERVAL, remaining),
            )
            break
        except _asyncio.TimeoutError:
            now = loop.time()
            try:
                file_count = sum(1 for path in output_dir.rglob("*") if path.is_file())
            except OSError:
                file_count = last_file_count

            if file_count > last_file_count:
                last_file_count = file_count
                last_progress_time = now
            elif now - last_progress_time >= _STALL_TIMEOUT:
                logger.warning(
                    "[boot-time] No new artifacts for %ds (stall detected at "
                    "%d files), killing subprocess",
                    int(now - last_progress_time),
                    file_count,
                )
                proc.kill()
                stall_killed = True
                break

    # The communicate task continues draining output after kill. Bound the
    # cleanup in case a descendant inherited one of the pipe file descriptors.
    try:
        stdout_bytes, stderr_bytes = await _asyncio.wait_for(
            _asyncio.shield(communicate_task),
            timeout=10,
        )
    except _asyncio.TimeoutError:
        communicate_task.cancel()
        try:
            await communicate_task
        except _asyncio.CancelledError:
            pass
        stdout_bytes, stderr_bytes = b"", b""

    exit_code = proc.returncode or 0
    stdout_str = stdout_bytes.decode(errors="replace")
    stderr_str = stderr_bytes.decode(errors="replace")
    if stall_killed:
        stderr_str += (
            "\n[agentic-perf] Benchmark killed: no new artifact files for 5 "
            "minutes (stall detected). This may indicate the board is "
            "unresponsive after a cold reboot or the serial connection is dead."
        )

    # ── Parse reboot method from script output ───────
    reboot_method = ""
    for line in stdout_str.split("\n"):
        if line.startswith("Reboot mode:"):
            reboot_method = line.split(":", 1)[1].strip()
            break

    # ── Stall diagnostics ─────────────────────────────────
    # When the stall detector kills the process, capture
    # board state before reporting failure.
    stall_diag: dict[str, Any] = {}
    # Capture diagnostics on any failure, not just stall kills.
    # The script may exit with code 1 (serial timeout, boot
    # failure) before the stall detector triggers.
    # Check for boot sample files (boot_time_logs.json), not
    # raw file count — serial/metadata files are always present.
    sample_count = _count_boot_time_samples(output_dir)
    run_diag = stall_killed or (exit_code != 0 and sample_count == 0)
    if run_diag and _ssh is not None and sut_host:
        logger.info("[boot-time] Capturing stall diagnostics for %s", sut_host)
        try:
            ping = await _ssh.run(
                sut_host,
                "echo ALIVE",
                timeout=5,
            )
            stall_diag["ssh_reachable"] = ping.exit_code == 0 and "ALIVE" in ping.stdout
        except Exception:
            stall_diag["ssh_reachable"] = False

        if not stall_diag.get("ssh_reachable"):
            # Board not reachable via SSH — try ping
            try:
                ping_proc = await _asyncio.create_subprocess_exec(
                    "ping",
                    "-c1",
                    "-W3",
                    sut_host,
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                await ping_proc.wait()
                stall_diag["pingable"] = ping_proc.returncode == 0
            except Exception:
                stall_diag["pingable"] = False

        stall_diag["samples_before_stall"] = sample_count
        stall_diag["stall_duration_s"] = _STALL_TIMEOUT

        # Write diagnostics to artifact file
        try:
            import json as _json

            artifact_filesystem.write(
                "stall-diagnostics.json",
                _json.dumps(stall_diag, indent=2),
                mode=artifact_file_mode,
            )
            logger.info(
                "[boot-time] Stall diagnostics: %s",
                stall_diag,
            )
        except Exception:
            pass

    # ── Stop passive serial capture ─────────────────────────
    if serial_proc is not None:
        try:
            serial_proc.terminate()
            try:
                await serial_proc.wait(timeout=10)
            except _asyncio.TimeoutError:
                # The tracked wait has already escalated to kill.
                pass
            logger.info("[boot-time] Passive serial capture stopped")
        except Exception as e:
            logger.warning(f"[boot-time] Error stopping serial capture: {e}")
    if serial_log_fh is not None:
        serial_log_fh.close()
    if serial_log_path.exists():
        size = serial_log_path.stat().st_size
        if size > 0:
            logger.info(
                f"[boot-time] Serial capture log: {serial_log_path} ({size} bytes)"
            )
        else:
            # Remove empty log file
            artifact_filesystem.unlink("serial-capture.log", missing_ok=True)

    # ── Parse results ─────────────────────────────────────────
    # Find the results folder created by boot-timings-test.sh
    result_folders = sorted(output_dir.glob("results-*"))
    if not result_folders:
        # Try without prefix pattern
        result_folders = [d for d in output_dir.iterdir() if d.is_dir()]

    boot_time_logs: list[Path] = []
    for folder in result_folders:
        boot_time_logs.extend(sorted(folder.glob("*boot_time_logs.json")))

    # ── Collect system metadata ───────────────────────
    metadata_file = output_dir / "metadata.json"
    collect_metadata = scripts_dir / "collect-system-metadata.sh"
    if collect_metadata.exists():
        logger.info(f"[boot-time] Collecting system metadata from {sut_host}")
        meta_proc = await AuditedSubprocessRunner().start(
            [
                str(collect_metadata),
                sut_host,
            ],
            cwd=str(scripts_dir),
            mutating=True,
        )
        meta_out, _ = await meta_proc.communicate()
        if meta_proc.returncode == 0 and meta_out:
            artifact_filesystem.write(
                "metadata.json", meta_out, mode=artifact_file_mode
            )
            logger.info("[boot-time] Metadata collected")
        else:
            # Create minimal stub so merge can proceed
            artifact_filesystem.write("metadata.json", "{}", mode=artifact_file_mode)
            logger.info("[boot-time] Metadata collection failed — using empty stub")
    else:
        artifact_filesystem.write("metadata.json", "{}", mode=artifact_file_mode)

    # ── Merge into Horreum-compatible JSON ─────────────
    merged_file = output_dir / "merged-results.json"
    if boot_time_logs and merge_script.exists():
        merge_cmd = [
            sys.executable,
            str(merge_script),
            "-m",
            str(metadata_file),
            "--schema",
            "urn:boot-time-verbose:07",
            "--run-source",
            "agentic-perf",
        ]
        if description:
            merge_cmd.extend(["--description", description])
        # Pass partial-run info if available
        for folder in result_folders:
            status_file = folder / "collection_status.json"
            if status_file.exists():
                try:
                    cs = json.loads(status_file.read_text())
                    merge_cmd.extend(
                        [
                            "--requested-samples",
                            str(cs.get("requested_samples", samples)),
                        ]
                    )
                    if cs.get("partial"):
                        merge_cmd.extend(
                            [
                                "--partial-run",
                                "--partial-failure-reason",
                                cs.get(
                                    "failure_reason",
                                    "unknown",
                                ),
                            ]
                        )
                except (json.JSONDecodeError, OSError):
                    pass
                break
        merge_cmd.extend(str(f) for f in boot_time_logs)

        merge_proc = await AuditedSubprocessRunner().start(
            merge_cmd,
            cwd=str(scripts_dir),
            mutating=True,
        )
        merge_out, merge_err = await merge_proc.communicate()
        if merge_proc.returncode == 0 and merge_out:
            artifact_filesystem.write(
                "merged-results.json", merge_out, mode=artifact_file_mode
            )
            logger.info(f"[boot-time] Merged results saved to {merged_file}")
        else:
            logger.warning(
                "[boot-time] Merge failed: " + merge_err.decode(errors="replace")[:200]
            )

    # ── Extract KPIs from per-sample summary files ───────
    kpis: dict[str, Any] = {}
    summary_files: list[Path] = []
    for folder in result_folders:
        summary_files.extend(sorted(folder.glob("*_summary.json")))
    # Exclude all_summary.json (combined file)
    summary_files = [f for f in summary_files if f.name != "all_summary.json"]
    if summary_files:
        sa_totals: list[float] = []
        sa_kernels: list[float] = []
        sa_initrds: list[float] = []
        sa_userspaces: list[float] = []
        for sf in summary_files:
            try:
                sd = json.loads(sf.read_text())
                sa = sd.get("satime", {})
                if "total" in sa:
                    sa_totals.append(sa["total"])
                if "kernel" in sa:
                    sa_kernels.append(sa["kernel"])
                if "initrd" in sa:
                    sa_initrds.append(sa["initrd"])
                if "userspace" in sa:
                    sa_userspaces.append(sa["userspace"])
            except (json.JSONDecodeError, OSError):
                continue

        def _avg(vals: list[float]) -> float | None:
            return round(sum(vals) / len(vals), 3) if vals else None

        kpis = {
            "sample_count": len(summary_files),
            "avg_total_boot_s": _avg(sa_totals),
            "avg_kernel_s": _avg(sa_kernels),
            "avg_initrd_s": _avg(sa_initrds),
            "avg_userspace_s": _avg(sa_userspaces),
        }

    # KPI pattern was requested but no matching data found.
    # This indicates the board didn't produce expected boot
    # metrics — treat as a node-level failure condition.
    kpi_failure = False
    if kpi_pattern and exit_code == 0:
        has_kpi_data = bool(
            kpis.get("avg_kernel_s")
            or kpis.get("avg_initrd_s")
            or kpis.get("avg_userspace_s")
        )
        if not has_kpi_data:
            kpi_failure = True

    status = "completed"
    if exit_code != 0:
        status = "failed"
    elif kpi_failure:
        status = "completed_no_kpi"

    response: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "run_id": f"boot-time-{run_uuid}",
        "harness": "boot-time",
        "samples_requested": samples,
        "samples_collected": len(boot_time_logs),
        "output_dir": str(output_dir),
        "message": (
            f"Boot time test completed ({len(boot_time_logs)}/{samples} samples)"
            if exit_code in (0, 2) and not kpi_failure
            else (
                "Boot time test completed but expected KPI "
                "patterns not found (kernel/initrd/userspace). "
                "The board may not be producing valid boot "
                "metrics. This is a node-level issue."
            )
            if kpi_failure
            else f"Boot time test failed (exit {exit_code})"
        ),
    }
    if kpis:
        response["kpis"] = kpis
    if merged_file.exists():
        response["merged_results_file"] = str(merged_file)
        try:
            merged = json.loads(merged_file.read_text())
            cfg = merged.get("rhivos_config", {})
            if cfg:
                response["system_config"] = {
                    k: cfg[k]
                    for k in (
                        "kernel",
                        "os_name",
                        "architecture",
                    )
                    if cfg.get(k)
                }
        except (json.JSONDecodeError, OSError):
            pass
    if exit_code not in (0, 2):
        response["output"] = stdout_str[-3000:]
        response["error"] = stderr_str[-1000:]
    else:
        # On success, include a compact summary so the
        # evaluate agent can see serial capture status
        # and key metrics without the full log.
        summary_lines = []
        for line in stdout_str.split("\n"):
            ll = line.lower()
            if any(
                k in ll
                for k in (
                    "serial capture",
                    "jumpstarter",
                    "sample",
                    "error",
                    "\u2713",
                    "\u2717",
                    "boot_time",
                    "kpi",
                    "warning",
                    "failed",
                )
            ):
                summary_lines.append(line.rstrip())
        if summary_lines:
            response["output_summary"] = "\n".join(summary_lines[-30:])

    # Save output_dir to ticket so the evaluate agent
    # can find artifacts. Accumulate across loop-back
    # runs so all artifacts remain accessible.
    if _ticket and response.get("output_dir"):
        try:
            from providers.execution import AuditedAsyncHTTPClient

            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            from agents.server_utils import ticket_state_headers

            headers = ticket_state_headers()
            ticket_id = _ticket.get("id", "")
            if ticket_id:
                async with AuditedAsyncHTTPClient(
                    timeout=10.0, headers=headers
                ) as _client:
                    # Fetch current list to append
                    r = await _client.get(
                        f"{store_url}/api/v1/tickets/{ticket_id}",
                    )
                    existing = r.json().get("custom_fields", {}).get("output_dirs", [])
                    new_dir = response["output_dir"]
                    if new_dir not in existing:
                        existing.append(new_dir)
                    update_response = await _client.patch(
                        f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                        json={
                            "fields": {
                                "output_dir": new_dir,
                                "output_dirs": existing,
                            }
                        },
                    )
                    update_response.raise_for_status()
                logger.info(
                    f"[boot-time] Saved output_dir to ticket"
                    f" {ticket_id} ({len(existing)} total)"
                )
        except Exception:
            logger.warning("Failed to save output_dir", exc_info=True)

    if stall_diag:
        response["stall_diagnostics"] = stall_diag

    if reboot_method:
        response["reboot_method"] = reboot_method
        # Flag mismatch: cold boot requested but SSH reboot used
        boot_type = (
            _ticket.get("custom_fields", {}).get("directives", {}).get("boot_type", "")
            if _ticket
            else ""
        )
        if boot_type == "cold" and reboot_method == "ssh":
            response["reboot_method_mismatch"] = (
                "Cold boot requested but script used SSH "
                "reboots. Jumpstarter power control flags "
                "may not have been passed correctly."
            )

    return json.dumps(response)


def _count_boot_time_samples(output_dir: Path) -> int:
    """Return the number of boot-time sample artifacts in an output directory."""
    return sum(1 for _ in output_dir.glob("**/*boot_time_logs.json"))


async def get_registered_tools():
    """Introspect this server's registered @mcp.tool() functions."""
    from providers.llm.base import ToolDefinition

    tools = await mcp.list_tools()
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or "",
            input_schema=t.parameters,
        )
        for t in tools
    ]


if __name__ == "__main__":
    mcp.run()
