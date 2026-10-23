"""Noticeboard, notification log and the outbound-message controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import announcements as announcement_service
from ..services import employees as employees_service
from ..services import orgtime
from .. import notify

router = APIRouter(tags=["noticeboard"])


@router.get("/announcements")
def noticeboard(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    return render(
        request,
        "announcements.html",
        active="announcements",
        user=user,
        items=announcement_service.list_all(storage, include_inactive=True),
        stats=announcement_service.stats(storage),
        levels=announcement_service.LEVELS,
        audiences=announcement_service.AUDIENCES,
        outbox=notify.outbox_stats(storage),
        recent=notify.recent(storage, limit=8),
        email_driver="smtp" if request.app.state.settings.smtp_host else "console (.eml files)",
        whatsapp_provider=request.app.state.settings.whatsapp_provider,
        employee_count=len(employees_service.list_employees(storage, status="active", limit=1000)),
    )


@router.post("/announcements")
def create_announcement(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    title: str = Form(...),
    body: str = Form(...),
    level: str = Form("info"),
    audience: str = Form("all"),
    pinned: str = Form(""),
    expires_on: str = Form(""),
    also_email: str = Form(""),
    also_whatsapp: str = Form(""),
    send_now: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        announcement = announcement_service.create(
            storage,
            title=title,
            body=body,
            level=level,
            audience=audience,
            pinned=bool(pinned),
            expires_on=expires_on or None,
            actor=user.username,
        )
    except ValueError as exc:
        return redirect("/announcements", err=str(exc))

    channels = []
    if also_email:
        channels.append("email")
    if also_whatsapp:
        channels.append("whatsapp")

    message = "Announcement published."
    if channels:
        queued = announcement_service.broadcast(storage, settings, announcement, channels=channels)
        message += f" Queued {queued.get('email', 0)} email(s) and {queued.get('whatsapp', 0)} WhatsApp message(s)."
        if send_now:
            result = notify.dispatch(storage, settings, limit=100)
            sent = sum(1 for row in result if row.get("ok"))
            message += f" Sent {sent} now."
    return redirect("/announcements", ok=message)


@router.post("/announcements/{announcement_id}/toggle")
def toggle_announcement(
    request: Request,
    announcement_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    row = announcement_service.toggle(storage, announcement_id, actor=user.username)
    return redirect("/announcements", ok="Shown on dashboards." if row["active"] else "Hidden from dashboards.")


@router.post("/announcements/{announcement_id}/delete")
def delete_announcement(
    request: Request,
    announcement_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    announcement_service.delete(storage, announcement_id, actor=user.username)
    return redirect("/announcements", ok="Announcement deleted.")


# ------------------------------------------------------------------- outbox
@router.get("/notifications")
def notification_log(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    status: str = "all",
):
    return render(
        request,
        "notifications.html",
        active="announcements",
        user=user,
        rows=notify.recent(storage, limit=200, status=status),
        stats=notify.outbox_stats(storage),
        status_filter=status,
        email_driver="smtp" if settings.smtp_host else "console (.eml files)",
        whatsapp_provider=settings.whatsapp_provider,
        sms_provider=settings.sms_provider,
    )


@router.post("/notifications/dispatch")
def dispatch_now(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    results = notify.dispatch(storage, settings, limit=200)
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent
    if not results:
        return redirect("/notifications", ok="Nothing waiting in the outbox.")
    note = f"Processed {len(results)}: {sent} sent"
    if failed:
        note += f", {failed} failed (check the log)"
    return redirect("/notifications", ok=note + ".")


@router.post("/notifications/retry")
def retry_failed(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    count = notify.retry_failed(storage)
    return redirect("/notifications", ok=f"{count} failed message(s) queued for another attempt.")


@router.post("/notifications/test")
def send_test(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    channel: str = Form("email"),
    recipient: str = Form(...),
    return_to: str = Form(""),
):
    verify_form_csrf(user, csrf)
    back = "/settings" if return_to == "/settings" else "/notifications"
    body = (
        f"<p>This is a test message from {settings.org_name} HRMS, sent at "
        f"{orgtime.local_now(settings).strftime('%H:%M on %d %b %Y')}.</p>"
        f"<p>If you are reading this, the {channel} channel works.</p>"
    )
    row_id = notify.queue(
        storage, channel=channel, recipient=recipient, subject="HRMS test message",
        body=body if channel == "email" else f"{settings.org_name} HRMS test message.",
        category="test", settings=settings,
    )
    if row_id is None:
        return redirect(back, err="Enter a recipient first.")
    result = notify.dispatch(storage, settings, limit=1)
    ok = bool(result and result[0].get("ok"))
    if ok:
        return redirect(back, ok=f"Test {channel} delivered to {recipient}.")
    detail = result[0].get("error") if result else "unknown error"
    return redirect(back, err=f"Test {channel} failed: {detail}")
