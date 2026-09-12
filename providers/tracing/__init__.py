"""Canonical tracing contracts and causal context helpers."""

from .client import TraceClient, TraceDeliveryError
from .context import (
    TraceContext,
    bind_trace_context,
    child_context,
    current_trace_context,
    new_trace_context,
    reset_trace_context,
)
from .models import (
    SCHEMA_VERSION,
    ActionDescriptor,
    ActionType,
    ErrorDescriptor,
    IdempotencyDescriptor,
    IdempotencyOutcome,
    LifecycleDescriptor,
    LifecycleState,
    MCPIdentity,
    MonotonicTimer,
    OperationOutcome,
    PayloadDescriptor,
    ProducerIdentity,
    RetryKind,
    TraceEventV1,
)
from .operations import AmbiguousOperation, OperationCancelled, operation
from .payloads import (
    PayloadBlobStore,
    PayloadBuilder,
    PayloadStorageError,
    canonicalize_payload,
)
from .runtime import TraceRecorder, trace_headers
from .spool import (
    SpoolBackpressure,
    SpoolCorruption,
    SpoolError,
    TraceSpool,
    drain_abandoned_spools,
)

__all__ = [
    "SCHEMA_VERSION",
    "ActionDescriptor",
    "ActionType",
    "ErrorDescriptor",
    "IdempotencyDescriptor",
    "IdempotencyOutcome",
    "LifecycleDescriptor",
    "LifecycleState",
    "MCPIdentity",
    "MonotonicTimer",
    "OperationOutcome",
    "PayloadDescriptor",
    "PayloadBlobStore",
    "PayloadBuilder",
    "PayloadStorageError",
    "ProducerIdentity",
    "RetryKind",
    "TraceContext",
    "TraceEventV1",
    "bind_trace_context",
    "child_context",
    "current_trace_context",
    "new_trace_context",
    "reset_trace_context",
    "TraceRecorder",
    "trace_headers",
    "canonicalize_payload",
    "TraceClient",
    "TraceDeliveryError",
    "TraceSpool",
    "SpoolError",
    "SpoolBackpressure",
    "SpoolCorruption",
    "drain_abandoned_spools",
    "operation",
    "AmbiguousOperation",
    "OperationCancelled",
]
