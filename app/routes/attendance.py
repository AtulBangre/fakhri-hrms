"""Attendance pages: today's board, manual corrections and the monthly register."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import orgtime

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("")
def daily(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    day: str | None = None,
):
    target = orgtime.parse_date(day) if day else orgtime.today(settings)
    rows = attendance_service.daily_rows(storage, settings, target)
    counts = {"present": 0, "late": 0, "half_day": 0, "leave": 0, "absent": 0, "pending": 0}
    for row in rows:
        if row["effective_status"] == orgtime.STATUS_PRESENT:
            counts["present"] += 1
        elif row["effective_status"] == orgtime.STATUS_HALF_DAY:
            counts["half_day"] += 1
        elif row["effective_status"] == orgtime.STATUS_LEAVE:
            counts["leave"] += 1
        elif row["effective_status"] == orgtime.STATUS_ABSENT:
            counts["absent"] += 1
        elif row["effective_status"] == orgtime.STATUS_PENDING:
            counts["pending"] += 1
        if row["is_late"]:
            counts["late"] += 1

    return render(
        request,
        "attendance.html",
        active="attendance",
        user=user,
        day=target,
        day_iso=target.isoformat(),
        day_label=orgtime.human_day(target),
        kind=orgtime.day_kind(storage, target),
        holiday=orgtime.holiday_name(storage, target),
        rows=rows,
        counts=counts,
        statuses=orgtime.ALL_STATUSES,
        leave_types=orgtime.ALL_LEAVE_TYPES,
        leave_labels=orgtime.LEAVE_LABELS,
        previous_day=(target.fromordinal(target.toordinal() - 1)).isoformat(),
        next_day=(target.fromordinal(target.toordinal() + 1)).isoformat(),
    )


@router.post("/mark")
def mark(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    work_date: str = Form(...),
    status: str = Form(...),
    check_in: str = Form(""),
    check_out: str = Form(""),
    leave_type: str = Form(""),
    note: str = Form(""),
    return_day: str = Form(""),
):
    verify_form_csrf(user, csrf)
    target = return_day or work_date
    try:
        attendance_service.mark_manual(
            storage, employee_id=employee_id, work_date=work_date, status=status,
            check_in=check_in or None, check_out=check_out or None,
            leave_type=leave_type or None, note=note or None, actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect("/attendance", err=str(exc), day=target)
    employee = employees_service.get_employee(storage, employee_id)
    return redirect(
        "/attendance", day=target,
        ok=f"{employee['name'] if employee else 'Employee'} marked {orgtime.STATUS_LABELS.get(status, status)} on {work_date}.",
    )


@router.post("/clear")
def clear(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    work_date: str = Form(...),
    return_day: str = Form(""),
):
    verify_form_csrf(user, csrf)
    attendance_service.clear_day(storage, employee_id=employee_id, work_date=work_date, actor=user.username)
    return redirect("/attendance", day=return_day or work_date, ok="Entry removed.")


@router.get("/register")
def register(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
    department: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    try:
        data = attendance_service.monthly_register(storage, settings, period, department=department or None)
    except (ValueError, TypeError):
        period = orgtime.period_of(orgtime.today(settings))
        data = attendance_service.monthly_register(storage, settings, period)
    return render(
        request, "register.html", active="attendance", user=user, register=data,
        period=period, department=department or "",
    )


@router.get("/register.csv")
def register_csv(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
    department: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    data = attendance_service.monthly_register(storage, settings, period, department=department or None)
    return PlainTextResponse(
        attendance_service.register_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance-{period}.csv"'},
    )


@router.get("/summary.csv")
def summary_csv(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
    department: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    data = attendance_service.monthly_register(storage, settings, period, department=department or None)
    return PlainTextResponse(
        attendance_service.summary_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance-summary-{period}.csv"'},
    )


@router.post("/holidays")
def add_holiday(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    day: str = Form(...),
    name: str = Form(...),
    return_to: str = Form("/attendance"),
):
    verify_form_csrf(user, csrf)
    from ..db import utcnow_iso

    try:
        storage.execute(
            "INSERT INTO holidays (day, name, created_at) VALUES (?,?,?)",
            (orgtime.parse_date(day).isoformat(), name.strip(), utcnow_iso()),
        )
    except Exception:  # noqa: BLE001 - duplicate day
        return redirect(return_to, err=f"{day} is already a holiday.")
    storage.audit(actor=user.username, action="add_holiday", entity="holiday", detail=f"{day} {name}")
    return redirect(return_to, ok=f"Holiday added: {name} on {day}.")


@router.post("/holidays/delete")
def delete_holiday(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    holiday_id: int = Form(...),
    return_to: str = Form("/attendance"),
):
    verify_form_csrf(user, csrf)
    storage.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
    storage.audit(actor=user.username, action="delete_holiday", entity="holiday", entity_id=holiday_id)
    return redirect(return_to, ok="Holiday removed.")

