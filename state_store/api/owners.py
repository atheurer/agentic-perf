"""Ticket ownership management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import Principal
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["owners"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


def _get_store(request: Request):
    return request.app.state.store


@router.get("/{ticket_id}/owners")
def list_owners(ticket_id: str, request: Request):
    store = _get_store(request)
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"owners": ticket.owners}


@router.put("/{ticket_id}/owners/{username}")
def add_owner(ticket_id: str, username: str, request: Request):
    store = _get_store(request)
    principal = _get_principal(request)
    multi_user = _is_multi_user(request)

    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    if multi_user:
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is not None:
            from ..identity import UserNotFound

            try:
                user_store.get_user(username)
            except UserNotFound:
                raise HTTPException(
                    status_code=404,
                    detail=f"User '{username}' not found",
                )

        if not ticket.owners and principal.kind == "user":
            if username.lower() != principal.username:
                raise HTTPException(
                    status_code=403,
                    detail=("Unclaimed tickets: you can only add yourself as owner"),
                )
        elif ticket.owners:
            if principal.kind == "service" or principal.is_admin:
                pass
            elif principal.username not in ticket.owners:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Only current owners or admins can add owners; "
                        f"owners: {', '.join(ticket.owners)}"
                    ),
                )

    username_lower = username.lower()
    if username_lower in ticket.owners:
        return {"owners": ticket.owners}

    new_owners = ticket.owners + [username_lower]
    updated = store.set_owners(ticket_id, new_owners)
    return {"owners": updated.owners}


@router.delete("/{ticket_id}/owners/{username}")
def remove_owner(ticket_id: str, username: str, request: Request):
    store = _get_store(request)
    principal = _get_principal(request)
    multi_user = _is_multi_user(request)

    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    if multi_user:
        if principal.kind == "service" or principal.is_admin:
            pass
        elif principal.username not in ticket.owners:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Only current owners or admins can remove owners; "
                    f"owners: {', '.join(ticket.owners)}"
                ),
            )

    username_lower = username.lower()
    if username_lower not in ticket.owners:
        raise HTTPException(
            status_code=404,
            detail=f"'{username}' is not an owner of ticket {ticket_id}",
        )

    if len(ticket.owners) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove the last owner; transfer ownership first",
        )

    new_owners = [o for o in ticket.owners if o != username_lower]
    updated = store.set_owners(ticket_id, new_owners)
    return {"owners": updated.owners}
