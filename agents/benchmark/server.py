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

from agents.benchmark.harnesses.boot_time import BootTimeContext, run_boot_time_test
from agents.crucible_mcp import CrucibleMCPAdapter
from agents.mcp_audit import create_ticket_mcp
from agents.mcp_client import MCPToolCallError
from agents.server_utils import (
    FORBIDDEN_REBOOT_HOSTS,
    _emit_context_audit_event,
    _public_context_result,
    build_crucible_context_gateway,
    build_repo_cache,
    build_skill_provider_async,
    build_ssh_from_ticket,
    controller_context_gateway,
    crucible_context_gateway,
    is_self_host,
    read_skill_documents,
    ticket_controller_host,
    tool_progress,
)
from agents.skill_gateway import (
    SKILL_GATEWAY_TOOL_DESCRIPTION,
    organization_manages_harness,
    skill_config_view,
    skill_context_gateway,
)
from agents.tool_ownership import (
    ownership_metadata,
    require_ticket_harness,
    selected_harness_from_ticket,
)
from providers.benchmark_identity import (
    benchmark_intent_identity as _benchmark_intent_identity,
)
from providers.benchmark_identity import (
    comparison_fingerprint,
    comparison_step_requested,
)
from providers.execution import (
    AuditedFilesystem,
    RootedPath,
    durable_filesystem_emitter,
)
from providers.skills.crucible import crucible_runfile_contract_errors

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("benchmark-agent")
_CRUCIBLE_TOOL_OWNER = "crucible"
_BOOT_TIME_TOOL_OWNER = "boot-time"

CONTROLLER_KEY_COMMENT = "agentic-perf-controller-key"

_CRUCIBLE_ROOT = "/opt/crucible"
_OPERATION_INLINE_RESULT_BYTES = 3000
_OPERATION_RESULT_BLOB_MAX_BYTES = 1_048_576


def _is_expired_plan_handle(exc: MCPToolCallError) -> bool:
    try:
        payload = json.loads(str(exc))
    except json.JSONDecodeError:
        return "expired_plan_handle" in str(exc)
    return isinstance(payload, dict) and payload.get("code") == "expired_plan_handle"


def _is_run_document_too_large(exc: MCPToolCallError) -> bool:
    try:
        payload = json.loads(str(exc))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("code") == "too_large"


def _execution_result_blob_store(ticket_id: str):
    from providers.tracing import PayloadBlobStore

    return PayloadBlobStore(ticket_id=ticket_id)


def _prepare_operation_result(
    ticket_id: str, result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep durable receipts small while retaining a private full result."""
    from providers.redaction import get_shared_redactor

    safe = get_shared_redactor().redact(ticket_id, result)
    if not isinstance(safe, dict) or "redaction_error" in safe:
        raise ValueError("benchmark result redaction failed")
    identity_keys = (
        "status",
        "exit_code",
        "harness",
        "run_id",
        "run_dir",
        "validation_id",
    )
    if any(safe.get(key) != result.get(key) for key in identity_keys):
        raise ValueError("benchmark result identity could not be retained safely")
    safe_bytes = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    if len(safe_bytes) <= _OPERATION_INLINE_RESULT_BYTES:
        return safe, safe
    result_blob_store = _execution_result_blob_store(ticket_id)
    reference = result_blob_store.put(
        safe_bytes, max_bytes=_OPERATION_RESULT_BLOB_MAX_BYTES
    )
    compact = {key: safe[key] for key in identity_keys if key in safe}
    compact["result_blob_ref"] = reference
    if len(json.dumps(compact).encode()) > _OPERATION_INLINE_RESULT_BYTES:
        raise ValueError("benchmark result identity exceeds receipt limit")
    return compact, safe


def _replay_operation_result(ticket_id: str, cached: dict[str, Any]) -> dict[str, Any]:
    """Recover the full cached result without relaunching a benchmark."""
    reference = cached.get("result_blob_ref")
    if not reference:
        return cached
    try:
        payload = _execution_result_blob_store(ticket_id).get(
            reference, max_bytes=_OPERATION_RESULT_BLOB_MAX_BYTES
        )
        result = json.loads(payload)
        keys = ("status", "exit_code", "harness", "run_id", "run_dir", "validation_id")
        if not isinstance(result, dict) or any(
            result.get(key) != cached.get(key) for key in keys
        ):
            raise ValueError("benchmark result identity mismatch")
        return result
    except Exception:
        logger.exception("Unable to recover cached benchmark result")
        return {
            "status": "indeterminate",
            "reason_code": "cached_result_unavailable",
            "message": "The recorded benchmark result payload could not be recovered.",
        }


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
    execution_transport: str = "",
    mcp_contract_version: str = "",
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
    if execution_transport == "crucible_mcp":
        record.update(
            {
                "execution_transport": execution_transport,
                "mcp_contract_version": mcp_contract_version or "3",
                "crucible_idempotency_key": (
                    f"{ticket_id or 'external'}:crucible:{validation_id}"
                ),
            }
        )
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
            ticket_cf = ticket_response.json().get("custom_fields") or {}
            plan = ticket_cf.get("execution_plan") or {}
            if isinstance(plan.get("current_step"), int):
                record["execution_plan_step_id"] = str(plan["current_step"])
                _validation_records[validation_id]["execution_plan_step_id"] = str(
                    plan["current_step"]
                )
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


_FORBIDDEN_REBOOT_HOSTS = FORBIDDEN_REBOOT_HOSTS
_is_self_host = is_self_host


SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Module-level globals — lazily initialized by _ensure_init()
_initialized = False
_crucible_mcp: CrucibleMCPAdapter | None = None


def _truncated_output_guidance(
    stdout: str,
    stderr: str,
    *,
    controller: str = "",
    run_id: str = "",
    artifact_files: list[str] | None = None,
) -> dict[str, str]:
    """Build truncated output + guidance for where to find full data.

    Returns a dict with 'output', 'error', and 'full_output_hint' keys
    suitable for merging into a tool response.
    """
    result: dict[str, str] = {
        "output": stdout[-3000:] if stdout else "",
        "error": stderr[-1000:] if stderr else "",
    }
    hints: list[str] = []
    if len(stdout) > 3000:
        hints.append(f"Output truncated to last 3000 of {len(stdout)} bytes.")
    if controller and run_id:
        hints.append(
            f"Full logs available via get_run_logs("
            f"controller='{controller}', run_id='{run_id}')."
        )
    if artifact_files:
        hints.append(
            "Local artifact files available via read_artifact_file(): "
            + ", ".join(artifact_files)
        )
    if hints:
        result["full_output_hint"] = " ".join(hints)
    return result


_ssh = None
_skill_provider = None
_crucible_context = None
_repo_cache = None
_ticket: dict[str, Any] = {}
_validation_records: dict[str, dict[str, Any]] = {}
_BENCHMARK_OPERATION_LEASE_TTL_SECONDS = 900.0
_BENCHMARK_OPERATION_LEASE_RENEW_INTERVAL_SECONDS = 300.0
_BENCHMARK_OPERATION_RENEW_RETRY_INITIAL_SECONDS = 1.0
_BENCHMARK_OPERATION_RENEW_RETRY_MAX_SECONDS = 30.0


class _BenchmarkOperation:
    """Small async adapter over the shared fenced operation registry."""

    def __init__(self, key: str, request_hash: str, owner: str) -> None:
        self.key = key
        self.request_hash = request_hash
        self.owner = owner
        self._client = None
        self._local = None
        self._renewal_stop: asyncio.Event | None = None
        self._renewal_task: asyncio.Task[BaseException | None] | None = None
        self._renewal_failure: BaseException | None = None
        self._renewal_pending_failure: BaseException | None = None
        self._initial_lease_deadline: float | None = None
        self._renewal_inflight = False
        self._renewal_state_changed: asyncio.Event | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    async def acquire(self) -> tuple[dict[str, Any], str]:
        from providers.tracing.client import TraceClient

        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
        if token:
            self._client = TraceClient(url, token)
            self._initial_lease_deadline = (
                asyncio.get_running_loop().time()
                + _BENCHMARK_OPERATION_LEASE_TTL_SECONDS
            )
            try:
                result = await asyncio.to_thread(
                    self._client.operation_acquire,
                    self.key,
                    self.request_hash,
                    _BENCHMARK_OPERATION_LEASE_TTL_SECONDS,
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
        self._initial_lease_deadline = (
            asyncio.get_running_loop().time() + _BENCHMARK_OPERATION_LEASE_TTL_SECONDS
        )
        try:
            operation, status = await asyncio.to_thread(
                self._local.acquire_operation_result,
                self.key,
                self.request_hash,
                self.owner,
                _BENCHMARK_OPERATION_LEASE_TTL_SECONDS,
            )
        except (OperationLeaseError, OperationTransitionError):
            operation = self._local.get_operation(self.key)
            return (operation.__dict__ if operation else {}), "in_progress"
        return operation.__dict__, status

    async def start_renewal(self, operation: dict[str, Any]) -> None:
        """Keep this fenced execution lease alive until terminalization.

        The registry client is synchronous, so each renewal runs in a worker
        thread while the Crucible process and progress callbacks use the event
        loop. The completion callback also stops the heartbeat if the tool
        exits through an unexpected exception or cancellation path.
        """
        if self._renewal_task is not None:
            return
        self._renewal_stop = asyncio.Event()
        self._renewal_state_changed = asyncio.Event()
        self._renewal_task = asyncio.create_task(
            self._renew_operation_lease(operation, self._renewal_stop),
            name=f"benchmark-operation-renew:{self.key}",
        )
        parent_task = asyncio.current_task()
        if parent_task is not None:
            stop = self._renewal_stop

            def cleanup_after_parent(_task: asyncio.Task[Any]) -> None:
                stop.set()
                if self._closed:
                    return

                async def cleanup() -> None:
                    try:
                        await self.close()
                    except Exception:
                        logger.exception(
                            "benchmark operation cleanup failed for %s", self.key
                        )

                self._cleanup_task = asyncio.create_task(
                    cleanup(), name=f"benchmark-operation-cleanup:{self.key}"
                )

            parent_task.add_done_callback(cleanup_after_parent)

    async def _renew_operation_lease(
        self, operation: dict[str, Any], stop: asyncio.Event
    ) -> BaseException | None:
        loop = asyncio.get_running_loop()
        lease_deadline = self._initial_lease_deadline or (
            loop.time() + _BENCHMARK_OPERATION_LEASE_TTL_SECONDS
        )
        interval = min(
            _BENCHMARK_OPERATION_LEASE_RENEW_INTERVAL_SECONDS,
            _BENCHMARK_OPERATION_LEASE_TTL_SECONDS / 3,
        )
        retry_delay: float | None = None
        while True:
            remaining = lease_deadline - loop.time()
            if remaining <= 0:
                failure = self._renewal_pending_failure or TimeoutError(
                    "benchmark operation lease expired before renewal was confirmed"
                )
                self._renewal_failure = failure
                self._renewal_state_changed.set()
                return failure
            wait_for = min(retry_delay or interval, remaining)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait_for)
                return self._renewal_pending_failure
            except asyncio.TimeoutError:
                try:
                    token = int(operation["fencing_generation"])
                    renew_started = loop.time()
                    self._renewal_inflight = True
                    self._renewal_state_changed.set()
                    if self._client is not None:
                        renewal_result = await asyncio.to_thread(
                            self._client.operation_transition,
                            self.key,
                            "renew",
                            token,
                            ttl_seconds=_BENCHMARK_OPERATION_LEASE_TTL_SECONDS,
                        )
                    elif self._local is not None:
                        renewal_result = await asyncio.to_thread(
                            self._local.renew_operation,
                            self.key,
                            self.owner,
                            token,
                            _BENCHMARK_OPERATION_LEASE_TTL_SECONDS,
                        )
                    else:
                        raise RuntimeError("benchmark operation registry unavailable")
                    self._renewal_failure = None
                    self._renewal_pending_failure = None
                    lease_deadline = (
                        renew_started + _BENCHMARK_OPERATION_LEASE_TTL_SECONDS
                    )
                    renewed_operation = (
                        renewal_result.get("operation", {})
                        if isinstance(renewal_result, dict)
                        else renewal_result.__dict__
                    )
                    renewed_expiry = renewed_operation.get("lease_expires_at")
                    if renewed_expiry:
                        try:
                            expiry = datetime.fromisoformat(renewed_expiry)
                            remaining = (
                                expiry - datetime.now(timezone.utc)
                            ).total_seconds()
                            lease_deadline = min(
                                lease_deadline,
                                loop.time() + max(0.0, remaining),
                            )
                        except (TypeError, ValueError):
                            pass
                    self._renewal_inflight = False
                    self._renewal_state_changed.set()
                    retry_delay = None
                except Exception as exc:
                    self._renewal_pending_failure = exc
                    self._renewal_inflight = False
                    self._renewal_state_changed.set()
                    if self._authoritative_lease_rejection(exc):
                        self._renewal_failure = exc
                        return exc
                    retry_delay = min(
                        _BENCHMARK_OPERATION_RENEW_RETRY_INITIAL_SECONDS
                        if retry_delay is None
                        else retry_delay * 2,
                        _BENCHMARK_OPERATION_RENEW_RETRY_MAX_SECONDS,
                    )

    @staticmethod
    def _authoritative_lease_rejection(exc: BaseException) -> bool:
        """Recognize registry responses proving this fencing lease is stale."""
        cause: BaseException | None = exc
        while cause is not None:
            if type(cause).__name__ in {
                "OperationLeaseError",
                "OperationTransitionError",
            }:
                return True
            status_code = getattr(getattr(cause, "response", None), "status_code", None)
            if status_code == 409:
                return True
            cause = cause.__cause__
        return False

    async def _await_renewal_resolution(self) -> None:
        """Let transient renewal failures retry before finalizing the operation."""
        task = self._renewal_task
        changed = self._renewal_state_changed
        if task is None or changed is None:
            return
        while not task.done():
            changed.clear()
            if not self._renewal_inflight and self._renewal_pending_failure is None:
                return
            if task.done():
                break
            await changed.wait()

    async def stop_renewal(self) -> BaseException | None:
        """Stop and join renewal before mutating or closing the operation."""
        task = self._renewal_task
        stop = self._renewal_stop
        if task is None or stop is None:
            return self._renewal_failure or self._renewal_pending_failure
        stop.set()
        try:
            failure = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Let an in-flight synchronous renewal finish before its client is
            # closed. The caller's cancellation is still propagated.
            try:
                await task
            finally:
                self._renewal_task = None
                self._renewal_stop = None
            raise
        self._renewal_task = None
        self._renewal_stop = None
        return failure or self._renewal_failure or self._renewal_pending_failure

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
        if self._closed:
            return
        await self.stop_renewal()
        try:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
            if self._local is not None:
                self._local.close()
        finally:
            self._closed = True

    async def terminalize(
        self,
        operation: dict[str, Any],
        outcome: str,
        descriptor: dict[str, Any],
    ) -> bool:
        """Persist a terminal outcome; retry ambiguous writes as indeterminate."""
        await self._await_renewal_resolution()
        renewal_failure = await self.stop_renewal()
        renewal_failed = renewal_failure is not None
        if renewal_failed:
            requested_outcome = outcome
            benchmark_result = descriptor.get("benchmark_result")
            outcome = "indeterminate"
            failure_descriptor: dict[str, Any] = {
                key: value
                for key, value in descriptor.items()
                if key not in {"outcome", "benchmark_result"}
            }
            failure_descriptor.update(
                {
                    "outcome": "lease_renewal_failed",
                    "requested_outcome": requested_outcome,
                    "renewal_error_type": type(renewal_failure).__name__,
                }
            )
            if isinstance(benchmark_result, dict):
                compact_result = {
                    key: benchmark_result[key]
                    for key in (
                        "exit_code",
                        "harness",
                        "run_id",
                        "run_dir",
                        "validation_id",
                    )
                    if key in benchmark_result
                }
                compact_result.update(
                    {
                        "status": "indeterminate",
                        "message": "Lease renewal was not confirmed; reconcile this run",
                    }
                )
                failure_descriptor["benchmark_result"] = compact_result
            descriptor = failure_descriptor
        try:
            await self.transition(outcome, operation, descriptor=descriptor)
            return not renewal_failed
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
                    return False
                except Exception:
                    logger.exception("benchmark operation indeterminate write failed")
        return False


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global \
        _initialized, \
        _ssh, \
        _skill_provider, \
        _crucible_context, \
        _repo_cache, \
        _ticket, \
        _crucible_mcp
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    _skill_provider = await build_skill_provider_async(skill_phase="benchmark")
    _crucible_context = build_crucible_context_gateway(catalog_only=False)
    try:
        _repo_cache = await build_repo_cache()
    except Exception:
        _repo_cache = None
    controller = ticket_controller_host(_ticket)
    ticket_id = str(_ticket.get("id") or os.environ.get("TICKET_ID", ""))
    if controller and ticket_id:
        _crucible_mcp = CrucibleMCPAdapter.from_config(
            controller=controller,
            ticket_id=ticket_id,
            agent_id="benchmark-agent",
        )
    _initialized = True


async def _require_tool_owner(owner_harness: str) -> None:
    """Initialize the authenticated ticket context and enforce tool ownership."""
    await _ensure_init()
    default_harness = getattr(_skill_provider, "default_harness", "")
    require_ticket_harness(
        _ticket,
        owner_harness,
        default_harness=(default_harness if isinstance(default_harness, str) else ""),
    )


def _supports_generic_execution(harness_name: str) -> bool:
    """Check the selected adapter's code-declared generic execution support."""
    # Crucible's execution adapter is implemented directly in this server;
    # other harnesses declare support beside their provider registration.
    if harness_name == _CRUCIBLE_TOOL_OWNER:
        return True
    if hasattr(_skill_provider, "supports_generic_execution_for"):
        return _skill_provider.supports_generic_execution_for(harness_name)
    provider = _skill_provider
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
    return bool(getattr(provider, "generic_execution_supported", False))


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
        if (plan_step_params(ticket) or {}).get("workload_group") and record.get(
            "execution_plan_step_id"
        ) != str(_resolve_current_step_id(ticket)):
            return None, "validation token belongs to a different execution plan step"
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


async def _resolve_legacy_crucible_validation(
    *,
    ticket_id: str,
    validation_id: str,
    record: dict[str, Any],
    controller: str,
    harness: str,
) -> str:
    intent_key, intent_hash, _ = _benchmark_intent_identity(
        ticket_id,
        validation_id,
        record,
        controller,
        harness,
        record.get("run_command", "crucible run"),
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
        result = (
            _replay_operation_result(ticket_id, cached)
            if isinstance(cached, dict)
            else {"status": operation_record.get("terminal_outcome", "indeterminate")}
        )
        await operation_guard.close()
        return json.dumps(
            {
                **result,
                "operation_id": intent_key,
                "existing_operation": True,
                "legacy_execution": True,
            }
        )
    if acquisition == "in_progress":
        await operation_guard.close()
        return json.dumps(
            {
                "status": "existing_operation",
                "operation_id": intent_key,
                "operation": operation_record,
                "legacy_execution": True,
                "message": (
                    "The legacy CLI run is already in flight and was not "
                    "relaunched under MCP."
                ),
            }
        )
    if acquisition != "acquired":
        await operation_guard.close()
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "operation_registry_unavailable",
                "message": "Benchmark operation registry unavailable",
            }
        )
    rejection = {
        "status": "rejected",
        "harness": harness,
        "validation_id": validation_id,
        "reason_code": "mcp_revalidation_required",
        "message": (
            "This validation predates Crucible MCP contract v3. Revalidate "
            "the exact run document before execution."
        ),
    }
    await operation_guard.terminalize(
        operation_record,
        "fail",
        {"benchmark_result": rejection},
    )
    await operation_guard.close()
    return json.dumps(rejection)


