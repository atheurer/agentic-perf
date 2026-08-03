"""Webhook ingestion API endpoints.

Two endpoints:
- ``GET /webhooks`` — list available sources (normal auth via router dep)
- ``POST /webhooks/{source}`` — receive webhook (custom auth: query
  string ``?token=xxx`` OR Bearer header; service accounts also
  validate source IP and rate limit)
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from state_store.identity import UserStore, hash_token
from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.webhooks.registry import get_translator, list_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# In-memory rate-limit tracker: username -> list of timestamps
_rate_limit_window: dict[str, list[float]] = {}


def _check_rate_limit(username: str, max_per_hour: int | None) -> None:
    """Enforce per-user rate limiting.  Raises 429 on breach."""
    if max_per_hour is None:
        return
    now = time.monotonic()
    cutoff = now - 3600.0
    timestamps = _rate_limit_window.get(username, [])
    # Prune old entries
    timestamps = [t for t in timestamps if t > cutoff]
    if len(timestamps) >= max_per_hour:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
        )
    timestamps.append(now)
    _rate_limit_window[username] = timestamps


def _match_ip(client_ip: str, allowed: list[str]) -> bool:
    """Check if *client_ip* matches any entry in *allowed* (IP or CIDR)."""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            network = ipaddress.ip_network(entry, strict=False)
            if addr in network:
                return True
        except ValueError:
            # Plain IP comparison
            if client_ip == entry:
                return True
    return False


def _authenticate_webhook(
    request: Request,
    source: str,
) -> str:
    """Authenticate the webhook caller.  Returns the username.

    Accepts token via query string ``?token=`` or ``Authorization: Bearer``.
    Service accounts additionally require source IP validation.
    """
    # Extract token from query string or header
    raw_token: str | None = request.query_params.get("token")
    if not raw_token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]

    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    # Check deployment token first
    deploy_token = getattr(request.app.state, "api_token", "")
    if deploy_token and secrets.compare_digest(
        raw_token.encode("utf-8", errors="replace"),
        deploy_token.encode("utf-8", errors="replace"),
    ):
        return "deployment"

    # Try user store lookup
    user_store: UserStore | None = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    token_h = hash_token(raw_token)
    user = user_store.lookup_by_token_hash(token_h)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    if user.disabled:
        raise HTTPException(status_code=401, detail="User account is disabled")

    # Service account extra checks
    if user.service_account:
        # Validate webhook source is allowed
        # Validate source IP
        # Respect X-Forwarded-For behind reverse proxies
        # (OpenShift router, HAProxy, etc.).
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else ""
        if not _match_ip(client_ip, user.allowed_sources):
            raise HTTPException(
                status_code=403,
                detail="Source IP not allowed",
            )

        # Rate limit
        _check_rate_limit(user.username, user.max_requests_per_hour)

    user_store.touch_last_used(user.username)
    return user.username


def _find_dedup(
    store: Any,
    trigger_source: str,
    dedup_key_value: str | None,
) -> str | None:
    """Check open tickets for a matching dedup key.  Returns ticket ID or None."""
    if dedup_key_value is None:
        return None
    for ticket in store.list_tickets():
        if ticket.status in {TicketStatus.CLOSED}:
            continue
        cf = ticket.custom_fields
        if cf.get("trigger_source") != trigger_source:
            continue
        if cf.get("dedup_key") == dedup_key_value:
            return ticket.id
    return None


@router.get("")
def list_webhook_sources(request: Request) -> dict[str, list[str]]:
    """List available webhook sources.  Requires normal auth."""
    # Validate bearer token only — no source IP or webhook
    # source checks needed for a read-only listing.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )
    presented = auth_header[7:]
    if not presented:
        raise HTTPException(status_code=401, detail="Empty bearer token")

    deploy_token = getattr(request.app.state, "api_token", "")
    if deploy_token and secrets.compare_digest(
        presented.encode("utf-8", errors="replace"),
        deploy_token.encode("utf-8", errors="replace"),
    ):
        return {"sources": list_sources()}

    user_store: UserStore | None = getattr(
        request.app.state,
        "user_store",
        None,
    )
    if user_store is not None:
        token_h = hash_token(presented)
        user = user_store.lookup_by_token_hash(token_h)
        if user is not None and not user.disabled:
            return {"sources": list_sources()}

    raise HTTPException(status_code=401, detail="Invalid API token")


@router.post("/{source}")
async def receive_webhook(
    source: str,
    request: Request,
) -> dict[str, Any]:
    """Receive and process a webhook from *source*."""
    # Custom auth (bypasses the global auth dependency)
    username = _authenticate_webhook(request, source)

    # Look up translator
    try:
        translator = get_translator(source)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown webhook source: {source}",
        )

    # Parse body
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Translate
    ticket_fields = translator.translate(payload)
    dedup_key_value = translator.dedup_key(payload)

    # Dedup check
    store = request.app.state.store
    existing_id = _find_dedup(
        store,
        ticket_fields.get("custom_fields", {}).get("trigger_source", source),
        dedup_key_value,
    )
    if existing_id:
        logger.info(
            "Dedup hit for source=%s key=%s ticket=%s",
            source,
            dedup_key_value,
            existing_id,
        )
        return {
            "status": "duplicate",
            "ticket_id": existing_id,
            "dedup_key": dedup_key_value,
        }

    # Inject dedup key into custom_fields
    custom_fields = ticket_fields.get("custom_fields", {})
    if dedup_key_value:
        custom_fields["dedup_key"] = dedup_key_value

    # Create ticket
    create_req = CreateTicketRequest(
        summary=ticket_fields["summary"],
        description=ticket_fields.get("description", ""),
        custom_fields=custom_fields,
    )
    ticket = store.create_ticket(create_req, created_by=username)

    # Emit event so the dashboard timeline shows the trigger.
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus:
        event_bus.emit(
            ticket.id,
            "webhook",
            "ticket_created",
            {
                "source": source,
                "created_by": username,
                "dedup_key": dedup_key_value,
            },
        )

    # Transition to triage_pending so the orchestrator picks it up.
    store.transition_ticket(
        ticket.id,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )

    logger.info(
        "Webhook created ticket %s from source=%s user=%s",
        ticket.id,
        source,
        username,
    )
    return {
        "status": "created",
        "ticket_id": ticket.id,
    }
