"""Identity introspection endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from state_store.auth import Principal

router = APIRouter(tags=["identity"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


@router.get("/whoami")
def whoami(request: Request):
    principal = _get_principal(request)
    return {
        "kind": principal.kind,
        "username": principal.username,
        "is_admin": principal.is_admin,
    }
