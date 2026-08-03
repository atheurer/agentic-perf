"""Per-principal rate limiting with token-bucket algorithm.

Hand-rolled (~70 lines) to avoid external deps. The state store runs
a single uvicorn worker, so in-process dict state is sound — no lock
needed when the dependency is ``async def`` (runs on the event loop).

Clock injection enables deterministic tests without freezegun.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from collections.abc import Callable

    from .auth import Principal


class TokenBucket:
    """Refilling token bucket keyed by rate (RPM) and burst capacity."""

    __slots__ = ("capacity", "tokens", "refill_rate", "last_refill", "_clock")

    def __init__(
        self,
        rpm: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = burst
        self.tokens = float(burst)
        self.refill_rate = rpm / 60.0
        self.last_refill = clock()
        self._clock = clock

    def consume(self) -> float | None:
        """Try to consume one token.

        Returns ``None`` on success, or seconds until a token
        is available on failure.
        """
        now = self._clock()
        elapsed = now - self.last_refill
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_rate,
        )
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return None
        if self.refill_rate <= 0:
            return float("inf")
        return (1.0 - self.tokens) / self.refill_rate


class RateLimiter:
    """Per-principal rate limiter with bounded LRU table."""

    def __init__(
        self,
        *,
        rpm: int = 600,
        burst: int = 30,
        exempt_service: bool = True,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpm = rpm
        self.burst = burst
        self.exempt_service = exempt_service
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def check(self, principal: Principal) -> float | None:
        """Returns ``None`` if allowed, or Retry-After seconds."""
        if self.exempt_service and principal.kind == "service":
            return None
        key = principal.username
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._buckets.popitem(last=False)
            bucket = TokenBucket(
                self.rpm,
                self.burst,
                clock=self._clock,
            )
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
        return bucket.consume()


class AuthFailureLimiter:
    """Rate-limit authentication failures by client IP.

    Bounded table keyed on IP (never on the presented token —
    that is attacker-controlled memory). Includes a global
    failure bucket as a localhost backstop.
    """

    def __init__(
        self,
        *,
        failures_per_min: int = 30,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failures_per_min = failures_per_min
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._global_bucket = TokenBucket(
            failures_per_min * 4,
            failures_per_min * 4,
            clock=clock,
        )

    def record_failure(self, client_ip: str) -> None:
        """Record an auth failure for the given IP."""
        bucket = self._buckets.get(client_ip)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._buckets.popitem(last=False)
            bucket = TokenBucket(
                self.failures_per_min,
                self.failures_per_min,
                clock=self._clock,
            )
            self._buckets[client_ip] = bucket
        else:
            self._buckets.move_to_end(client_ip)
        bucket.consume()
        self._global_bucket.consume()

    def is_blocked(self, client_ip: str) -> float | None:
        """Check if the IP is blocked (non-consuming peek).

        Returns ``None`` if allowed, or Retry-After seconds.
        """
        now = self._global_bucket._clock()

        g = self._global_bucket
        g_elapsed = now - g.last_refill
        g_tokens = min(g.capacity, g.tokens + g_elapsed * g.refill_rate)
        if g_tokens < 1.0:
            if g.refill_rate <= 0:
                return float("inf")
            return (1.0 - g_tokens) / g.refill_rate

        bucket = self._buckets.get(client_ip)
        if bucket is None:
            return None
        b_elapsed = now - bucket.last_refill
        b_tokens = min(
            bucket.capacity,
            bucket.tokens + b_elapsed * bucket.refill_rate,
        )
        if b_tokens < 1.0:
            if bucket.refill_rate <= 0:
                return float("inf")
            return (1.0 - b_tokens) / bucket.refill_rate
        return None


_MAX_RETRY_AFTER = 3600


def _clamp_retry_after(seconds: float) -> str:
    """Convert wait time to a clamped Retry-After header value."""
    if not math.isfinite(seconds) or seconds <= 0:
        return "1"
    return str(min(int(math.ceil(seconds)), _MAX_RETRY_AFTER))


def make_rate_limit_dependency(
    limiter: RateLimiter | None,
) -> object:
    """Create a FastAPI dependency for per-request rate limiting.

    Must be ``async def`` with a synchronous body so it runs on
    the event loop (no lock needed). A sync ``def`` would run in
    the threadpool and require locking.
    """

    async def check_rate_limit(request: Request) -> None:
        if limiter is None:
            return
        principal: Principal | None = getattr(request.state, "principal", None)
        if principal is None:
            return
        retry_after = limiter.check(principal)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={
                    "Retry-After": _clamp_retry_after(retry_after),
                },
            )

    return check_rate_limit
