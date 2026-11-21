"""Leave management: applying paid / unpaid / half-day leave and balances."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from ..auth import SessionUser, require_hr, verify_form_csrf
from .. import notify
from ..deps import get_settings_dep, get_storage, leave_status, redirect, render
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import orgtime

router = APIRouter(prefix="/leave", tags=["leave"])


@router.get("/balances/export.xlsx")
def export_balances_xlsx(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    xlsx_bytes = leave_service.export_balances_xlsx(storage, settings)
    today_str = orgtime.today(settings).isoformat()
    filename = f"leave_balances_{today_str}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/balances/export.csv")
def export_balances(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    csv_text = leave_service.export_balances_csv(storage, settings)
    today_str = orgtime.today(settings).isoformat()
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="leave-balances-{today_str}.csv"'},
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
    filename = file.filename or "import.xlsx"
    rows = leave_service.parse_spreadsheet_rows(raw, filename)
    res = leave_service.import_balances_data(storage, rows, actor=user.username)

    # If called via AJAX fetch, return pure JSON response
    accept = request.headers.get("accept", "")
    if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, "summary": res})

    return list_page(request, user=user, storage=storage, settings=settings, import_summary=res)


@router.get("/employee/{employee_id}/history")
def employee_leave_history(
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Return JSON history and balance breakdown of an employee for the HR Leave modal."""
    from fastapi.responses import JSONResponse
    emp = employees_service.get_employee(storage, employee_id)
    if not emp:
        return JSONResponse({"error": "Employee not found"}, status_code=404)
    bal = leave_service.balance(storage, settings, employee_id)
    history = leave_service.list_leave(storage, employee_id=employee_id, status="all", limit=200)
    return JSONResponse({
        "employee": {
            "id": emp["id"],
            "name": emp["name"],
            "code": emp["code"],
            "department": emp.get("department") or "—",
            "designation": emp.get("designation") or "—",
        },
        "balance": {
            "pl_available": bal.get("pl_available", 0),
            "used_pl": bal.get("used_pl", 0),
            "annual_pl": bal.get("annual_pl", bal.get("accrued", 0)),
            "sl_available": bal.get("sl_available", 0),
            "used_sl": bal.get("used_sl", 0),
            "annual_sl": bal.get("annual_sl", 0),
            "cl_available": bal.get("cl_available", 0),
            "used_cl": bal.get("used_cl", 0),
            "annual_cl": bal.get("annual_cl", 0),
            "used_unpaid": bal.get("used_unpaid", 0),
            "total_available": bal.get("total_available", 0),
        },
        "leaves": [
            {
                "id": l["id"],
                "from_date": l["from_date"],
                "to_date": l["to_date"],
                "from_label": l["from_label"],
                "to_label": l["to_label"],
                "days": l["days"],
                "type_label": l["type_label"],
                "is_paid": l["is_paid"],
                "status": l["status"],
                "status_label": leave_status(l["status"]),
                "reason": l.get("reason") or "—",
                "decision_note": l.get("decision_note") or "",
                "applied_label": l.get("applied_label") or "—",
                "attachment_files": l.get("attachment_files") or [],
            }
            for l in history
        ],
    })


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
    leave_type: str = Form(""),
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
    """Tell the employee (by push, email, and WhatsApp) that HR decided their leave."""
    notify.push_employee(storage, settings, leave["employee_id"], f"Your leave was {decision}",
                         f"{leave['from_date']} to {leave['to_date']}, {leave['days']} day(s)."
                         + (f" Note: {note}" if note else ""),
                         "/me/leave", category="leave_update")
    employee = employees_service.get_employee(storage, leave["employee_id"])
    if not employee:
        return

    if settings.notify_leave_email and employee.get("email"):
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

    if getattr(settings, "notify_whatsapp_leave", True) and employee.get("phone"):
        notify.queue_whatsapp(
            storage, employee["phone"], "leave_decided", settings=settings,
            from_date=orgtime.human_date(leave["from_date"]), to_date=orgtime.human_date(leave["to_date"]),
            decision=decision,
        )


@router.get("/attachment/{stored_name}")
def download_leave_attachment(
    request: Request,
    stored_name: str,
    settings: Settings = Depends(get_settings_dep),
    storage=Depends(get_storage),
):
    """A supporting document (often medical): HR, the applicant and their reporting manager only."""
    from fastapi.responses import FileResponse, Response
    from ..auth import current_user
    from ..uploads import UploadError, path_for

    session = current_user(request)
    not_found = Response("Attachment file not found", status_code=404)
    if not stored_name or "%" in stored_name or '"' in stored_name:
        return not_found
    # LIKE on the JSON text, then an exact check on the parsed list
    row = storage.query_one(
        "SELECT l.employee_id, l.attachments, e.manager_id FROM leave_requests l "
        "JOIN employees e ON e.id = l.employee_id WHERE l.attachments LIKE ?",
        (f'%"{stored_name}"%',),
    )
    if row is None:
        return not_found
    try:
        files = json.loads(row["attachments"] or "[]")
    except ValueError:
        files = []
    match = next((f for f in files if isinstance(f, dict) and f.get("stored_name") == stored_name), None)
    if match is None:
        return not_found
    me = int(session.employee_id or 0)
    allowed = session.is_hr or (session.is_employee and me in (row["employee_id"], row["manager_id"] or -1))
    if not allowed:
        return not_found  # someone else's document looks like no document
    try:
        path = path_for(settings, "leave", stored_name)
    except (UploadError, ValueError):
        return not_found
    return FileResponse(path, media_type=match.get("mime") or "application/octet-stream",
                        filename=match.get("filename") or stored_name,
                        headers={"Cache-Control": "private, no-store"})
