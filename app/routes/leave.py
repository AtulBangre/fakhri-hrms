"""Leave management: applying paid / unpaid / half-day leave and balances."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_hr, verify_form_csrf
from .. import notify
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import orgtime

router = APIRouter(prefix="/leave", tags=["leave"])


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
        )
    except employees_service.ValidationError as exc:
        return redirect("/leave", err=str(exc), period=period or None)

    leave = result["leave"]
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
):
    verify_form_csrf(user, csrf)
    try:
        result = leave_service.approve_leave(storage, leave_id=leave_id, actor=user.username)
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
    """Tell the employee (by email) that HR decided their leave."""
    if not settings.notify_leave_email:
        return
    employee = employees_service.get_employee(storage, leave["employee_id"])
    if not employee or not employee.get("email"):
        return
    notify.queue_email(
        storage, employee["email"], "leave_decided", settings=settings,
        category=f"leave_{decision}", name=(employee["name"] or "").split(" ")[0] or "there",
        from_date=leave["from_date"], to_date=leave["to_date"], days=leave["days"],
        decision=decision, note=note or "",
    )
