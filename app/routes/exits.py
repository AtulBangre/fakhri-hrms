"""Exit management: resignations, clearance and the full & final settlement."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import exits, orgtime
from ..services import records

router = APIRouter(tags=["exits"])


def _as_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(float((value or "").strip()))
    except ValueError:
        return default


# ====================================================================== HR
@router.get("/exits")
def exits_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    status: str = "open",
    employee_id: int | None = None,
):
    status = status if status in ("open", "settled", "withdrawn", "rejected", "all") else "open"
    today = orgtime.today(settings)
    return render(
        request, "exits.html", active="exits", user=user,
        rows=exits.list_exits(storage, status=status), status=status, counts=exits.counts(storage),
        employees=employees_service.list_employees(storage, status="active", limit=2000),
        exit_types=exits.EXIT_TYPES, notice_days=exits.notice_period(storage),
        today=today, preselect=employee_id,
    )


@router.post("/exits")
def record_exit(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    exit_type: str = Form("resignation"),
    resignation_date: str = Form(...),
    last_working_day: str = Form(...),
    notice_days: str = Form(""),
    notice_waived: str = Form(""),
    reason: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        row = exits.record_exit(
            storage, settings, employee_id=employee_id, exit_type=exit_type,
            resignation_date=resignation_date, last_working_day=last_working_day,
            notice_days=_as_int(notice_days, exits.notice_period(storage)),
            notice_waived=bool(notice_waived), reason=reason, actor=user.username,
        )
    except exits.ValidationError as exc:
        return redirect("/exits", err=str(exc))
    return redirect(f"/exits/{row['id']}", ok="Exit recorded — work through the clearance checklist next.")


@router.get("/exits/{exit_id}")
def exit_detail(
    request: Request,
    exit_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    try:
        row = exits.get(storage, exit_id)
    except exits.ValidationError as exc:
        return redirect("/exits", err=str(exc))
    return render(
        request, "exit_detail.html", active="exits", user=user, row=row,
        checklist=exits.checklist(storage, exit_id),
        fnf=exits.compute_fnf(storage, settings, row) if row["status"] in ("approved", "settled") else None,
        assets=records.assets_for_employee(storage, row["employee_id"]),
        exit_types=exits.EXIT_TYPES,
    )


@router.get("/exits/{exit_id}/statement")
def statement(
    request: Request,
    exit_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    try:
        row = exits.get(storage, exit_id)
    except exits.ValidationError as exc:
        return redirect("/exits", err=str(exc))
    if row["status"] not in ("approved", "settled"):
        return redirect(f"/exits/{exit_id}", err="Approve the exit first.")
    return render(
        request, "fnf_statement.html", active="exits", user=user, row=row,
        fnf=exits.compute_fnf(storage, settings, row), back_url=f"/exits/{exit_id}",
        org_name=storage.get_setting("org_name", settings.org_name),
    )


@router.post("/exits/{exit_id}/approve")
def approve(
    request: Request,
    exit_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    last_working_day: str = Form(...),
    notice_days: str = Form("30"),
    notice_waived: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        exits.approve(storage, exit_id, last_working_day=last_working_day,
                      notice_days=_as_int(notice_days, 30), notice_waived=bool(notice_waived),
                      actor=user.username)
    except exits.ValidationError as exc:
        return redirect(f"/exits/{exit_id}", err=str(exc))
    return redirect(f"/exits/{exit_id}", ok="Exit approved. Clearance checklist created.")


@router.post("/exits/{exit_id}/{action}")
def act(
    request: Request,
    exit_id: int,
    action: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    extra_earnings: str = Form("0"),
    extra_deductions: str = Form("0"),
    recover_assets: str = Form(""),
    note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    back = f"/exits/{exit_id}"
    try:
        if action == "reject":
            exits.reject(storage, exit_id, actor=user.username)
            message = "Resignation rejected."
        elif action == "withdraw":
            exits.withdraw(storage, exit_id, actor=user.username)
            message = "Exit withdrawn — the employee stays active."
        elif action == "settlement":
            exits.update_settlement(storage, exit_id, extra_earnings=_as_float(extra_earnings),
                                    extra_deductions=_as_float(extra_deductions),
                                    recover_assets=bool(recover_assets), note=note, actor=user.username)
            message = "Settlement updated."
        elif action == "settle":
            row = exits.settle(storage, settings, exit_id, actor=user.username)
            message = f"F&F settled for net {row['fnf_net']:,.2f}. Employee marked inactive."
        else:
            return redirect(back, err="Unknown action.")
    except exits.ValidationError as exc:
        return redirect(back, err=str(exc))
    return redirect(back, ok=message)


@router.post("/exit-checklist/{item_id}")
def toggle_item(
    request: Request,
    item_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    done: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        exit_id = exits.toggle_item(storage, item_id, done=bool(done), actor=user.username)
    except exits.ValidationError as exc:
        return redirect("/exits", err=str(exc))
    return redirect(f"/exits/{exit_id}")


# ===================================================================== ESS
@router.get("/me/exit")
def my_exit(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    employee_id = int(user.employee_id or 0)
    latest = exits.latest_for(storage, employee_id)
    today = orgtime.today(settings)
    notice = exits.notice_period(storage)
    return render(
        request, "me_exit.html", active="me-exit", user=user, latest=latest,
        notice_days=notice, today=today,
        suggested_lwd=(today.fromordinal(today.toordinal() + notice)).isoformat(),
        assets=records.assets_for_employee(storage, employee_id),
        fnf=exits.compute_fnf(storage, settings, latest) if latest and latest["status"] == "settled" else None,
    )


@router.post("/me/exit")
def submit_resignation(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    reason: str = Form(""),
    proposed_last_day: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        exits.resign(storage, settings, employee_id=int(user.employee_id or 0), reason=reason,
                     proposed_last_day=proposed_last_day or None, actor=user.username)
    except exits.ValidationError as exc:
        return redirect("/me/exit", err=str(exc))
    return redirect("/me/exit", ok="Resignation submitted. HR will confirm your last working day.")


@router.post("/me/exit/{exit_id}/withdraw")
def withdraw_resignation(
    request: Request,
    exit_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        exits.withdraw(storage, exit_id, actor=user.username, employee_id=int(user.employee_id or 0))
    except exits.ValidationError as exc:
        return redirect("/me/exit", err=str(exc))
    return redirect("/me/exit", ok="Resignation withdrawn.")
