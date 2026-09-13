from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.config import _load_config_file
from paths import (
    AGENTIC_PERF_HOME,
    TRACE_DB_PATH,
    get_instance_name,
    persistence_root_fingerprint,
)
from providers.events import EventBus
from providers.tracing import TraceContext, bind_trace_context, reset_trace_context

from .api.router import api_router, chat_router, health_router, webhook_router
from .audit import AuditLog, set_actor
from .auth import load_or_generate_token, make_auth_dependency
from .process_lock import PersistenceRootLock
from .ratelimit import (
    AuthFailureLimiter,
    RateLimiter,
    make_rate_limit_dependency,
)
from .store import TicketStore
from .trace_store import TraceStore

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)

_runtime_locks: dict[Path, tuple[PersistenceRootLock, int]] = {}
_runtime_locks_guard = threading.Lock()


def _validate_positive_int(
    value: Any,
    name: str,
    *,
    allow_zero: bool = True,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    if not allow_zero and value == 0:
        raise ValueError(f"{name} must be > 0, got 0")
    return value


async def _set_audit_actor(request: Request) -> None:
    principal = getattr(request.state, "principal", None)
    ip = request.client.host if request.client else "unknown"
    if principal:
        set_actor(principal.kind, principal.username, ip)


def mount_routers(
    app: FastAPI,
    auth: Any,
    rate_limit: Any = None,
) -> None:
    """Mount API, health, and webhook routers with dependencies.

    Factored out so test helpers (which clear and re-mount routers)
    can use the same wiring as production. ``rate_limit`` may be
    ``None`` to skip the rate-limit dependency.
    """
    deps: list = [Depends(auth), Depends(_set_audit_actor)]
    if rate_limit is not None:
        deps.append(Depends(rate_limit))
    app.include_router(api_router, dependencies=deps)
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(chat_router)


def _initialize_runtime(app: FastAPI, port: int) -> None:
    """Construct writable runtime components only after the root lock is held."""
    app.state.trace_store = TraceStore(TRACE_DB_PATH)
    app.state.trace_instance_id = get_instance_name()
    app.state.trace_health = {
        "ingested": 0,
        "ingestion_failures": 0,
        "schema_rejections": 0,
        "quarantined_frames": 0,
    }

    token = load_or_generate_token()
    app.state.api_token = token

    cfg = _load_config_file()
    auth_cfg = cfg.get("auth", {})
    multi_user = auth_cfg.get("multi_user", False)
    anonymous_read = auth_cfg.get("anonymous_read", False)
    token_ttl_days = _validate_positive_int(
        auth_cfg.get("token_ttl_days", 0),
        "auth.token_ttl_days",
    )
    app.state.multi_user = multi_user
    app.state.anonymous_read = anonymous_read
    app.state.token_ttl_days = token_ttl_days
    if anonymous_read:
        logger.info("Anonymous read-only access enabled")

    user_store = None
    if multi_user:
        from .identity import UserStore

        user_store = UserStore()
    app.state.user_store = user_store

    if token_ttl_days > 0 and multi_user and user_store is not None:
        now = datetime.now(timezone.utc)

        def _token_age_days(u) -> int:
            issued = u.token_issued_at or u.created_at
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            return (now - issued).days

        expired = [
            u.username
            for u in user_store.list_users()
            if _token_age_days(u) >= token_ttl_days
        ]
        if expired:
            logger.warning(
                "Token TTL is %d days — %d user(s) have already-expired tokens: %s",
                token_ttl_days,
                len(expired),
                ", ".join(sorted(expired)),
            )

    # ── Rate limiting ────────────────────────────────────────
    rl_cfg = cfg.get("rate_limit", {})
    rl_enabled = rl_cfg.get("enabled", True)
    rl_per_user_rpm = _validate_positive_int(
        rl_cfg.get("per_user_rpm", 600),
        "rate_limit.per_user_rpm",
        allow_zero=not rl_enabled,
    )
    rl_burst = _validate_positive_int(
        rl_cfg.get("burst", 30),
        "rate_limit.burst",
        allow_zero=not rl_enabled,
    )
    rl_exempt_service = rl_cfg.get("exempt_service", True)
    rl_auth_failures_per_min = _validate_positive_int(
        rl_cfg.get("auth_failures_per_min", 30),
        "rate_limit.auth_failures_per_min",
        allow_zero=not rl_enabled,
    )

    auth_failure_limiter: AuthFailureLimiter | None = None
    rate_limiter: RateLimiter | None = None
    rate_limit_dep = None

    if rl_enabled:
        auth_failure_limiter = AuthFailureLimiter(
            failures_per_min=rl_auth_failures_per_min,
        )
        if multi_user:
            rate_limiter = RateLimiter(
                rpm=rl_per_user_rpm,
                burst=rl_burst,
                exempt_service=rl_exempt_service,
            )
            rate_limit_dep = make_rate_limit_dependency(rate_limiter)
            logger.info(
                "Rate limiting enabled: %d rpm, burst %d, service exempt=%s",
                rl_per_user_rpm,
                rl_burst,
                rl_exempt_service,
            )
        logger.info(
            "Auth failure limiting enabled: %d failures/min",
            rl_auth_failures_per_min,
        )

    app.state.rate_limiter = rate_limiter
    app.state.auth_failure_limiter = auth_failure_limiter

    auth = make_auth_dependency(
        token,
        multi_user=multi_user,
        user_store=user_store,
        token_ttl_days=token_ttl_days,
        auth_failure_limiter=auth_failure_limiter,
        anonymous_read=anonymous_read,
    )
    app.state.auth_dependency = auth

    from providers.redaction import Redactor

    audit_redactor = Redactor()
    # Compatibility adapters use independent SQLite connections to the same
    # database, so API worker threads never interleave transactions on one
    # connection while the TraceStore remains the sole persistence authority.
    audit_log = AuditLog(
        redactor=audit_redactor,
        trace_store=app.state.trace_store,
        process_identity=getattr(app.state, "store_diagnostics", {}),
    )
    app.state.audit_log = audit_log
    app.state.event_bus = EventBus(redactor=audit_redactor)
    app.state.store = TicketStore(
        audit_log=audit_log,
        event_bus=app.state.event_bus,
        trace_store=app.state.trace_store,
    )
    mount_routers(app, auth, rate_limit_dep)

    chat_cfg = cfg.get("chat", {})
    if chat_cfg.get("enabled", False):
        try:
            from agents.chat.agent import ChatAgent
            from providers.llm.factory import create_llm_provider

            llm_cfg = cfg.get("llm", {})
            chat_model_cfg = cfg.get("agent_models", {}).get("chat", {})
            provider = llm_cfg.get("provider", "")
            model = chat_model_cfg.get("model", llm_cfg.get("model", ""))
            if provider and model:
                chat_llm = create_llm_provider(
                    provider=provider,
                    model=model,
                    api_key=llm_cfg.get("api_key", ""),
                    backend=llm_cfg.get("backend", ""),
                    project_id=llm_cfg.get("project_id", ""),
                    region=llm_cfg.get("region", ""),
                )
                if max_tokens := chat_model_cfg.get("max_tokens"):
                    chat_llm.max_tokens = int(max_tokens)
                if timeout := chat_model_cfg.get("timeout"):
                    chat_llm.timeout = float(timeout)
                app.state.chat_agent = ChatAgent(
                    llm=chat_llm,
                    store_url=f"http://localhost:{port}",
                    max_tool_rounds=int(chat_model_cfg.get("max_tool_rounds", 10)),
                )
                logger.info("Chat agent enabled (model=%s)", model)
            else:
                logger.warning("Chat enabled but no LLM provider/model configured")
        except Exception:
            logger.exception("Failed to initialize chat agent")


def _acquire_runtime_lock(port: int) -> PersistenceRootLock:
    """Acquire one process-wide reference to the authoritative root lock."""
    root = AGENTIC_PERF_HOME.resolve()
    with _runtime_locks_guard:
        shared = _runtime_locks.get(root)
        if shared is not None:
            lock, references = shared
            _runtime_locks[root] = (lock, references + 1)
            return lock
        lock = PersistenceRootLock(root, port)
        lock.acquire()
        _runtime_locks[root] = (lock, 1)
        return lock


def _release_runtime_lock(lock: PersistenceRootLock) -> None:
    root = lock.root.resolve()
    with _runtime_locks_guard:
        shared = _runtime_locks.get(root)
        if shared is None:
            return
        _, references = shared
        if references > 1:
            _runtime_locks[root] = (lock, references - 1)
            return
        del _runtime_locks[root]
        lock.release()


def _start_runtime(app: FastAPI, port: int) -> None:
    """Acquire the root lock before constructing any writable backend."""
    if getattr(app.state, "runtime_initialized", False):
        return
    lock = _acquire_runtime_lock(port)
    app.state.process_lock = lock
    app.state.store_diagnostics = {
        "store_id": lock.store_id,
        "process_session_id": lock.session_id,
        "persistence_root_fingerprint": persistence_root_fingerprint(),
        "instance_name": get_instance_name(),
        "process": lock.metadata,
    }
    try:
        _initialize_runtime(app, port)
        app.state.runtime_initialized = True
        logger.info(
            "State store started: store_id=%s session_id=%s root=%s",
            lock.store_id,
            lock.session_id,
            app.state.store_diagnostics["persistence_root_fingerprint"],
        )
    except Exception:
        _release_runtime_lock(lock)
        app.state.process_lock = None
        raise


def _close_runtime(app: FastAPI) -> None:
    if not getattr(app.state, "runtime_initialized", False):
        return
    for name in ("event_bus", "audit_log"):
        adapter = getattr(app.state, name, None)
        if adapter is not None:
            adapter.close()
    app.state.trace_store.close()
    app.state.runtime_initialized = False
    lock = getattr(app.state, "process_lock", None)
    if lock is not None:
        _release_runtime_lock(lock)
        app.state.process_lock = None


def create_app(*, initialize_immediately: bool = False) -> FastAPI:
    port = int(os.environ.get("STORE_PORT", "8090"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not getattr(app.state, "runtime_initialized", False):
            _start_runtime(app, port)
        try:
            yield
        finally:
            _close_runtime(app)

    app = FastAPI(title="Agentic Perf State Store", version="0.1.0", lifespan=lifespan)
    app.state.trace_health = {
        "ingested": 0,
        "ingestion_failures": 0,
        "schema_rejections": 0,
        "quarantined_frames": 0,
    }

    @app.middleware("http")
    async def restore_trace_context(request: Request, call_next):
        """Restore correlation only from an authenticated internal caller.

        Middleware runs before route dependencies, so authenticate here before
        binding anything.  A user token (or a forged marker) may authorize the
        route but can never choose its causal parent.
        """
        if request.headers.get("X-Agentic-Perf-Causal-Context") != "v1":
            return await call_next(request)
        auth = getattr(request.app.state, "auth_dependency", None)
        if auth is None:
            return await call_next(request)
        try:
            principal = await auth(request)
        except HTTPException:
            # The route dependency returns the normal authentication response;
            # importantly, no untrusted context is bound on that path.
            return await call_next(request)
        if principal.kind != "service":
            return await call_next(request)
        traceparent = request.headers.get("traceparent", "").split("-")
        try:
            context = TraceContext(
                ticket_id=request.headers.get("X-Agentic-Perf-Ticket-Id") or None,
                agent_id=request.headers.get("X-Agentic-Perf-Agent-Id") or None,
                invocation_id=request.headers.get("X-Agentic-Perf-Invocation-Id")
                or None,
                trace_id=traceparent[1],
                action_id=request.headers.get("X-Agentic-Perf-Action-Id")
                or traceparent[2],
                parent_action_id=request.headers.get("X-Agentic-Perf-Parent-Action-Id")
                or None,
            )
        except (IndexError, ValueError):
            return await call_next(request)
        token = bind_trace_context(context)
        try:
            return await call_next(request)
        finally:
            reset_trace_context(token)

    @app.exception_handler(RequestValidationError)
    async def count_trace_schema_rejections(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path.startswith("/api/v1/traces/events"):
            app.state.trace_health["schema_rejections"] += 1
        return await request_validation_exception_handler(request, exc)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        ],
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    if initialize_immediately:
        _start_runtime(app, port)

    def close_compatibility_runtime() -> None:
        """Keep direct in-process app users able to release test resources."""
        _close_runtime(app)

    app.router.on_shutdown.append(close_compatibility_runtime)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def serve_dashboard():
            index_path = STATIC_DIR / "index.html"
            html = index_path.read_text()
            inject_token = "" if app.state.multi_user else app.state.api_token
            token_script = f'<script>window.API_TOKEN="{inject_token}";</script>'
            html = html.replace("</head>", f"{token_script}</head>", 1)
            return HTMLResponse(
                content=html,
                headers={"Cache-Control": "no-cache"},
            )

    return app


app = create_app(initialize_immediately=False)

if __name__ == "__main__":
    uvicorn.run(
        "state_store.main:app",
        host="0.0.0.0",
        port=8090,
    )
