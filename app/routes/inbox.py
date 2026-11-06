"""The bell: a user's notifications, and registering their devices for push.

Every query is limited to the signed-in user. There is no user id parameter.
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import notify, push
from ..auth import SessionUser, get_session
from ..deps import get_settings_dep, get_storage

router = APIRouter(prefix="/api", tags=["inbox"])


class SeenRequest(BaseModel):
    last_id: int


class UnsubscribeRequest(BaseModel):
    endpoint: str


def _member(request: Request) -> SessionUser:
    user = get_session(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if not (user.is_hr or user.is_employee):
        raise HTTPException(status_code=403, detail="Not available on this login")
    return user


def _member_with_csrf(request: Request, user: SessionUser = Depends(_member)) -> SessionUser:
    provided = request.headers.get("x-csrf-token") or ""
    if not hmac.compare_digest(provided, user.csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return user


@router.get("/inbox")
def inbox(
    user: SessionUser = Depends(_member),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    rows = notify.inbox_rows(storage, user.id)
    seen = notify.inbox_seen(storage, user.id)
    items = []
    for row in rows:
        try:
            meta = json.loads(row.get("meta") or "{}")
        except ValueError:
            meta = {}
        items.append({
            "id": row["id"], "title": row["subject"] or "Notification", "body": row["body"],
            "url": meta.get("url") or "/", "at": row["created_at"], "unread": row["id"] > seen,
        })
    return {
        "items": items,
        "unread": sum(1 for item in items if item["unread"]),
        "push_key": push.public_key(settings) if settings.notify_push else None,
    }


@router.post("/inbox/seen")
def mark_seen(payload: SeenRequest, user: SessionUser = Depends(_member_with_csrf), storage=Depends(get_storage)):
    notify.mark_inbox_seen(storage, user.id, payload.last_id)
    return {"ok": True}


@router.post("/push/subscribe")
async def subscribe(request: Request, user: SessionUser = Depends(_member_with_csrf), storage=Depends(get_storage)):
    try:
        subscription = await request.json()
    except ValueError:
        subscription = None
    if not push.valid_subscription(subscription):
        raise HTTPException(status_code=422, detail="This device address is not accepted.")
    push.subscribe(storage, user.id, subscription)
    return {"ok": True}


@router.post("/push/unsubscribe")
def unsubscribe(payload: UnsubscribeRequest, user: SessionUser = Depends(_member_with_csrf), storage=Depends(get_storage)):
    push.unsubscribe(storage, user.id, payload.endpoint)
    return {"ok": True}


@router.post("/push/test")
def test_push(
    user: SessionUser = Depends(_member_with_csrf),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    notify.push_user(storage, settings, user.id, "Notifications are on",
                     "This device will now receive HRMS alerts.", "/", category="push_test")
    notify.dispatch(storage, settings, limit=10)
    return {"ok": True}
