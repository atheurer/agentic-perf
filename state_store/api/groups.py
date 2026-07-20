"""Group management API endpoints.

All endpoints require authentication.  Mutation endpoints are
admin-only; listing is available to any authenticated user.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from state_store.auth import Principal, require_admin
from state_store.identity import (
    DuplicateGroup,
    GroupNotFound,
    UserNotFound,
    UserStore,
)

router = APIRouter(prefix="/groups", tags=["groups"])


class CreateGroupRequest(BaseModel):
    name: str
    description: str = ""


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
def create_group(body: CreateGroupRequest, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        group = store.create_group(body.name, description=body.description)
    except DuplicateGroup as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return group.model_dump(mode="json")


@router.get("")
def list_groups(request: Request):
    _get_principal(request)
    store = _get_store(request)
    return [g.model_dump(mode="json") for g in store.list_groups()]


@router.delete("/{name}")
def delete_group(name: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        store.delete_group(name)
    except GroupNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted"}


@router.put("/{name}/members/{username}")
def add_member(name: str, username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        store.add_member(name, username)
    except GroupNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "added"}


@router.delete("/{name}/members/{username}")
def remove_member(name: str, username: str, request: Request):
    principal = _get_principal(request)
    require_admin(principal)
    store = _get_store(request)
    try:
        store.remove_member(name, username)
    except GroupNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "removed"}
