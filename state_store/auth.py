"""Bearer token authentication for the state store API.

Generates a shared secret on first startup and validates it on
every /api/v1/ request. The token file lives in the secrets
directory (~/.agentic-perf/secrets/api-token) and is readable
by the orchestrator and agent processes.

When ``auth.multi_user`` is enabled in the config, per-user tokens
are supported alongside the deployment token.  Each user's bearer
token is hashed with SHA-256 and looked up in the UserStore.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, Request

from paths import SECRETS_DIR

if TYPE_CHECKING:
    from .identity import UserStore

logger = logging.getLogger(__name__)

TOKEN_FILE = SECRETS_DIR / "api-token"
TOKEN_ENV_VAR = "AGENTIC_PERF_API_TOKEN"


@dataclass(frozen=True)
class Principal:
    """Identity of the authenticated caller."""

    kind: Literal["user", "service"]
    username: str
    is_admin: bool


def load_or_generate_token() -> str:
    """Read the API token from disk, or generate one if missing."""
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token

    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    logger.info("Generated new API token at %s", TOKEN_FILE)
    return token


def read_token_from_file() -> str:
    """Read the API token from the secrets file.

    For use by clients (orchestrator, CLI) that need to present
    the token but shouldn't generate one.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token

    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token

    return ""


def make_auth_dependency(
    token: str,
    *,
    multi_user: bool = False,
    user_store: UserStore | None = None,
):
    """Create a FastAPI dependency that validates bearer tokens.

    In legacy mode (``multi_user=False``), validates against the
    single deployment token and returns a service Principal.

    In multi-user mode, checks the deployment token first, then
    hashes the presented token and looks up the user in the store.
    """

    async def verify_token(request: Request) -> Principal:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid Authorization header",
            )
        presented = auth_header[7:]

        principal: Principal | None = None

        if presented == token:
            principal = Principal(
                kind="service",
                username="deployment",
                is_admin=True,
            )
        elif multi_user and user_store is not None:
            from .identity import hash_token

            token_h = hash_token(presented)
            user = user_store.lookup_by_token_hash(token_h)
            if user is not None:
                if user.disabled:
                    raise HTTPException(
                        status_code=401,
                        detail="User account is disabled",
                    )
                principal = Principal(
                    kind="user",
                    username=user.username,
                    is_admin=user.is_admin,
                )

        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API token",
            )

        request.state.principal = principal
        return principal

    return verify_token


# ------------------------------------------------------------------
# Authorization helpers
# ------------------------------------------------------------------


def require_admin(principal: Principal) -> None:
    """Raise 403 if the principal is not an admin."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin privileges required",
        )


def require_self_or_admin(principal: Principal, username: str) -> None:
    """Raise 403 unless the principal is admin or the named user."""
    if principal.is_admin:
        return
    if principal.kind == "user" and principal.username == username.lower():
        return
    raise HTTPException(
        status_code=403,
        detail="You can only perform this action on your own account",
    )
