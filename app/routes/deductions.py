"""Deductions and fine management routes: month-wise overview, CRUD, waivers, and sync."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import deductions as deductions_service
from ..services import employees as employees_service
from ..services import orgtime

router = APIRouter(prefix="/deductions", tags=["deductions"])


@router.get("")
def list_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
    category: str | None = None,
    department: str | None = None,
    status: str = "active",
    q: str | None = None,
):
    current = orgtime.period_of(orgtime.today(settings))
    error = None
    parsed = orgtime.parse_period(period) if period else current
    if parsed is None:
        error = f"'{period}' is not a valid month — showing {orgtime.month_label(current)} instead."
        parsed = current

    items = deductions_service.list_deductions(
        storage,
        parsed,
        category=category or None,
        department=department or None,
        status=status or None,
        q=q or None,
    )
    stats = deductions_service.stats_for_period(storage, parsed)
    all_employees = employees_service.list_employees(storage, limit=500, status="active")

    return render(
        request,
        "deductions.html",
        active="deductions",
        user=user,
        period=parsed,
        period_label=orgtime.month_label(parsed),
        items=items,
        stats=stats,
        employees=all_employees,
        departments=employees_service.departments(storage),
        categories=deductions_service.DEDUCTION_CATEGORIES,
        category_labels=deductions_service.CATEGORY_LABELS,
        filters={
            "period": parsed,
            "category": category or "",
            "department": department or "",
            "status": status or "active",
            "q": q or "",
        },
        error=error,
        previous_period=orgtime.period_of(
            orgtime.month_bounds(parsed)[0].fromordinal(orgtime.month_bounds(parsed)[0].toordinal() - 1)
        ),
        next_period=orgtime.period_of(
            orgtime.month_bounds(parsed)[1].fromordinal(orgtime.month_bounds(parsed)[1].toordinal() + 1)
        ),
    )


@router.post("/add")
def add(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    period: str = Form(...),
    category: str = Form(...),
    amount: float = Form(...),
    reason: str = Form(...),
    work_date: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        deductions_service.add_deduction(
            storage,
            employee_id=employee_id,
            period=period,
            category=category,
            amount=amount,
            reason=reason,
            work_date=work_date or None,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect(f"/deductions?period={period}", err=str(exc))
    return redirect(f"/deductions?period={period}", ok=f"Deduction of ₹{amount:,.2f} added successfully.")


@router.post("/{deduction_id}/edit")
def edit(
    deduction_id: int,
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    category: str = Form(...),
    amount: float = Form(...),
    reason: str = Form(...),
    period: str = Form(...),
):
    verify_form_csrf(user, csrf)
    try:
        deductions_service.update_deduction(
            storage,
            deduction_id,
            category=category,
            amount=amount,
            reason=reason,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect(f"/deductions?period={period}", err=str(exc))
    return redirect(f"/deductions?period={period}", ok="Deduction updated.")


@router.post("/{deduction_id}/waive")
def waive(
    deduction_id: int,
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
    reason: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        deductions_service.waive_deduction(
            storage,
            deduction_id,
            reason=reason or None,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect(f"/deductions?period={period}", err=str(exc))
    return redirect(f"/deductions?period={period}", ok="Deduction waived.")


@router.post("/{deduction_id}/delete")
def delete(
    deduction_id: int,
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
):
    verify_form_csrf(user, csrf)
    try:
        deductions_service.delete_deduction(storage, deduction_id, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect(f"/deductions?period={period}", err=str(exc))
    return redirect(f"/deductions?period={period}", ok="Deduction deleted.")


@router.post("/sync-fines")
def sync_fines(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
):
    verify_form_csrf(user, csrf)
    count = deductions_service.sync_attendance_fines(storage, period, actor=user.username)
    return redirect(f"/deductions?period={period}", ok=f"Synced {count} attendance fine(s) into deductions ledger.")
