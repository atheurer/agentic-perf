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
    UserQuota,
    UserStore,
)

router = APIRouter(prefix="/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    is_admin: bool = False
    service_account: bool = False
    allowed_sources: list[str] = []
    max_requests_per_hour: int | None = None


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
    if body.max_requests_per_hour is not None and body.max_requests_per_hour < 0:
        raise HTTPException(
            status_code=422, detail="max_requests_per_hour must be non-negative"
        )
    try:
        user, raw_token = store.create_user(
            body.username,
            is_admin=body.is_admin,
            service_account=body.service_account,
            allowed_sources=body.allowed_sources,
            max_requests_per_hour=body.max_requests_per_hour,
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


class UpdateUserRequest(BaseModel):
    allowed_sources: list[str] | None = None
    max_requests_per_hour: int | None = None

    # Distinguish "field omitted" from "field set to null".
    # When the caller sends {"max_requests_per_hour": null},
    # Pydantic sets it to None.  When the field is absent,
    # we need to skip it.  model_fields_set tracks which
    # keys the caller actually provided.
    #
    # Note: allowed_sources is a full replacement, not an append.
    # To add a source, GET the user first, add to the list, then PATCH.


@router.patch("/{username}")
def update_user(username: str, body: UpdateUserRequest, request: Request):
    """Update mutable fields on an existing user (admin-only)."""
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)

    from state_store.identity import UNSET

    # These fields only take effect for service accounts.
    try:
        existing = store.get_user(username)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not existing.service_account:
        sa_fields = {"allowed_sources", "max_requests_per_hour"} & body.model_fields_set
        if sa_fields:
            raise HTTPException(
                status_code=422,
                detail=f"Fields {', '.join(sorted(sa_fields))} apply only to service accounts",
            )

    kwargs: dict = {}
    if "allowed_sources" in body.model_fields_set:
        if body.allowed_sources is None:
            raise HTTPException(
                status_code=422,
                detail="allowed_sources cannot be null; pass an empty list to clear or omit the field to leave unchanged",
            )
        kwargs["allowed_sources"] = body.allowed_sources
    if "max_requests_per_hour" in body.model_fields_set:
        if body.max_requests_per_hour is not None and body.max_requests_per_hour < 0:
            raise HTTPException(
                status_code=422,
                detail="max_requests_per_hour must be a non-negative integer or null",
            )
        kwargs["max_requests_per_hour"] = body.max_requests_per_hour
    else:
        kwargs["max_requests_per_hour"] = UNSET

    if not kwargs or (
        len(kwargs) == 1 and kwargs.get("max_requests_per_hour") is UNSET
    ):
        raise HTTPException(status_code=422, detail="No mutable fields provided")

    try:
        user = store.update_user(username, **kwargs)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return UserStore.to_safe_dict(user)


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


@router.put("/{username}/quota")
def set_user_quota(username: str, body: UserQuota, request: Request):
    """Set a user's LLM budget quota (admin-only)."""
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.set_user_quota(username, body)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)


@router.delete("/{username}/quota")
def clear_user_quota(username: str, request: Request):
    """Clear a user's LLM budget quota (admin-only)."""
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        user = store.set_user_quota(username, None)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UserStore.to_safe_dict(user)
