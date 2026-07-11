"""Bearer token authentication for the state store API.

Generates a shared secret on first startup and validates it on
every /api/v1/ request. The token file lives in the secrets
directory (~/.agentic-perf/secrets/api-token) and is readable
by the orchestrator and agent processes.

This is the deployment-level auth layer. Multi-user / per-user
auth is a future concern — this module is designed to be replaced
or extended when that lands.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import HTTPException, Request

from paths import SECRETS_DIR

logger = logging.getLogger(__name__)

TOKEN_FILE = SECRETS_DIR / "api-token"
TOKEN_ENV_VAR = "AGENTIC_PERF_API_TOKEN"


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


def make_auth_dependency(token: str):
    """Create a FastAPI dependency that validates the bearer token."""

    async def verify_token(request: Request) -> None:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid Authorization header",
            )
        if auth_header[7:] != token:
            raise HTTPException(
                status_code=401,
                detail="Invalid API token",
            )

    return verify_token