def _controller_host() -> str | None:
    """Return the ticket's explicit Crucible controller host."""
    return ticket_controller_host(_ticket)


# Skill / Doc tools (no SSH)
# ---------------------------------------------------------------------------


@mcp.tool(description=SKILL_GATEWAY_TOOL_DESCRIPTION)
async def get_skill_context(
    subject: str,
    operation: str = "bootstrap",
    ref: str = "",
    path: str = "",
    from_ref: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
    cursor: str = "",
) -> str:
    """Retrieve guidance through server-owned subject and source bindings."""
    await _ensure_init()
    runfile_contract = (
        _crucible_context.get_runfile_contract()
        if subject == "harness/crucible" and _crucible_context is not None
        else None
    )
    response_overhead_bytes = (
        len(
            json.dumps(
                {"runfile_contract": runfile_contract},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        if runfile_contract is not None and operation in {"list", "search"}
        else 0
    )
    response = await skill_context_gateway(
        _skill_provider,
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="benchmark-agent",
        phase="benchmark",
        ssh=_ssh,
        controller_host=_controller_host(),
        mcp_adapter=_crucible_mcp,
        subject=subject,
        operation=operation,
        ref=ref,
        path=path,
        from_ref=from_ref,
        query=query,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
        cursor=cursor,
        response_overhead_bytes=response_overhead_bytes,
        local_skills_dir=SKILLS_DIR,
        repo_cache=_repo_cache,
    )
    if subject == "harness/crucible":
        if (
            operation in {"list", "search"}
            and type(max_bytes) is int
            and len(response.encode("utf-8")) + response_overhead_bytes - 1 > max_bytes
        ):
            too_small = {
                "found": False,
                "operation": operation,
                "reason": "page_too_small",
                "message": (
                    "max_bytes cannot fit the run-file contract and complete result envelope."
                ),
            }
            error_response = json.dumps(
                too_small, ensure_ascii=False, separators=(",", ":")
            )
            if len(error_response.encode("utf-8")) > max_bytes:
                error_response = json.dumps(
                    {"reason": "page_too_small"}, ensure_ascii=False
                )
            if len(error_response.encode("utf-8")) > max_bytes:
                return "{}"
            return error_response
        return _with_crucible_runfile_contract(response, runfile_contract)
    return response


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read local skill documents for harnesses that still use the legacy fallback."""
    await _ensure_init()
    results = []
    for doc in docs:
        harness = doc.get("harness") or doc.get("category", "")
        filename = doc.get("filename") or doc.get("name", "")
        if isinstance(harness, str) and harness.strip().lower() == "crucible":
            results.append(
                {
                    "found": False,
                    "harness": harness,
                    "filename": filename,
                    "message": (
                        "Use get_skill_context(subject='harness/crucible', "
                        "operation='bootstrap')."
                    ),
                }
            )
        elif harness in {"kube-burner", "k8s-netperf"}:
            results.append(
                {
                    "found": False,
                    "harness": harness,
                    "filename": filename,
                    "message": (
                        f"Use get_skill_context(subject='harness/{harness}', "
                        "operation='bootstrap')."
                    ),
                }
            )
        else:
            results.extend(read_skill_documents(SKILLS_DIR, [doc]))
    return json.dumps(results)


@mcp.tool()
async def list_harness_docs(harness: str) -> str:
    """List documentation files available for a benchmark harness. Returns file paths and sizes. Use this to discover what reference material is available before constructing a run file."""
    await _ensure_init()
    if isinstance(harness, str) and harness.strip().lower() == "crucible":
        return json.dumps(
            {
                "found": False,
                "message": "Use get_skill_context(subject='harness/crucible', operation='bootstrap').",
            }
        )
    if harness in {"kube-burner", "k8s-netperf"}:
        return json.dumps(
            {
                "docs": [],
                "message": f"Use get_skill_context(subject='harness/{harness}', operation='bootstrap').",
            }
        )
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
    if isinstance(harness, str) and harness.strip().lower() == "crucible":
        return json.dumps(
            {
                "found": False,
                "message": "Use get_skill_context(subject='harness/crucible', operation='bootstrap').",
            }
        )
    if harness in {"kube-burner", "k8s-netperf"}:
        return json.dumps(
            {
                "found": False,
                "message": f"Use get_skill_context(subject='harness/{harness}', operation='bootstrap').",
            }
        )
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
    """Get execution settings for the harness that owns the benchmark. Crucible returns approved controller, endpoint, and default fields; deterministic action tools consume service-only commands. Read applicable guidance through get_skill_context. Other organization subjects require an approved view schema; unconfigured harnesses retain their legacy response."""
    await _ensure_init()
    if harness_name == "crucible" or organization_manages_harness(
        _skill_provider, harness_name
    ):
        try:
            execution = await skill_config_view(
                _skill_provider, harness_name, "execution", "benchmark"
            )
        except ValueError:
            return json.dumps(
                {
                    "harness": harness_name,
                    "found": False,
                    "reason": "configuration_view_unavailable",
                }
            )
        return json.dumps(
            {"harness": harness_name, "found": bool(execution), **execution}
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
    if "default_userenv" in execution:
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
                    'Use get_skill_context(subject="harness/crucible", operation="bootstrap" or '
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
                    "Use get_skill_context(subject='harness/crucible') to read multiplex.json "
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
    if _crucible_context is None:
        raise RuntimeError("Crucible context provider was not initialized")
    if operation not in {"bootstrap", "read", "search"}:
        return _with_crucible_runfile_contract(
            json.dumps(
                {
                    "found": False,
                    "operation": operation,
                    "reason": "unsupported_operation",
                    "guidance": "Use bootstrap, read, or search.",
                }
            )
        )
    controller_host = _controller_host()
    if _ssh is None or not controller_host:
        return _with_crucible_runfile_contract(
            json.dumps(
                {
                    "found": False,
                    "operation": operation,
                    "reason": "controller_not_identified",
                }
            )
        )
    return _with_crucible_runfile_contract(
        await controller_context_gateway(
            ssh=_ssh,
            controller_host=controller_host,
            mcp_adapter=_crucible_mcp,
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
    )


def _with_crucible_runfile_contract(
    response: str, runfile_contract: dict[str, Any] | None = None
) -> str:
    """Attach provider-owned run-file requirements to model-facing context."""
    if _crucible_context is None:
        raise RuntimeError("Crucible context provider was not initialized")
    result = json.loads(response)
    if not isinstance(result, dict):
        raise ValueError("Crucible context response must be a JSON object")
    result["runfile_contract"] = (
        runfile_contract
        if runfile_contract is not None
        else _crucible_context.get_runfile_contract()
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


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
    provider = _crucible_context
    if provider is None or not hasattr(provider, "get_benchmark_context"):
        return json.dumps(
            {
                "found": False,
                "benchmark": benchmark,
                "reason": "crucible_provider_unavailable",
            }
        )

    controller_host = _controller_host()
    if controller_host:
        if operation not in {"bootstrap", "list", "read", "search"}:
            return json.dumps(
                {
                    "found": False,
                    "operation": operation,
                    "reason": "unsupported_operation",
                    "guidance": "Use bootstrap, read, list, or search.",
                }
            )
        gateway_operation = "manifest" if operation == "list" else operation
        return await controller_context_gateway(
            ssh=_ssh,
            controller_host=controller_host,
            mcp_adapter=_crucible_mcp,
            ticket_id=os.environ.get("TICKET_ID", ""),
            agent_name="benchmark-agent",
            phase=phase,
            benchmark=benchmark,
            operation=gateway_operation,
            path=path,
            query=query,
            include_alternates=include_alternates,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )

    if operation not in {"context", "bootstrap", "list", "read", "search"}:
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "unsupported_operation",
                "guidance": "Use context, bootstrap, read, list, or search.",
            }
        )
    if operation != "context":
        return await crucible_context_gateway(
            provider,
            ticket_id=os.environ.get("TICKET_ID", ""),
            agent_name="benchmark-agent",
            phase=phase,
            benchmark=benchmark,
            operation=operation,
            namespace=namespace,
            path=path,
            subject_area=subject_area,
            include_alternates=include_alternates,
            query=query,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )

    result = await provider.get_benchmark_context(benchmark)
    ticket_id = os.environ.get("TICKET_ID", "")
    if result.get("found") and ticket_id:
        from providers.workspace.manager import WorkspaceManager

        manager = WorkspaceManager(
            ticket_id=ticket_id, agent_name="benchmark-agent", phase=phase
        )
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
        manifest = {
            "schema_version": 1,
            "phase": phase,
            "policy": "github_before_controller_assignment",
            "documents": [],
        }
        result["workspace"] = {
            "effective_context": manager.save_effective_context(manifest),
            "effective_source": "github",
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
    subject-scoped schema from ``get_skill_context``.
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
                    "Use get_skill_context(subject='harness/crucible') to read the tool "
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
                    "get_skill_context(subject='harness/crucible') to discover and read them."
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


async def _autoconsume_runfile_approval(
    ticket: dict[str, Any],
    validation_id: str | None,
    persisted_approval_id: str | None = None,
) -> dict[str, Any]:
    """Create, approve, and consume a run-file approval without a pause.

    Honors the ``user_pre_run_approval=false`` directive (#1232): the
    prompt promises a run without a human pause, while the durable
    validation work (#788) requires every execution to carry a bound,
    auditable approval. This produces the identical create → approve →
    consume trail a human decision would, marked as automatic.

    Returns ``{"status": "ok", "approval_request_id"}`` on success,
    ``{"status": "not_eligible"}`` when the directive does not opt
    out of the pause, and ``{"status": "rejected", "detail"}`` when
    the opt-out is set but the durable approval could not be produced
    (the caller keeps its hard gate in that case). The approval is left
    approved-and-unconsumed: ``execute_benchmark`` performs the single
    atomic consume on its normal path, preserving the exactly-once
    invariant. A consumed approval is reused only when its ID was persisted
    on the same prepared operation before the consume request.
    """
    ticket_id = ticket.get("id") or os.environ.get("TICKET_ID", "")
    if not ticket_id:
        return {"status": "not_eligible"}
    directives = (ticket.get("custom_fields") or {}).get("directives") or {}
    if directives.get("user_pre_run_approval") is not False:
        return {"status": "not_eligible"}

    cf = ticket.get("custom_fields") or {}
    manifest = cf.get("benchmark_validations") or {}
    records = manifest.get("records") if isinstance(manifest, dict) else {}
    record = records.get(validation_id) if validation_id and records else None
    if not isinstance(record, dict):
        return {
            "status": "rejected",
            "detail": (
                "no validation record for automatic approval; call "
                "validate_benchmark first"
            ),
        }

    from providers.tracing import current_trace_context

    context = current_trace_context()
    # Inside this MCP tool call the trace context carries the invocation
    # identity but usually no tool-call id (that lives in the agent's
    # per-call child context). The approvals API requires both in the
    # trusted fence headers, so a synthetic id marks the auto-approval —
    # the same pattern the deterministic kernel runner uses.
    invocation_id = (
        str(context.invocation_id) if context and context.invocation_id else ""
    )
    tool_call_id = (
        context.tool_call_id
        if context and context.tool_call_id
        else "execute-benchmark-autoapproval"
    )
    claim_id = (
        os.environ.get("AGENTIC_PERF_CLAIM_ID")
        or (cf.get("claim") or {}).get("claim_id")
        or ""
    )
    automatic_summary = "Automatic run-file approval (user_pre_run_approval=false)"
    create_body = {
        "kind": "benchmark_run_file",
        "validation_id": validation_id,
        "presented_run_file_digest": record.get("runfile_fingerprint"),
        "execution_intent_digest": record.get("execution_intent_digest"),
        "execution_intent_id": record.get("execution_intent_id"),
        "attempt_id": record.get("attempt_id"),
        "summary": automatic_summary,
        "invocation_id": invocation_id or None,
        "tool_call_id": tool_call_id or None,
        "session_id": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"),
        "session_epoch": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH"),
        "waiter_owner": invocation_id or None,
        "ticket_attempt": claim_id or None,
        "claim_id": claim_id or None,
    }
    intent_binding = {"ticket_id": ticket_id}
    intent_binding.update(
        {
            key: create_body[key]
            for key in (
                "kind",
                "validation_id",
                "presented_run_file_digest",
                "execution_intent_digest",
                "summary",
            )
        }
    )
    fence_binding = {
        key: create_body[key]
        for key in ("session_id", "session_epoch", "claim_id", "ticket_attempt")
    }

    def same_automatic_intent(item: Any) -> bool:
        return isinstance(item, dict) and all(
            item.get(key) == value for key, value in intent_binding.items()
        )

    def reusable_approval(approvals: Any) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(approvals, list):
            return None, "approval list is malformed"
        matches = [item for item in approvals if same_automatic_intent(item)]
        if not matches:
            return None, None
        if len(matches) != 1:
            return None, f"expected one automatic approval, found {len(matches)}"
        matched = matches[0]
        if (
            persisted_approval_id
            and matched.get("approval_request_id") != persisted_approval_id
        ):
            return (
                None,
                "existing automatic approval differs from the operation approval ID",
            )
        if any(matched.get(key) != value for key, value in fence_binding.items()):
            return (
                None,
                "existing automatic approval has a stale session or claim fence",
            )
        if matched.get("status") not in {"pending", "approved"}:
            return None, "existing automatic approval is not pending or approved"
        if matched.get("consumed_at") is not None and not (
            persisted_approval_id
            and matched.get("approval_request_id") == persisted_approval_id
            and matched.get("status") == "approved"
        ):
            return None, "existing automatic approval was already consumed"
        approval_id = matched.get("approval_request_id")
        if not isinstance(approval_id, str) or not approval_id:
            return None, "existing automatic approval has no ID"
        return matched, None

    def bound_approved(item: Any, approval_id: str) -> bool:
        return (
            same_automatic_intent(item)
            and item.get("approval_request_id") == approval_id
            and item.get("status") == "approved"
            and item.get("consumed_at") is None
            and all(item.get(key) == value for key, value in fence_binding.items())
        )

    from agents.server_utils import ticket_state_headers

    headers = ticket_state_headers()
    if invocation_id:
        headers["X-Agentic-Perf-Invocation-Id"] = invocation_id
    if tool_call_id:
        headers["X-Agentic-Perf-Tool-Call-Id"] = tool_call_id

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    if any(
        not isinstance(value, str) or not value
        for value in (
            *intent_binding.values(),
            *fence_binding.values(),
            invocation_id,
            tool_call_id,
        )
    ):
        return {
            "status": "rejected",
            "detail": "automatic approval requires complete intent and fence identity",
        }

    async def read_existing(client: Any) -> tuple[dict[str, Any] | None, str | None]:
        listed = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}/approvals")
        if listed.status_code >= 300:
            raise RuntimeError(f"listing failed ({listed.status_code})")
        return reusable_approval(listed.json().get("approvals"))

    try:
        from providers.execution import AuditedAsyncHTTPClient

        async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
            try:
                matched, match_error = await read_existing(client)
            except Exception as exc:
                return {
                    "status": "rejected",
                    "detail": f"approval lookup failed: {exc}",
                    "retryable": True,
                }
            if match_error:
                return {"status": "rejected", "detail": match_error}
            if matched is None:
                if persisted_approval_id:
                    return {
                        "status": "rejected",
                        "detail": "operation approval ID was not found",
                        "retryable": True,
                    }
                creation_error = ""
                try:
                    created = await client.post(
                        f"{store_url}/api/v1/tickets/{ticket_id}/approvals",
                        json=create_body,
                    )
                except Exception as exc:
                    # Creation may have committed before its response was lost.
                    creation_error = f"approval creation response lost: {exc}"
                else:
                    if 300 <= created.status_code < 500:
                        return {
                            "status": "rejected",
                            "detail": (
                                f"approval creation failed ({created.status_code}): "
                                f"{created.text[:200]}"
                            ),
                        }
                    if created.status_code >= 500:
                        creation_error = (
                            f"approval creation failed ({created.status_code})"
                        )
                    else:
                        try:
                            matched, match_error = reusable_approval([created.json()])
                        except Exception as exc:
                            creation_error = (
                                f"approval creation response was malformed: {exc}"
                            )
                        else:
                            if match_error or matched is None:
                                creation_error = (
                                    match_error
                                    or "approval creation returned an unrelated request"
                                )
                if creation_error:
                    try:
                        matched, match_error = await read_existing(client)
                    except Exception as readback_exc:
                        return {
                            "status": "rejected",
                            "detail": f"{creation_error}; reconciliation failed: {readback_exc}",
                            "retryable": True,
                        }
                    if match_error or matched is None:
                        return {
                            "status": "rejected",
                            "detail": f"{creation_error}; {match_error or 'no bound approval found'}",
                            "retryable": True,
                        }
            if matched is not None:
                approval_id = matched["approval_request_id"]
            if matched is not None and matched.get("consumed_at") is not None:
                return {
                    "status": "ok",
                    "approval_request_id": approval_id,
                    "already_consumed": True,
                }
            already_approved = matched is not None and matched["status"] == "approved"

            resolution_error = ""
            try:
                if already_approved:
                    resolved = None
                else:
                    resolved = await client.post(
                        f"{store_url}/api/v1/tickets/{ticket_id}"
                        f"/approvals/{approval_id}/resolve",
                        json={
                            "decision": "approved",
                            "reason": "automatic approval per user_pre_run_approval=false",
                            "validation_id": validation_id,
                            "presented_run_file_digest": record.get(
                                "runfile_fingerprint"
                            ),
                            "execution_intent_digest": record.get(
                                "execution_intent_digest"
                            ),
                        },
                    )
                if resolved is not None and resolved.status_code >= 300:
                    resolution_error = (
                        f"approval resolution failed ({resolved.status_code}): "
                        f"{resolved.text[:200]}"
                    )
                elif resolved is not None and not bound_approved(
                    resolved.json(), approval_id
                ):
                    resolution_error = "approval resolution response did not confirm the bound approval"
            except Exception as exc:
                resolution_error = f"approval resolution response lost: {exc}"

            if resolution_error:
                # A response can be lost after the store commits the decision.
                # Reconcile only the ID returned by create and its exact intent.
                try:
                    listed = await client.get(
                        f"{store_url}/api/v1/tickets/{ticket_id}/approvals"
                    )
                    if listed.status_code >= 300:
                        raise RuntimeError(f"listing failed ({listed.status_code})")
                    approvals = listed.json().get("approvals", [])
                    confirmed = any(
                        bound_approved(item, approval_id) for item in approvals
                    )
                except Exception as exc:
                    return {
                        "status": "rejected",
                        "detail": f"{resolution_error}; reconciliation failed: {exc}",
                        "retryable": True,
                    }
                if not confirmed:
                    return {
                        "status": "rejected",
                        "detail": f"{resolution_error}; approval was not confirmed",
                        "retryable": True,
                    }
    except Exception as exc:
        return {
            "status": "rejected",
            "detail": f"automatic approval failed: {exc}",
        }

    try:
        await _add_execution_note(
            ticket_id,
            f"**Run-file approval granted automatically** "
            f"(user_pre_run_approval=false): {approval_id} "
            f"for validation {validation_id}.",
        )
    except Exception:
        logger.warning("[benchmark] auto-approval comment failed for %s", ticket_id)
    return {"status": "ok", "approval_request_id": approval_id}


def plan_step_params(ticket: dict[str, Any]) -> dict[str, Any] | None:
    """Return the current plan step's params from the ticket's plan state."""
    cf = ticket.get("custom_fields") or {}
    plan = cf.get("execution_plan") or {}
    current_id = _resolve_current_step_id(ticket)
    if current_id is None:
        return None
    steps = plan.get("steps") or []
    step = next(
        (s for s in steps if isinstance(s, dict) and s.get("id") == current_id), None
    )
    return (step or {}).get("params") or None


def _check_plan_mv_params(
    ticket: dict[str, Any],
    run_file: dict[str, Any] | None,
    plan_mv_params: dict[str, Any] | None,
    *,
    enforce_no_extras: bool = False,
) -> str:
    """Return a reason when the validated runfile does not match the plan's mv_params.

    The plan step's ``mv_params`` is the deterministic statement of what the
    benchmark must vary; the LLM-built run file must carry them verbatim in
    its ``mv-params.sets[0].params``. This is the enforcement behind "the
    plan is the intent" — it is what catches a dropped ``seconds`` value
    that would otherwise silently run the harness default (600s instead of
    the planned 30s in PERF-E47D0F45).
    """
    # Harness-neutral: the run file under execution is passed in
    # directly (from the validation record for crucible, or the
    # caller-supplied file for generic adapters).
    plan_mv_params = plan_mv_params or {}
    if not plan_mv_params and not enforce_no_extras:
        return ""
    run_file = run_file or {}
    benchmarks = run_file.get("benchmarks") or []
    for benchmark in benchmarks:
        if not isinstance(benchmark, dict):
            return "run file contains a malformed benchmark entry"
        mv_params = benchmark.get("mv-params") or {}
        if not isinstance(mv_params, dict):
            return "run file contains malformed mv-params"
        sets = mv_params.get("sets") or [{}]
        if not isinstance(sets, list):
            return "run file contains malformed mv-params sets"
        for param_set in sets:
            if not isinstance(param_set, dict):
                return "run file contains a malformed mv-params set"
            params = param_set.get("params") or []
            if not isinstance(params, list) or any(
                not isinstance(param, dict) or not param.get("arg") for param in params
            ):
                return "run file contains malformed mv-params"
            names = [str(param["arg"]) for param in params]
            if len(names) != len(set(names)):
                return "run file contains duplicate mv-params"
            runfile_params = {str(param["arg"]): param.get("vals") for param in params}
            for key, expected in plan_mv_params.items():
                expected_vals = expected if isinstance(expected, list) else [expected]
                actual = runfile_params.get(str(key))
                if actual != [str(value) for value in expected_vals]:
                    return (
                        f"run file mv-params does not match the plan: plan requires "
                        f"{key}={expected_vals!r}, run file has {actual!r} (or omits it)"
                    )
            extras = sorted(set(runfile_params) - {str(key) for key in plan_mv_params})
            if extras and (plan_mv_params or enforce_no_extras):
                return (
                    "run file varies parameters the plan does not name: "
                    f"{', '.join(extras)}"
                )
    return ""


def _resolve_current_step_id(ticket: dict[str, Any]) -> int | None:
    """Derive the executing step from the ticket's own plan state.

    The authoritative source is ``execution_plan.current_step`` — in-band,
    written by the orchestrator when the plan advances. Returns None only
    when the ticket carries no plan at all (external/legacy runs).
    """
    cf = ticket.get("custom_fields") or {}
    plan = cf.get("execution_plan") or {}
    current = plan.get("current_step")
    if isinstance(current, int) and not isinstance(current, bool):
        return current
    return None


def _check_workload_group_drift(
    ticket: dict[str, Any], runfile_fingerprint: str | None
) -> str:
    """Return a drift reason when this execution would break group identity.

    Benchmark steps carrying the same ``params.workload_group`` must run
    an identical runfile: earlier completed steps in the group record the
    runfile fingerprint their execution used (written by the orchestrator's
    step-results capture), and this execution's validated runfile must
    match. Returns "" when the check passes or does not apply.

    Fail-closed (review round 2): the current step comes from the ticket's
    own ``execution_plan.current_step``, never an environment variable.
    A kernel-comparison ticket whose executing step cannot be determined
    is a rejection, not a bypass.
    """
    cf = ticket.get("custom_fields") or {}
    plan = cf.get("execution_plan") or {}
    if not plan.get("steps"):
        return ""  # no plan: external run, group enforcement does not apply
    current_id = _resolve_current_step_id(ticket)
    if current_id is None:
        return (
            "the executing plan step could not be determined from the "
            "ticket's execution_plan.current_step; refusing to run a "
            "workload-group benchmark without a verified step identity"
        )
    steps = plan.get("steps") or []
    current = next((s for s in steps if s.get("id") == current_id), None)
    if not isinstance(current, dict):
        return (
            f"execution_plan.current_step {current_id} does not match any "
            "plan step; refusing to run a workload-group benchmark "
            "without a verified step identity"
        )
    group = (current.get("params") or {}).get("workload_group")
    if not group:
        return ""

    # Harness-neutral: the caller supplies the fingerprint of the run
    # file actually being executed — from the validation record (crucible)
    # or from the caller-supplied run file (generic adapters). This keeps
    # the identity checks from being Crucible-only (review round 2).
    this_fingerprint = runfile_fingerprint or ""
    if not this_fingerprint:
        return "workload-group benchmark has no verified execution identity"

    for step in steps:
        if not isinstance(step, dict) or step.get("id", -1) >= current_id:
            continue
        if (step.get("params") or {}).get("workload_group") != group:
            continue
        results = step.get("results") or {}
        prior = results.get("runfile_fingerprint")
        if not prior:
            # A feature-created plan must establish identity on every
            # group step: a missing fingerprint means the earlier run's
            # identity was never proven, so comparison enforcement cannot
            # be satisfied (review round 2 — no silent bypass).
            return (
                f"step {step.get('id')} in workload group {group!r} has no "
                "recorded runfile fingerprint; the earlier run's identity "
                "is unproven. Re-run the earlier benchmark step so its "
                "configuration is recorded before executing this step."
            )
        if prior != this_fingerprint:
            return (
                f"step {current_id} runfile fingerprint {this_fingerprint[:12]}... "
                f"differs from step {step.get('id')} in workload group "
                f"{group!r} ({prior[:12]}...); benchmark steps in the same "
                "group must execute an identical run file. Re-validate with "
                "the exact runfile the group's earlier steps used (same "
                "benchmark parameters, e.g. mv-params)."
            )
    return ""


async def _add_execution_note(ticket_id: str, body: str) -> None:
    """Best-effort audit comment for automatic approvals."""
    from agents.server_utils import ticket_state_headers
    from providers.execution import AuditedAsyncHTTPClient

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    headers = ticket_state_headers()
    async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
        response = await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/comments",
            json={
                "author": f"{os.environ.get('AGENT_NAME', 'benchmark-agent')}/execute_benchmark",
                "body": body,
            },
        )
        response.raise_for_status()


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
    default_harness = getattr(_skill_provider, "default_harness", "")
    harness_name = selected_harness_from_ticket(
        _ticket,
        default_harness=(default_harness if isinstance(default_harness, str) else ""),
    )
    if not harness_name:
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "harness_not_selected",
                "message": (
                    "The ticket must select a harness or inherit the configured "
                    "provider default before benchmark execution; use "
                    "workflow_source for Arcaflow workflows."
                ),
            }
        )
    requested_harness = (
        selected_harness_from_ticket(
            {"custom_fields": {"directives": {"harness": harness}}}
        )
        if harness is not None
        else harness_name
    )
    if requested_harness != harness_name:
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "harness_mismatch",
                "harness": harness_name,
                "message": "Execution must use the harness selected by the ticket.",
            }
        )

    if comparison_step_requested(_ticket) and harness_name != "crucible":
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "workload_group_harness_unsupported",
                "harness": harness_name,
                "message": (
                    "This harness cannot provide the durable execution receipt "
                    "required for a truthful kernel comparison."
                ),
            }
        )

    if not _supports_generic_execution(harness_name):
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "generic_execution_unsupported",
                "harness": harness_name,
                "message": (
                    f"The {harness_name!r} harness has no generic benchmark "
                    "execution adapter. Use its owning execution tool."
                ),
            }
        )

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
                harness_name,
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
                        cached = _replay_operation_result(ticket_id, cached)
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
    run_uuid = uuid.uuid4().hex[:8]
    step_params = plan_step_params(active_check) or {}
    in_workload_group = bool(step_params.get("workload_group"))
    comparison_step = comparison_step_requested(active_check)
    if comparison_step and not in_workload_group:
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "workload_group_step_missing",
                "message": "The comparison's current benchmark step is not identifiable.",
            }
        )
    if in_workload_group and harness_name != "crucible":
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "workload_group_harness_unsupported",
                "harness": harness_name,
                "message": (
                    "This harness cannot provide the durable execution receipt "
                    "required for a truthful kernel comparison."
                ),
            }
        )

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
        ticket_id = str(active_check.get("id") or os.environ.get("TICKET_ID", ""))
        if record.get("execution_transport") != "crucible_mcp":
            return await _resolve_legacy_crucible_validation(
                ticket_id=ticket_id or "external",
                validation_id=validation_id,
                record=record,
                controller=controller,
                harness=harness_name,
            )
        if _crucible_mcp is None:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "validation_id": validation_id,
                    "reason_code": "unsupported_controller",
                    "message": (
                        "The assigned Crucible controller does not support "
                        "MCP contract v3 execution."
                    ),
                }
            )
        expected_idempotency_key = f"{ticket_id or 'external'}:crucible:{validation_id}"
        if (
            record.get("mcp_contract_version") != "3"
            or record.get("crucible_idempotency_key") != expected_idempotency_key
        ):
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "validation_id": validation_id,
                    "reason_code": "mcp_revalidation_required",
                    "message": (
                        "This validation predates Crucible MCP contract v3. "
                        "Revalidate the exact run document before execution."
                    ),
                }
            )
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
    if harness_name == "crucible":
        contract_errors = crucible_runfile_contract_errors(run_file)
        if contract_errors:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": harness_name,
                    "reason_code": "invalid_runfile_contract",
                    "message": (
                        f"Run-file failed schema validation: {contract_errors}"
                    ),
                }
            )

    if run_command is not None and record.get("execution_transport") != "crucible_mcp":
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
    approval_already_consumed = False
    autoapproval_eligible = (
        harness_name == "crucible"
        and bool(active_check.get("id") or os.environ.get("TICKET_ID"))
        and not approval_request_id
        and ((active_check.get("custom_fields") or {}).get("directives") or {}).get(
            "user_pre_run_approval"
        )
        is False
    )
    if validation_id and (approval_request_id or autoapproval_eligible):
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
                cached = _replay_operation_result(ticket_id, cached)
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
        if harness_name == "crucible":
            await operation_guard.start_renewal(operation_record)
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

    # Workload-group immutability (#998): benchmark steps in the same
    # workload_group must execute an identical runfile, and a plan step
    # that names mv_params must see them verbatim in the validated run
    # file. Checked here — immediately before the approval gate and any
    # side effect — because a mismatch means the LLM rebuilt the run file
    # differently than planned (observed in PERF-E47D0F45: one run
    # silently executed the sleep benchmark's 600s default instead of the
    # planned 30s, making the A/B comparison invalid).
    # Harness-neutral run-file identity: crucible resolves the run file
    # from the validation record; generic adapters execute the
    # caller-supplied run_file directly. Both must satisfy the same
    # group-identity and plan-parameter checks (review round 2).
    if run_file is not None:
        effective_run_file = run_file
        current_fingerprint = _runfile_fingerprint(run_file)
    else:
        cf_exec = active_check.get("custom_fields") or {}
        manifest_exec = cf_exec.get("benchmark_validations") or {}
        records_exec = (
            manifest_exec.get("records", {}) if isinstance(manifest_exec, dict) else {}
        )
        record_exec = records_exec.get(validation_id) if validation_id else None
        effective_run_file = (
            record_exec.get("run_file") if isinstance(record_exec, dict) else None
        )
        current_fingerprint = (
            record_exec.get("runfile_fingerprint")
            if isinstance(record_exec, dict)
            else None
        )
    effective_fingerprint = (
        comparison_fingerprint(record) if in_workload_group else current_fingerprint
    )
    drift = _check_workload_group_drift(active_check, effective_fingerprint)
    if drift:
        rejection = {
            "status": "rejected",
            "reason_code": "workload_drift",
            "message": drift,
        }
        if operation_guard and operation_owned:
            await operation_guard.terminalize(
                operation_record,
                "fail",
                {"outcome": "workload_drift", "benchmark_result": rejection},
            )
            await operation_guard.close()
        return json.dumps(rejection)
    plan_mv = step_params.get("mv_params")
    if plan_mv or in_workload_group:
        mv_error = _check_plan_mv_params(
            active_check,
            effective_run_file,
            plan_mv,
            enforce_no_extras=in_workload_group,
        )
        if mv_error:
            rejection = {
                "status": "rejected",
                "reason_code": "runfile_params_mismatch",
                "message": mv_error,
            }
            if operation_guard and operation_owned:
                await operation_guard.terminalize(
                    operation_record,
                    "fail",
                    {
                        "outcome": "runfile_params_mismatch",
                        "benchmark_result": rejection,
                    },
                )
                await operation_guard.close()
            return json.dumps(rejection)

    # Claim the execution intent before creating an automatic approval. The
    # acquired lease remains held through approval consumption and execution.
    if (
        harness_name == "crucible"
        and (active_check.get("id") or os.environ.get("TICKET_ID"))
        and not approval_request_id
    ):
        persisted_approval_id = (
            (operation_record.get("external_ids") or {}).get("approval_request_id")
            if operation_record
            else None
        )
        autoapproval = await _autoconsume_runfile_approval(
            active_check, validation_id, persisted_approval_id
        )
        if autoapproval.get("status") == "ok":
            approval_request_id = autoapproval["approval_request_id"]
            approval_already_consumed = autoapproval.get("already_consumed", False)
        else:
            rejection = {
                "status": "rejected",
                "reason_code": "approval_required",
                "message": "ticket-scoped benchmark execution requires approval_request_id",
            }
            if autoapproval.get("detail"):
                rejection["auto_approval_error"] = autoapproval["detail"]
                rejection["message"] += "; automatic approval was attempted and failed"
            if operation_guard and operation_owned:
                if not autoapproval.get("retryable"):
                    await operation_guard.terminalize(
                        operation_record,
                        "fail",
                        {"outcome": "approval_required", "benchmark_result": rejection},
                    )
                await operation_guard.close()
            return json.dumps(rejection)

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
        claim_id = os.environ.get("AGENTIC_PERF_CLAIM_ID") or (
            (active_check.get("custom_fields", {}).get("claim") or {}).get("claim_id")
        )
        consume_body = {
            "validation_id": validation_id,
            "presented_run_file_digest": record.get("runfile_fingerprint"),
            "execution_intent_digest": record.get("execution_intent_digest"),
            "session_id": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"),
            "session_epoch": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH"),
            "ticket_attempt": claim_id,
            "claim_id": claim_id,
        }
        external_ids = (operation_record or {}).get("external_ids") or {}
        persisted_approval_id = external_ids.get("approval_request_id")
        if persisted_approval_id and persisted_approval_id != approval_request_id:
            if operation_guard and operation_owned:
                await operation_guard.close()
            return json.dumps(
                {"status": "rejected", "reason_code": "approval_binding_mismatch"}
            )
        if operation_guard and operation_owned and not persisted_approval_id:
            try:
                operation_record = await operation_guard.transition(
                    "external-id",
                    operation_record,
                    external_ids={
                        **external_ids,
                        "approval_request_id": approval_request_id,
                    },
                )
            except Exception:
                logger.exception("[benchmark] approval ID persistence failed")
                await operation_guard.close()
                return json.dumps(
                    {"status": "rejected", "reason_code": "approval_check_failed"}
                )

        def bound_consumed_approval(item: Any) -> bool:
            expected = {
                "approval_request_id": approval_request_id,
                "ticket_id": ticket_id,
                "kind": "benchmark_run_file",
                **consume_body,
            }
            return (
                isinstance(item, dict)
                and all(isinstance(value, str) and value for value in expected.values())
                and all(item.get(key) == value for key, value in expected.items())
                and item.get("status") == "approved"
                and item.get("consumed_at") is not None
            )

        async def read_consumed(client: Any) -> bool:
            listed = await client.get(
                f"{store_url}/api/v1/tickets/{ticket_id}/approvals"
            )
            if listed.status_code >= 300:
                raise RuntimeError(f"approval lookup failed ({listed.status_code})")
            approvals = listed.json().get("approvals")
            if not isinstance(approvals, list):
                raise RuntimeError("approval lookup returned a malformed list")
            matches = [
                item
                for item in approvals
                if isinstance(item, dict)
                and item.get("approval_request_id") == approval_request_id
            ]
            if len(matches) != 1:
                return False
            return bound_consumed_approval(matches[0])

        try:
            from providers.execution import AuditedAsyncHTTPClient

            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            from agents.server_utils import ticket_state_headers

            headers = ticket_state_headers()
            async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
                if persisted_approval_id:
                    approval_already_consumed = await read_consumed(client)
                if not approval_already_consumed:
                    try:
                        consumed = await client.post(
                            f"{store_url}/api/v1/tickets/{ticket_id}/approvals/"
                            f"{approval_request_id}/consume",
                            json=consume_body,
                        )
                    except Exception:
                        # A lost response can follow a committed consume.
                        approval_already_consumed = await read_consumed(client)
                        if not approval_already_consumed:
                            raise
                    else:
                        if consumed.status_code >= 500 or consumed.status_code == 409:
                            approval_already_consumed = await read_consumed(client)
                            if (
                                consumed.status_code >= 500
                                and not approval_already_consumed
                            ):
                                raise RuntimeError(
                                    "approval consume could not be confirmed"
                                )
                        if (
                            300 <= consumed.status_code < 500
                            and not approval_already_consumed
                        ):
                            if operation_guard and operation_owned:
                                await operation_guard.terminalize(
                                    operation_record, "fail", {"outcome": "rejected"}
                                )
                                await operation_guard.close()
                            return json.dumps(
                                {
                                    "status": "rejected",
                                    "reason_code": "approval_not_approved",
                                }
                            )
        except Exception:
            logger.exception("[benchmark] approval capability consumption failed")
            # The operation is still prepared. An uncertain consume or failed
            # readback must remain reclaimable after its lease expires.
            if operation_guard and operation_owned:
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
            response["output"] = (result.stdout or "")[-6000:]
            local_indexers = [
                endpoint["indexer"]
                for endpoint in config.get("metricsEndpoints", [])
                if isinstance(endpoint, dict)
                and isinstance(endpoint.get("indexer"), dict)
                and endpoint["indexer"].get("type") == "local"
            ]
            result_directories: list[str] = []
            for indexer in local_indexers:
                metrics_directory = indexer.get("metricsDirectory")
                if not metrics_directory:
                    metrics_directory = "collected-metrics-{{.UUID}}"
                metrics_directory = str(metrics_directory).replace(
                    "{{.UUID}}", run_uuid
                )
                result_directory = os.path.normpath(
                    os.path.join(template_dir, metrics_directory)
                )
                if os.path.commonpath([template_dir, result_directory]) == template_dir:
                    result_directories.append(result_directory)

            response["result_directories"] = result_directories
            result_files: list[str] = []
            for result_directory in result_directories:
                listing = await _ssh.run(
                    controller,
                    "find "
                    f"{shlex.quote(result_directory)} "
                    "-maxdepth 2 -type f -name '*.json' -printf '%P\\n' "
                    "2>/dev/null | sort | head -20",
                    timeout=30,
                )
                for relative_path in (listing.stdout or "").splitlines():
                    if relative_path:
                        result_files.append(
                            os.path.join(result_directory, relative_path)
                        )
                    if len(result_files) >= 20:
                        break
                if len(result_files) >= 20:
                    break

            response["result_files"] = result_files
            if result_files:
                # Keep the model-facing payload bounded; the returned file
                # list lets the agent retrieve individual artifacts if needed.
                files_to_read = result_files[:20]
                read_command = (
                    "cat -- "
                    + " ".join(shlex.quote(path) for path in files_to_read)
                    + " 2>/dev/null | head -c 6000"
                )
                metrics_result = await _ssh.run(controller, read_command, timeout=30)
                metrics_content = metrics_result.stdout or ""
                if len(files_to_read) == 1 and len(metrics_content) < 6000:
                    try:
                        response["result_summary"] = json.loads(metrics_content)
                    except json.JSONDecodeError:
                        response["result_summary"] = {
                            "files": files_to_read,
                            "content": metrics_content,
                            "truncated": False,
                        }
                else:
                    response["result_summary"] = {
                        "files": files_to_read,
                        "content": metrics_content,
                        "truncated": len(metrics_content) >= 6000,
                    }
            else:
                response["result_collection"] = {
                    "status": "empty",
                    "message": (
                        "kube-burner exited successfully but no local JSON result "
                        "files were found in the configured metrics directory"
                    ),
                }
        if result.exit_code != 0:
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
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
            response.update(
                _truncated_output_guidance(
                    result.stdout or "",
                    result.stderr or "",
                    controller=controller,
                )
            )
        return json.dumps(response)

    if harness_name == "k8s-netperf":
        config = run_file.get("config", {})
        cli_flags = run_file.get("cli_flags", [])

        template_dir = f"/tmp/k8s-netperf-{run_uuid}"
        config_path = f"{template_dir}/netperf.yml"

        namespace_probe = await _ssh.run(
            controller,
            "kubectl get namespace netperf -o name",
            timeout=30,
        )
        if namespace_probe.exit_code == 0:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": 1,
                    "harness": "k8s-netperf",
                    "message": (
                        "Refusing to run because namespace netperf already exists; "
                        "the harness uses this namespace and cleanup would affect "
                        "resources owned by another user"
                    ),
                }
            )
        if "notfound" not in (namespace_probe.stderr or "").lower():
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": namespace_probe.exit_code or 1,
                    "harness": "k8s-netperf",
                    "message": (
                        "Could not verify that namespace netperf is absent; "
                        "refusing to change the cluster"
                    ),
                    "error": (namespace_probe.stderr or "")[-1000:],
                }
            )

        cluster_nodes = await _ssh.run(
            controller,
            "kubectl get nodes -o name",
            timeout=60,
        )
        if cluster_nodes.exit_code != 0 or not (cluster_nodes.stdout or "").strip():
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": cluster_nodes.exit_code or 1,
                    "harness": "k8s-netperf",
                    "message": "Could not verify that the cluster has any nodes",
                    "error": (cluster_nodes.stderr or "")[-1000:],
                }
            )

        unlabeled_nodes = await _ssh.run(
            controller,
            "kubectl get nodes -l '!node-role.kubernetes.io/worker' -o name",
            timeout=60,
        )
        if unlabeled_nodes.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": unlabeled_nodes.exit_code,
                    "harness": "k8s-netperf",
                    "message": "Could not determine which nodes need the worker label",
                    "error": (unlabeled_nodes.stderr or "")[-1000:],
                }
            )
        nodes_to_restore = [
            node.strip()
            for node in (unlabeled_nodes.stdout or "").splitlines()
            if node.strip()
        ]

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

        created_namespace = False
        added_worker_labels: list[str] = []
        cleanup_errors: list[dict[str, Any]] = []
        response: dict[str, Any] | None = None
        try:
            namespace_create = await _ssh.run(
                controller,
                "kubectl create namespace netperf",
                timeout=60,
            )
            if namespace_create.exit_code != 0:
                response = {
                    "status": "failed",
                    "exit_code": namespace_create.exit_code,
                    "harness": "k8s-netperf",
                    "message": "Could not create the netperf namespace",
                    "error": (namespace_create.stderr or "")[-1000:],
                }
            else:
                created_namespace = True

            if response is None:
                service_account = await _ssh.run(
                    controller,
                    "kubectl create serviceaccount netperf -n netperf",
                    timeout=60,
                )
                if service_account.exit_code != 0:
                    response = {
                        "status": "failed",
                        "exit_code": service_account.exit_code,
                        "harness": "k8s-netperf",
                        "message": "Could not create the netperf service account",
                        "error": (service_account.stderr or "")[-1000:],
                    }

            if response is None:
                for node in nodes_to_restore:
                    label_cmd = shlex.join(
                        [
                            "kubectl",
                            "label",
                            node,
                            "node-role.kubernetes.io/worker=",
                            "--overwrite",
                        ]
                    )
                    label_result = await _ssh.run(
                        controller,
                        label_cmd,
                        timeout=60,
                    )
                    if label_result.exit_code != 0:
                        response = {
                            "status": "failed",
                            "exit_code": label_result.exit_code,
                            "harness": "k8s-netperf",
                            "message": f"Could not add the worker label to {node}",
                            "error": (label_result.stderr or "")[-1000:],
                        }
                        break
                    added_worker_labels.append(node)

            if response is None:
                await _ssh.run(controller, f"mkdir -p {shlex.quote(template_dir)}")
                await _ssh.run(
                    controller,
                    f"cat > {shlex.quote(config_path)} << 'NPEOF'\n"
                    f"{config_content}\nNPEOF",
                )

                flags_str = shlex.join(cli_flags)
                np_cmd = run_command or "k8s-netperf"
                cmd = f"{np_cmd} --config {shlex.quote(config_path)} {flags_str} --json 2>&1"
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
                    response.update(
                        _truncated_output_guidance(
                            result.stdout or "",
                            result.stderr or "",
                            controller=controller,
                        )
                    )
        except Exception as exc:
            logger.exception("[benchmark] k8s-netperf execution failed")
            response = {
                "status": "failed",
                "exit_code": -1,
                "harness": "k8s-netperf",
                "message": "k8s-netperf execution raised an exception",
                "error": str(exc)[:1000],
            }
        finally:
            for node in added_worker_labels:
                unlabel_cmd = shlex.join(
                    ["kubectl", "label", node, "node-role.kubernetes.io/worker-"]
                )
                unlabel = await _ssh.run(controller, unlabel_cmd, timeout=60)
                if unlabel.exit_code != 0:
                    cleanup_errors.append(
                        {
                            "action": f"remove worker label from {node}",
                            "exit_code": unlabel.exit_code,
                            "error": (unlabel.stderr or "")[-500:],
                        }
                    )
            if created_namespace:
                delete_namespace = await _ssh.run(
                    controller,
                    "kubectl delete namespace netperf --wait=true --ignore-not-found",
                    timeout=120,
                )
                if delete_namespace.exit_code != 0:
                    cleanup_errors.append(
                        {
                            "action": "delete created namespace netperf",
                            "exit_code": delete_namespace.exit_code,
                            "error": (delete_namespace.stderr or "")[-500:],
                        }
                    )
            remove_temp = await _ssh.run(
                controller,
                f"rm -rf {shlex.quote(template_dir)}",
                timeout=30,
            )
            if remove_temp.exit_code != 0:
                cleanup_errors.append(
                    {
                        "action": "remove temporary k8s-netperf directory",
                        "exit_code": remove_temp.exit_code,
                        "error": (remove_temp.stderr or "")[-500:],
                    }
                )

        if response is None:
            response = {
                "status": "failed",
                "exit_code": -1,
                "harness": "k8s-netperf",
                "message": "k8s-netperf did not produce a result",
            }
        if cleanup_errors:
            response["cleanup_errors"] = cleanup_errors
            if response.get("status") == "completed":
                response["status"] = "failed"
                response["message"] = (
                    "Benchmark completed, but cluster cleanup was incomplete"
                )
        return json.dumps(response)

    # Crucible is the only server-local default executor. All other harnesses
    # must reach an explicit provider-owned branch above.
    if harness_name != _CRUCIBLE_TOOL_OWNER:
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "generic_execution_unsupported",
                "harness": harness_name,
                "message": "No generic execution adapter is registered for this harness.",
            }
        )

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
                duplicate_result = _replay_operation_result(ticket_id, duplicate_result)
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
        await operation_guard.start_renewal(operation_record)
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

    idempotency_key = str(record.get("crucible_idempotency_key"))
    launch_attempted = False

    def decode_mcp_result(raw: str) -> dict[str, Any]:
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Crucible MCP returned an invalid response") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Crucible MCP returned an invalid response")
        return decoded

    async def finish_execution(
        response: dict[str, Any],
        outcome: str,
        *,
        reconcile: bool = False,
    ) -> str:
        if operation_guard and operation_owned:
            assert operation_record is not None
            try:
                persisted_result, response = _prepare_operation_result(
                    ticket_id, response
                )
            except Exception:
                logger.exception("Could not persist benchmark result payload")
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {"outcome": "result_payload_unavailable"},
                )
                await operation_guard.close()
                if reconcile:
                    await pause_for_reconciliation(
                        "benchmark result could not be stored"
                    )
                return json.dumps(
                    {
                        "status": "indeterminate",
                        "operation_id": intent_key,
                        "reason_code": "result_payload_unavailable",
                    }
                )
            terminal_ok = await operation_guard.terminalize(
                operation_record, outcome, {"benchmark_result": persisted_result}
            )
            await operation_guard.close()
            response["operation_id"] = intent_key
            if not terminal_ok:
                response["status"] = "indeterminate"
                response["message"] = (
                    "Operation result could not be durably recorded; "
                    "reconciliation required"
                )
        if reconcile:
            await pause_for_reconciliation(
                response.get("message", "execution is ambiguous")
            )
        return json.dumps(response)

    try:
        if operation_guard and operation_owned:
            assert operation_record is not None
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
                        "operation_id": intent_key,
                        "reason_code": "launch_boundary_persistence_failed",
                    }
                )

        persisted_plan_handle = (
            (operation_record or {}).get("external_ids") or {}
        ).get("crucible_plan_handle")
        if isinstance(persisted_plan_handle, str) and persisted_plan_handle:
            plan_handle = persisted_plan_handle
        else:
            try:
                raw_plan = await _crucible_mcp.call_tool(
                    "prepare_run",
                    {"document": run_file, "persist": True},
                )
                plan = decode_mcp_result(raw_plan)
                plan_validation = plan.get("validation")
                plan_handle = plan.get("plan_handle")
            except MCPToolCallError as exc:
                if not _is_run_document_too_large(exc):
                    raise
                return await finish_execution(
                    {
                        "status": "failed",
                        "validation_id": validation_id,
                        "execution_transport": "crucible_mcp",
                        "mcp_contract_version": "3",
                        "reason_code": "run_document_too_large",
                        "message": (
                            "The exact validated run document exceeds the "
                            "controller's inline MCP document limit."
                        ),
                    },
                    "fail",
                )
            if (
                not isinstance(plan_validation, dict)
                or plan_validation.get("valid") is not True
                or not isinstance(plan_handle, str)
                or not plan_handle
            ):
                return await finish_execution(
                    {
                        "status": "failed",
                        "harness": "crucible",
                        "validation_id": validation_id,
                        "reason_code": "invalid_run_document",
                        "message": (
                            "The validated run document was rejected during "
                            "MCP planning."
                        ),
                    },
                    "fail",
                )

            if operation_guard and operation_owned:
                assert operation_record is not None
                try:
                    operation_record = await operation_guard.transition(
                        "external-id",
                        operation_record,
                        external_ids={
                            **(operation_record.get("external_ids") or {}),
                            "crucible_plan_handle": plan_handle,
                            "crucible_idempotency_key": idempotency_key,
                        },
                    )
                except Exception:
                    await operation_guard.terminalize(
                        operation_record,
                        "indeterminate",
                        {
                            "outcome": "indeterminate",
                            "external_id_persist_failed": True,
                            "known_external_ids": {
                                "crucible_plan_handle": plan_handle,
                                "crucible_idempotency_key": idempotency_key,
                            },
                        },
                    )
                    await operation_guard.close()
                    await pause_for_reconciliation(
                        "MCP plan identity could not be persisted"
                    )
                    return json.dumps(
                        {
                            "status": "indeterminate",
                            "operation_id": intent_key,
                            "reason_code": "external_id_persistence_failed",
                        }
                    )

        launch_attempted = True
        try:
            raw_start = await _crucible_mcp.call_tool(
                "start_run",
                {
                    "idempotency_key": idempotency_key,
                    "document": run_file,
                    "plan_handle": plan_handle,
                },
            )
        except MCPToolCallError as exc:
            if not _is_expired_plan_handle(exc):
                raise
            raw_plan = await _crucible_mcp.call_tool(
                "prepare_run",
                {"document": run_file, "persist": True},
            )
            plan = decode_mcp_result(raw_plan)
            plan_validation = plan.get("validation")
            retry_handle = plan.get("plan_handle")
            if (
                not isinstance(plan_validation, dict)
                or plan_validation.get("valid") is not True
                or not isinstance(retry_handle, str)
                or not retry_handle
            ):
                raise
            if operation_guard and operation_owned:
                assert operation_record is not None
                operation_record = await operation_guard.transition(
                    "external-id",
                    operation_record,
                    external_ids={
                        **(operation_record.get("external_ids") or {}),
                        "crucible_plan_handle": retry_handle,
                        "crucible_idempotency_key": idempotency_key,
                    },
                )
            plan_handle = retry_handle
            raw_start = await _crucible_mcp.call_tool(
                "start_run",
                {
                    "idempotency_key": idempotency_key,
                    "document": run_file,
                    "plan_handle": plan_handle,
                },
            )
        start_result = decode_mcp_result(raw_start)
        job = start_result.get("job")
        mcp_job_id = job.get("mcp_job_id") if isinstance(job, dict) else ""
        if not isinstance(mcp_job_id, str) or not mcp_job_id:
            await pause_for_reconciliation("MCP job identity was not returned")
            return await finish_execution(
                {
                    "status": "indeterminate",
                    "harness": "crucible",
                    "validation_id": validation_id,
                    "crucible_idempotency_key": idempotency_key,
                    "reason_code": "mcp_job_id_missing",
                    "message": "MCP start succeeded but did not return a job identity.",
                },
                "indeterminate",
                reconcile=False,
            )

        external_ids = {
            **((operation_record or {}).get("external_ids") or {}),
            "crucible_mcp_job_id": mcp_job_id,
            "crucible_idempotency_key": idempotency_key,
        }

        if operation_guard and operation_owned:
            assert operation_record is not None
            try:
                operation_record = await operation_guard.transition(
                    "external-id", operation_record, external_ids=external_ids
                )
            except Exception:
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {
                        "outcome": "indeterminate",
                        "external_id_persist_failed": True,
                        "known_external_ids": external_ids,
                    },
                )
                await operation_guard.close()
                await pause_for_reconciliation(
                    "MCP job identity could not be persisted"
                )
                return json.dumps(
                    {
                        "status": "indeterminate",
                        "operation_id": intent_key,
                        "crucible_mcp_job_id": mcp_job_id,
                        "reason_code": "external_id_persistence_failed",
                    }
                )

        response: dict[str, Any] = {
            "status": "running",
            "validation_id": validation_id,
            "execution_transport": "crucible_mcp",
            "mcp_contract_version": "3",
            "crucible_mcp_job_id": mcp_job_id,
            "crucible_idempotency_key": idempotency_key,
            "harness": "crucible",
        }
        log_offset = 0
        poll_count = 0
        readiness_deadline: float | None = None

        async def fetch_logs() -> str:
            nonlocal log_offset
            raw_logs = await _crucible_mcp.call_tool(
                "get_run_logs",
                {
                    "mcp_job_id": mcp_job_id,
                    "offset": log_offset,
                    "limit": 65_536,
                },
            )
            logs = decode_mcp_result(raw_logs)
            text = logs.get("text")
            next_offset = logs.get("next_offset")
            if isinstance(next_offset, int):
                log_offset = next_offset
            return text if isinstance(text, str) else ""

        async def persist_status_identifiers(current_status: dict[str, Any]) -> bool:
            nonlocal operation_record
            values = {
                key: current_status.get(key)
                for key in ("cdm_run_id", "rickshaw_run_id", "run_directory")
            }
            if not any(isinstance(value, str) and value for value in values.values()):
                return True

            cdm_run_id = values["cdm_run_id"]
            rickshaw_run_id = values["rickshaw_run_id"]
            run_path = values["run_directory"]
            if isinstance(cdm_run_id, str) and cdm_run_id:
                response["run_id"] = cdm_run_id
                response["cdm_run_id"] = cdm_run_id
            if isinstance(rickshaw_run_id, str) and rickshaw_run_id:
                response["rickshaw_run_id"] = rickshaw_run_id
            if isinstance(run_path, str) and run_path:
                response["run_path"] = run_path

            if not (operation_guard and operation_owned):
                return True
            assert operation_record is not None
            status_external_ids = {
                **(operation_record.get("external_ids") or {}),
                "crucible_mcp_job_id": mcp_job_id,
                "crucible_idempotency_key": idempotency_key,
            }
            if isinstance(cdm_run_id, str) and cdm_run_id:
                status_external_ids["cdm_run_id"] = cdm_run_id
                status_external_ids["run_id"] = cdm_run_id
            if isinstance(rickshaw_run_id, str) and rickshaw_run_id:
                status_external_ids["rickshaw_run_id"] = rickshaw_run_id
            if isinstance(run_path, str) and run_path:
                status_external_ids["run_path"] = run_path

            existing_external_ids = operation_record.get("external_ids") or {}
            if all(
                existing_external_ids.get(key) == value
                for key, value in status_external_ids.items()
            ):
                return True

            try:
                operation_record = await operation_guard.transition(
                    "external-id",
                    operation_record,
                    external_ids=status_external_ids,
                )
            except Exception:
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {
                        "outcome": "indeterminate",
                        "external_id_persistence_failed": True,
                        "known_external_ids": status_external_ids,
                    },
                )
                await operation_guard.close()
                await pause_for_reconciliation(
                    "Crucible run identifiers could not be persisted"
                )
                response.update(
                    {
                        "status": "indeterminate",
                        "reason_code": "external_id_persistence_failed",
                        "message": (
                            "The run identifiers returned by get_run_status "
                            "could not be persisted."
                        ),
                    }
                )
                return False
            return True

        while True:
            raw_status = await _crucible_mcp.call_tool(
                "get_run_status", {"mcp_job_id": mcp_job_id}
            )
            status = decode_mcp_result(raw_status)
            state = str(status.get("state", ""))
            result_status = str(status.get("result_status", ""))
            if not await persist_status_identifiers(status):
                return json.dumps(response)

            if state in {
                "queued",
                "starting",
                "running",
                "postprocessing",
                "indexing",
            }:
                poll_count += 1
                if poll_count % 5 == 1:
                    try:
                        logs = await fetch_logs()
                        if logs:
                            await _benchmark_progress(logs[-2000:], poll_count * 2)
                    except Exception:
                        logger.debug("Crucible progress logs were unavailable")
                await asyncio.sleep(2)
                continue

            if state in {"recovery_required", "unknown_after_crash"}:
                response.update(
                    {
                        "status": "indeterminate",
                        "reason_code": state,
                        "message": (
                            "Crucible reported a recovery-required state; "
                            "the run was not relaunched."
                        ),
                    }
                )
                try:
                    response["run_log"] = (await fetch_logs())[-50_000:]
                except Exception:
                    response["log_status"] = "unavailable"
                return await finish_execution(response, "indeterminate", reconcile=True)

            if state == "failed":
                response.update(
                    {
                        "status": "failed",
                        "reason_code": status.get("error_category", "crucible_failed"),
                        "exit_code": status.get("exit_code"),
                        "message": (
                            status.get("error_message")
                            or "Crucible reported a failed run."
                        ),
                    }
                )
                try:
                    response["run_log"] = (await fetch_logs())[-50_000:]
                except Exception:
                    response["log_status"] = "unavailable"
                return await finish_execution(response, "fail")

            if state != "completed":
                response.update(
                    {
                        "status": "indeterminate",
                        "reason_code": "unknown_run_state",
                        "message": f"Crucible returned unknown run state {state!r}.",
                    }
                )
                return await finish_execution(response, "indeterminate", reconcile=True)

            if result_status in {"pending", "not_available"}:
                if readiness_deadline is None:
                    readiness_deadline = (
                        asyncio.get_running_loop().time()
                        + _crucible_mcp.settings.result_readiness_timeout_seconds
                    )
                if asyncio.get_running_loop().time() < readiness_deadline:
                    await asyncio.sleep(2)
                    continue
                response.update(
                    {
                        "status": "indeterminate",
                        "reason_code": "result_readiness_timeout",
                        "message": (
                            "Crucible completed the run but results remained "
                            "pending beyond the configured readiness timeout."
                        ),
                    }
                )
                return await finish_execution(response, "indeterminate", reconcile=True)

            if result_status in {"partial", "unavailable"}:
                response.update(
                    {
                        "status": result_status,
                        "reason_code": f"result_{result_status}",
                        "message": f"Crucible reported {result_status} results.",
                        "indexed_query_status": status.get("indexed_query_status"),
                    }
                )
                return await finish_execution(response, "fail")

            if result_status != "available":
                response.update(
                    {
                        "status": "indeterminate",
                        "reason_code": "unknown_result_status",
                        "message": (
                            f"Crucible returned unknown result status {result_status!r}."
                        ),
                    }
                )
                return await finish_execution(response, "indeterminate", reconcile=True)

            raw_summary = await _crucible_mcp.call_tool(
                "get_run_summary", {"mcp_job_id": mcp_job_id}
            )
            summary_result = decode_mcp_result(raw_summary)
            summary = summary_result.get("summary")
            if not isinstance(summary, dict):
                return await finish_execution(
                    {
                        **response,
                        "status": "failed",
                        "reason_code": "result_summary_invalid",
                        "message": "Crucible returned an invalid result summary.",
                    },
                    "fail",
                )
            response.update(
                {
                    "status": "completed",
                    "result_summary": summary,
                    "indexed_query_status": status.get("indexed_query_status"),
                    "message": "Benchmark completed",
                }
            )
            return await finish_execution(response, "complete")
    except Exception:
        logger.warning("[benchmark] Crucible MCP execution failed", exc_info=True)
        if launch_attempted:
            return await finish_execution(
                {
                    "status": "indeterminate",
                    "validation_id": validation_id,
                    "execution_transport": "crucible_mcp",
                    "mcp_contract_version": "3",
                    "crucible_idempotency_key": idempotency_key,
                    "reason_code": "mcp_execution_outcome_unknown",
                    "message": (
                        "The MCP run launch or polling outcome is ambiguous; "
                        "reconciliation is required and the run was not relaunched."
                    ),
                },
                "indeterminate",
                reconcile=True,
            )
        return await finish_execution(
            {
                "status": "failed",
                "validation_id": validation_id,
                "execution_transport": "crucible_mcp",
                "mcp_contract_version": "3",
                "reason_code": "mcp_execution_failed",
                "message": "The Crucible MCP execution request failed before launch.",
            },
            "fail",
        )

    except asyncio.CancelledError:
        if operation_guard and operation_owned:
            try:
                await operation_guard.terminalize(
                    operation_record,
                    "indeterminate",
                    {
                        "outcome": "indeterminate",
                        "reason_code": "ticket_cancelled_during_polling",
                        "polling_stopped": True,
                        "crucible_stopped": False,
                    },
                )
            finally:
                await operation_guard.close()
        raise


@mcp.tool(meta=ownership_metadata(_CRUCIBLE_TOOL_OWNER))
async def validate_benchmark(
    controller: str,
    run_file: dict,
    harness: str | None = None,
) -> str:
    """Validate a benchmark run-file on its controller without executing it.

    For Crucible, the controller MCP ``validate_run`` tool performs
    controller-side schema, endpoint, benchmark-parameter, and tool-parameter
    validation. This tool never starts a benchmark, deploys endpoints, or
    starts supporting services.
    """
    await _require_tool_owner(_CRUCIBLE_TOOL_OWNER)
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

    contract_errors = crucible_runfile_contract_errors(run_file)
    if contract_errors:
        return json.dumps(
            {
                "status": "invalid",
                "valid": False,
                "harness": harness_name,
                "errors": contract_errors,
            }
        )

    assigned_controller = _controller_host()
    if not assigned_controller or controller != assigned_controller:
        return json.dumps(
            {
                "status": "failed",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "errors": [
                    "Validation controller does not match the ticket's assigned controller"
                ],
            }
        )
    if _crucible_mcp is None:
        return json.dumps(
            {
                "status": "failed",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "errors": ["Crucible MCP contract v3 is unavailable"],
            }
        )

    try:
        ticket_id = os.environ.get("TICKET_ID", "")
        raw_result = await _crucible_mcp.call_tool(
            "validate_run",
            {"document": run_file},
        )
        try:
            result = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError):
            result = {
                "status": "failed",
                "valid": False,
                "errors": ["Crucible returned an invalid validation response"],
            }
        if not isinstance(result, dict):
            result = {
                "status": "failed",
                "valid": False,
                "errors": ["Crucible returned an invalid validation response"],
            }
        errors = result.get("errors", [])
        if not isinstance(errors, list):
            errors = [str(errors)] if errors else []
        output = (
            result.get("output")
            or result.get("message")
            or json.dumps(result, sort_keys=True)
        )
        valid = result.get("valid") is True or result.get("status") == "valid"
        if valid:
            fields = _ticket.get("custom_fields", {}) if _ticket else {}
            params_fingerprint = _compute_params_fingerprint(fields)
            execution_plan_fingerprint = _execution_plan_fingerprint(fields)
            validation_id = await _persist_validated_runfile(
                run_file,
                harness_name,
                controller,
                params_fingerprint,
                execution_plan_fingerprint,
                "crucible_mcp:start_run",
                "crucible_mcp:validate_run",
                output,
                str(
                    result.get(
                        "validator_version",
                        _crucible_mcp.contract_info.get("version", "unknown"),
                    )
                ),
                execution_transport="crucible_mcp",
                mcp_contract_version="3",
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

        details = (
            "; ".join(str(error) for error in errors) or output or ("Validation failed")
        )
        return json.dumps(
            {
                "status": "invalid",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "validation_output": _validation_output_descriptor(ticket_id, output),
                "errors": [details],
                "exit_code": result.get("exit_code"),
            }
        )
    except MCPToolCallError as exc:
        if not _is_run_document_too_large(exc):
            raise
        return json.dumps(
            {
                "status": "failed",
                "valid": False,
                "harness": harness_name,
                "controller": controller,
                "reason_code": "run_document_too_large",
                "errors": [
                    "The run document exceeds the controller's inline MCP document limit."
                ],
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

    if harness in {None, "crucible"}:
        assigned_controller = _controller_host()
        if not assigned_controller or controller != assigned_controller:
            return json.dumps(
                {
                    "status": "error",
                    "reason": "controller_mismatch",
                    "message": (
                        "The controller must match the ticket's assigned "
                        "Crucible controller."
                    ),
                }
            )
        if run_id.startswith("/"):
            return json.dumps(
                {
                    "status": "error",
                    "reason": "invalid_run_id",
                    "message": (
                        "Crucible logs require the MCP job ID; controller "
                        "run paths are not accepted."
                    ),
                }
            )
        if _crucible_mcp is None:
            return json.dumps(
                {
                    "status": "error",
                    "reason": "unsupported_controller",
                    "message": (
                        "The assigned Crucible controller does not support "
                        "MCP contract v3."
                    ),
                }
            )
        try:
            raw_logs = await _crucible_mcp.call_tool(
                "get_run_logs",
                {"mcp_job_id": run_id, "offset": 0, "limit": byte_limit},
            )
            logs = json.loads(raw_logs)
            if not isinstance(logs, dict):
                raise ValueError("invalid log response")
            return json.dumps(
                {
                    "status": "ok",
                    "crucible_mcp_job_id": run_id,
                    "log_lines": logs.get("text", ""),
                    "next_offset": logs.get("next_offset"),
                    "complete": logs.get("complete") is True,
                    "redacted": logs.get("redacted") is True,
                }
            )
        except Exception:
            return json.dumps(
                {
                    "status": "error",
                    "reason": "mcp_transport_error",
                    "message": "The Crucible MCP log request failed.",
                }
            )

    if run_id.startswith("/") and harness == "kube-burner":
        match = re.fullmatch(r"/tmp/kb-([0-9a-f]{8})", run_id)
        if not match:
            return json.dumps(
                {
                    "status": "invalid_run_id",
                    "message": "Expected a kube-burner run ID or its /tmp/kb-<uuid> directory",
                }
            )
        run_uuid = match.group(1)
        run_dir = run_id
    elif run_id.startswith("/"):
        run_dir = run_id
    elif harness == "zathras":
        search_pattern = results_dir_pattern or "/tmp/results_*"
        result = await _ssh.run(
            controller,
            f"ls -dt {search_pattern} 2>/dev/null | head -1",
        )
        run_dir = result.stdout.strip()
    elif harness == "kube-burner":
        match = re.fullmatch(r"kube-burner-([0-9a-f]{8})", run_id)
        if not match:
            return json.dumps(
                {
                    "status": "invalid_run_id",
                    "message": "Expected a kube-burner run ID returned by execute_benchmark",
                }
            )
        run_uuid = match.group(1)
        run_dir = f"/tmp/kb-{run_uuid}"
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
    elif harness == "kube-burner":
        log_path = f"{run_dir}/kube-burner-{run_uuid}.log"
        log_result = await _ssh.run(
            controller,
            f"test -f {shlex.quote(log_path)}"
            f" && tail -c {byte_limit} {shlex.quote(log_path)}",
            timeout=60,
        )
        if log_result.exit_code != 0:
            return json.dumps(
                {
                    "run_dir": run_dir,
                    "log_file": log_path,
                    "status": "not_found",
                    "message": "kube-burner log file was not found for this run",
                }
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


@mcp.tool(meta=ownership_metadata(_BOOT_TIME_TOOL_OWNER))
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
    return await run_boot_time_test(
        BootTimeContext(
            ssh=_ssh,
            repo_cache=_repo_cache,
            ticket=_ticket,
            require_tool_owner=lambda: _require_tool_owner(_BOOT_TIME_TOOL_OWNER),
        ),
        sut_host,
        samples=samples,
        kpi_pattern=kpi_pattern,
        clean_journal=clean_journal,
        description=description,
    )


@mcp.tool()
async def read_artifact_file(
    path: str,
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Read lines from a local artifact file (harness output, serial
    capture, diagnostics).  Use this to inspect the full output when
    the tool result was truncated.

    Args:
        path: Absolute path to the artifact file, or a path relative
            to the ticket's artifact directory (for example
            ``<run_uuid>/harness-output.log``).
        offset: 0-based line number to start reading from.
        limit: Maximum number of lines to return (max 500).

    Returns JSON with the selected lines.  Reads are capped at 256 KiB;
    when a single line exceeds the cap the content is cut short and
    ``truncated`` is true, because that line cannot be paged past.
    """
    await _ensure_init()

    from agents.server_utils import read_artifact_file_impl

    tid = _ticket.get("id", "") if _ticket else ""
    return read_artifact_file_impl(path, offset, limit, ticket_id=tid)


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
