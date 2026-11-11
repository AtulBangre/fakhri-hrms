"""Leave management: applying paid / unpaid / half-day leave and balances."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from ..auth import SessionUser, require_hr, verify_form_csrf
from .. import notify
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import orgtime

router = APIRouter(prefix="/leave", tags=["leave"])


@router.get("/balances/export.csv")
def export_balances(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    csv_text = leave_service.export_balances_csv(storage, settings)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leave-balances.csv"'},
    )


@router.get("/balances/template.csv")
def balances_template(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    csv_text = leave_service.get_balances_template_csv(storage)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leave-quotas-template.csv"'},
    )


@router.post("/balances/import")
async def import_balances(
    request: Request,
    file: UploadFile = File(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    verify_form_csrf(user, csrf)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    res = leave_service.import_balances_csv(storage, text, actor=user.username)
    return list_page(request, user=user, storage=storage, settings=settings, import_summary=res)


@router.get("")
def list_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
    employee_id: int | None = None,
    leave_type: str = "all",
    status: str = "all",
    import_summary: dict | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    rows = leave_service.list_leave(
        storage, period=period, employee_id=employee_id, leave_type=leave_type, status=status
    )
    people = employees_service.list_employees(storage, limit=500)
    balances = [
        {"employee": person, **leave_service.balance(storage, settings, person["id"])}
        for person in people
    ]
    balances.sort(key=lambda item: item["balance"])

    pl_policy_mode = storage.get_setting("pl_policy_mode", "accrual")
    cycle_type = storage.get_setting("leave_carry_forward_cycle", "calendar_year")
    today = orgtime.today(settings)
    cycle_start, cycle_end = orgtime.leave_cycle_bounds(today, cycle_type)
    if cycle_type == "calendar_year":
        cycle_label = f"Calendar Year {cycle_start.year} ({cycle_start.strftime('%d %b %Y')} – {cycle_end.strftime('%d %b %Y')})"
    else:
        cycle_label = f"Financial Year {cycle_start.year}-{str(cycle_end.year)[-2:]} ({cycle_start.strftime('%d %b %Y')} – {cycle_end.strftime('%d %b %Y')})"

    return render(
        request,
        "leave.html",
        active="leave",
        user=user,
        leaves=rows,
        employees=people,
        balances=balances,
        period=period,
        period_label=orgtime.month_label(period),
        leave_types=orgtime.ALL_LEAVE_TYPES,
        leave_labels=orgtime.LEAVE_LABELS,
        filters={"employee_id": employee_id or "", "leave_type": leave_type, "status": status},
        holidays=storage.query("SELECT * FROM holidays ORDER BY day"),
        by_type=leave_service.summary_for_period(storage, period),
        pending=leave_service.pending_requests(storage),
        pending_count=leave_service.count_pending(storage),
        pl_policy_mode=pl_policy_mode,
        cycle_label=cycle_label,
        accrual_per_month=storage.get_float("pl_accrual_per_month", 1.5),
        import_summary=import_summary,
    )


@router.post("/apply")
def apply(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    leave_type: str = Form(...),
    from_date: str = Form(...),
    to_date: str = Form(""),
    half_day: str = Form(""),
    reason: str = Form(""),
    period: str = Form(""),
    allow_negative: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        result = leave_service.apply_leave(
            storage,
            settings,
            employee_id=employee_id,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date or from_date,
            half_day=bool(half_day),
            reason=reason or None,
            actor=user.username,
            allow_negative=bool(allow_negative),
        )
    except employees_service.ValidationError as exc:
        return redirect("/leave", err=str(exc), period=period or None)

    leave = result["leave"]
    if settings.notify_leave_email:
        _notify_decision(storage, settings, leave, "approved")
    message = (
        f"{leave['employee_name']}: {leave['days']:g} day(s) "
        f"{orgtime.LEAVE_LABELS.get(leave_type, leave_type).lower()} applied."
    )
    if result["skipped"]:
        skipped = ", ".join(f"{item['date']} ({item['why']})" for item in result["skipped"])
        message += f" Skipped: {skipped}."
    return redirect("/leave", ok=message, period=period or None)


@router.post("/{leave_id}/cancel")
def cancel(
    request: Request,
    leave_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        leave = leave_service.cancel_leave(storage, leave_id, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect("/leave", err=str(exc))
    return redirect(
        "/leave", ok=f"Leave cancelled ({leave['from_date']} → {leave['to_date']}).", period=period or None
    )


@router.post("/{leave_id}/approve")
def approve(
    request: Request,
    leave_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(""),
    allow_negative: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        result = leave_service.approve_leave(storage, leave_id=leave_id, actor=user.username,
                                             allow_negative=bool(allow_negative))
    except employees_service.ValidationError as exc:
        return redirect("/leave", err=str(exc), period=period or None)

    leave = result["leave"]
    _notify_decision(storage, settings, leave, "approved")
    message = f"Approved {leave['days']:g} day(s) for {orgtime.human_date(leave['from_date'])}."
    if result["skipped"]:
        skipped = ", ".join(f"{item['date']} ({item['why']})" for item in result["skipped"])
        message += f" Skipped: {skipped}."
    return redirect("/leave", ok=message, period=period or None)


@router.post("/{leave_id}/reject")
def reject(
    request: Request,
    leave_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    reason: str = Form(""),
    period: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        leave = leave_service.reject_leave(
            storage, leave_id=leave_id, actor=user.username, reason=reason or None
        )
    except employees_service.ValidationError as exc:
        return redirect("/leave", err=str(exc), period=period or None)
    _notify_decision(storage, settings, leave, "rejected", note=reason or None)
    return redirect("/leave", ok="Request rejected — the employee can see it was declined.", period=period or None)


def _notify_decision(storage, settings, leave: dict, decision: str, *, note: str | None = None) -> None:
    """Tell the employee (by push and email) that HR decided their leave."""
    notify.push_employee(storage, settings, leave["employee_id"], f"Your leave was {decision}",
                         f"{leave['from_date']} to {leave['to_date']}, {leave['days']} day(s)."
                         + (f" Note: {note}" if note else ""),
                         "/me/leave", category="leave_update")
    if not settings.notify_leave_email:
        return
    employee = employees_service.get_employee(storage, leave["employee_id"])
    if not employee or not employee.get("email"):
        return
    manager = (
        storage.query_one("SELECT id, name, email FROM employees WHERE id = ?", (employee.get("manager_id"),))
        if employee.get("manager_id") else None
    )
    cc = [manager["email"]] if manager and manager.get("email") else None
    notify.queue_email(
        storage, employee["email"], "leave_decided", settings=settings,
        category=f"leave_{decision}", name=employee["name"],
        from_date=orgtime.human_date(leave["from_date"]), to_date=orgtime.human_date(leave["to_date"]),
        days=leave["days"], decision=decision, note=note or "",
        leave_type=orgtime.LEAVE_LABELS.get(leave.get("leave_type", ""), leave.get("leave_type", "leave")),
        decided_by="HR",
        cc=cc,
    )


@router.get("/attachment/{stored_name}")
def download_leave_attachment(
    request: Request,
    stored_name: str,
    settings: Settings = Depends(get_settings_dep),
):
    from ..auth import current_user
    session = current_user(request)
    if not session:
        return redirect("/login")
    from fastapi.responses import FileResponse, Response
    from ..uploads import path_for, UploadError
    import mimetypes
    try:
        p = path_for(settings, "leave", stored_name)
    except (UploadError, ValueError, Exception):
        return Response("Attachment file not found", status_code=404)
    if not p.exists():
        return Response("Attachment file not found", status_code=404)
    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media_type)
