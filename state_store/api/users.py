"""User management API endpoints.

All endpoints require authentication.  Admin-only endpoints are
gated by ``require_admin``; ``rotate-token`` allows self-service.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from state_store.auth import (
    Principal,
    require_admin,
    require_self_or_admin,
)
from state_store.identity import (
    DuplicateUser,
    UserNotFound,
    UserStore,
)

router = APIRouter(prefix="/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    is_admin: bool = False


def _get_store(request: Request) -> UserStore:
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(
            status_code=501,
            detail="Multi-user mode is not enabled",
        )
    return store


def _get_principal(request: Request) -> Principal:
    return request.state.principal


@router.post("")
def create_user(body: CreateUserRequest, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user, raw_token = store.create_user(
            body.username,
            is_admin=body.is_admin,
        )
    except DuplicateUser as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"user": UserStore.to_safe_dict(user), "token": raw_token}


@router.get("")
def list_users(request: Request):
    _get_principal(request)
    store = _get_store(request)
    return [UserStore.to_safe_dict(u) for u in store.list_users()]


@router.post("/{username}/disable")
def disable_user(username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.disable_user(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)


@router.post("/{username}/enable")
def enable_user(username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.enable_user(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)


@router.post("/{username}/rotate-token")
def rotate_token(username: str, request: Request):
    principal = _get_principal(request)
    require_self_or_admin(principal, username)
    store = _get_store(request)
    try:
        raw_token = store.rotate_token(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"token": raw_token}


@router.post("/{username}/admin")
def grant_admin(username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.set_admin(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)


@router.delete("/{username}/admin")
def revoke_admin(username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.remove_admin(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)
