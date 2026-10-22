"""Company noticeboard.

Announcements are the cheapest way to communicate something to everybody ("Diwali
holiday list is out", "payslips released"). They render on the HR dashboard and on
every employee's portal, ordered pinned-first then newest-first, and can be mailed
and/or sent on WhatsApp in the same action by queueing one message per active
employee.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from .. import notify
from . import employees as employees_service

LEVELS = ("info", "success", "warning", "urgent")
AUDIENCES = ("all", "employees", "hr")


def create(
    storage: Storage,
    *,
    title: str,
    body: str,
    level: str = "info",
    audience: str = "all",
    pinned: bool = False,
    expires_on: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise ValueError("An announcement needs a title.")
    if not body:
        raise ValueError("An announcement needs a message.")
    if level not in LEVELS:
        level = "info"
    if audience not in AUDIENCES:
        audience = "all"

    cursor = storage.execute(
        """
        INSERT INTO announcements (created_at, created_by, title, body, level, audience, pinned,
                                   active, expires_on)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (utcnow_iso(), actor, title[:160], body, level, audience, 1 if pinned else 0, 1, expires_on),
    )
    storage.audit(actor=actor, action="announcement", entity="announcement",
                  entity_id=cursor.lastrowid, detail=title[:80])
    return get(storage, int(cursor.lastrowid))


def get(storage: Storage, announcement_id: int) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM announcements WHERE id = ?", (announcement_id,))
    if row is None:
        raise ValueError("Announcement not found.")
    return row


def list_all(storage: Storage, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE active = 1"
    return storage.query(
        f"SELECT * FROM announcements {where} ORDER BY pinned DESC, id DESC"
    )


def visible_for(storage: Storage, audience: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Active, unexpired announcements for ``hr`` or ``employees``."""
    today = utcnow_iso()[:10]
    rows = storage.query(
        """
        SELECT * FROM announcements
        WHERE active = 1
          AND (expires_on IS NULL OR expires_on = '' OR expires_on >= ?)
          AND audience IN ('all', ?)
        ORDER BY pinned DESC, id DESC
        LIMIT ?
        """,
        (today, audience, limit),
    )
    return rows


def toggle(storage: Storage, announcement_id: int, *, actor: str | None = None) -> dict[str, Any]:
    row = get(storage, announcement_id)
    storage.execute(
        "UPDATE announcements SET active = ? WHERE id = ?",
        (0 if row["active"] else 1, announcement_id),
    )
    storage.audit(actor=actor, action="announcement_toggle", entity="announcement",
                  entity_id=announcement_id)
    return get(storage, announcement_id)


def delete(storage: Storage, announcement_id: int, *, actor: str | None = None) -> None:
    storage.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    storage.audit(actor=actor, action="announcement_delete", entity="announcement",
                  entity_id=announcement_id)


def broadcast(
    storage: Storage, settings: Settings, announcement: dict[str, Any], *, channels: list[str]
) -> dict[str, int]:
    """Queue the announcement to every active employee who has an address.

    Returns ``{"email": n, "whatsapp": n, "sms": n}`` — the number queued per
    channel, not the number delivered (that happens in the background).
    """
    people = employees_service.list_employees(storage, status="active", limit=1000)
    queued = {"email": 0, "whatsapp": 0, "sms": 0}
    body_html = "<p>" + announcement["body"].replace("\n", "<br>").replace("\r", "") + "</p>"
    date_label = announcement["created_at"][:10]

    for person in people:
        if "email" in channels and person.get("email"):
            row_id = notify.queue_email(
                storage,
                person["email"],
                "announcement",
                settings=settings,
                category="announcement",
                name=person["name"],
                title=announcement["title"],
                body_html=body_html,
                date=date_label,
            )
            queued["email"] += 1 if row_id else 0
        if "whatsapp" in channels and person.get("phone"):
            row_id = notify.queue(
                storage,
                channel="whatsapp",
                recipient=person["phone"],
                subject=None,
                body=f"*{settings.org_name}* — {announcement['title']}\n{announcement['body'][:300]}",
                category="announcement",
                settings=settings,
            )
            queued["whatsapp"] += 1 if row_id else 0
    return queued


def stats(storage: Storage) -> dict[str, int]:
    return {
        "total": int(storage.scalar("SELECT COUNT(*) AS n FROM announcements")),
        "active": int(storage.scalar("SELECT COUNT(*) AS n FROM announcements WHERE active = 1")),
        "pinned": int(storage.scalar("SELECT COUNT(*) AS n FROM announcements WHERE pinned = 1 AND active = 1")),
    }
