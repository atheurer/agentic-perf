from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class FenceContext:
    session_id: str
    epoch: int
    claim_id: str


_CURRENT_FENCE: ContextVar[FenceContext | None] = ContextVar(
    "agentic_perf_fence_context", default=None
)


def bind_fence_context(context: FenceContext | None) -> None:
    _CURRENT_FENCE.set(context)


def current_fence_context() -> FenceContext | None:
    return _CURRENT_FENCE.get()
