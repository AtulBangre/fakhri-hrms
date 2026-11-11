"""Employee self-service portal (``/me``).

The third role: a normal employee signs in with their own username (their
employee code, lower-cased) and gets **only their own** data —

    /me               today's status, this month, leave balance, latest payslip
    /me/attendance    their own month, day by day
    /me/leave         their own leave history + apply for leave (goes to HR as pending)
    /me/payslips      their own payslips, printable

Every query is filtered by ``session.employee_id`` from the signed cookie; there
is no employee-id parameter anywhere in these routes for someone to tamper with.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, require_employee, verify_form_csrf
from fastapi import Response
from ..deps import get_settings_dep, get_storage, redirect, render
from ..pdf import filename_for, render_payslip
from .. import notify
from ..services import account as account_service
from ..services import announcements as announcement_service
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import loans as loans_service
from ..services import orgtime, payroll, regularization

router = APIRouter(prefix="/me", tags=["self-service"])


def _is_final(storage, period: str) -> bool:
    """Employees only ever see finalised or paid payslips — drafts can still change."""
    run = payroll.get_run(storage, period)
    return bool(run and run["status"] in ("finalized", "paid"))


def _is_final_slip(storage, slip: dict | None) -> bool:
    """Check if a specific payslip is finalized/paid and visible to the employee."""
    if not slip or slip.get("status") == "on_hold":
        return False
    if slip.get("status") in ("finalized", "paid"):
        return True
    return _is_final(storage, slip["period"])


def _me(storage, user: SessionUser) -> dict:
    employee = employees_service.get_employee(storage, int(user.employee_id or 0))
    if employee is None:
        raise employees_service.ValidationError(
            "Your employee record is no longer available. Please ask HR."
        )
    return employee


def _is_manager(storage, employee_id: int) -> bool:
    count = storage.scalar("SELECT COUNT(*) FROM employees WHERE manager_id = ? AND status = 'active'", (employee_id,))
    return bool(count and int(count) > 0)


@router.get("")
def dashboard(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    employee = _me(storage, user)
    today = orgtime.today(settings)
    period = orgtime.period_of(today)
    row = attendance_service.get_row(storage, employee["id"], today)
    summary = attendance_service.employee_month_summary(storage, settings, employee["id"], period)
    balance = leave_service.balance(storage, settings, employee["id"])

    slips = storage.query(
        """
        SELECT p.* FROM payslips p JOIN payroll_runs r ON r.id = p.run_id 
        WHERE p.employee_id = ? 
          AND (p.status IN ('finalized', 'paid') OR (r.status IN ('finalized', 'paid') AND p.status != 'on_hold'))
        ORDER BY p.period DESC LIMIT 3
        """,
        (employee["id"],)
    )
    latest_slip = slips[0] if slips else None

    active_loan = loans_service.get_active_loan_for_employee(storage, employee["id"])
    if active_loan:
        active_loan["repaid_pct"] = (
            round((active_loan["total_repaid"] / active_loan["principal_amount"]) * 100, 1)
            if active_loan["principal_amount"] > 0
            else 100.0
        )
    checkout_after = None
    if row and row.get("check_in") and not row.get("check_out"):
        checkout_after = attendance_service.checkout_opens_at(
            settings, row["check_in"], orgtime.hhmmss(orgtime.local_now(settings))
        )

    return render(
        request,
        "me.html",
        active="me",
        user=user,
        employee=employee,
        today=today,
        today_row=row,
        checkout_after=checkout_after,
        face_pending=account_service.face_request(storage, employee["id"])["pending"],
        today_label=orgtime.human_day(today),
        kind=orgtime.day_kind(storage, today),
        summary=summary,
        balance=balance,
        period=period,
        period_label=orgtime.month_label(period),
        slips=slips,
        latest_slip=latest_slip,
        active_loan=active_loan,
        pending=leave_service.list_leave(storage, employee_id=employee["id"], status="pending", limit=5),
        recent_leave=leave_service.employee_leave_history(storage, employee["id"], limit=4),
        shift_start=orgtime.shift_for(storage, employee)["start_time"],
        shift_end=orgtime.shift_for(storage, employee)["end_time"],
        announcements=announcement_service.visible_for(storage, "employees"),
        company_notice_footer=storage.get_setting("company_notice_footer", ""),
        is_manager=_is_manager(storage, employee["id"]),
    )


@router.get("/attendance")
def my_attendance(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    employee = _me(storage, user)
    period = period or orgtime.period_of(orgtime.today(settings))

    start, end = orgtime.month_bounds(period)
    rows = {
        row["work_date"]: row
        for row in storage.query(
            "SELECT * FROM attendance WHERE employee_id = ? AND work_date BETWEEN ? AND ?",
            (employee["id"], start.isoformat(), end.isoformat()),
        )
    }
    today = orgtime.today(settings)
    window = attendance_service.employment_window(storage, employee, start, end)
    days = []
    for day in orgtime.days_in_period(period):
        iso = day.isoformat()
        record = rows.get(iso)
        cls = attendance_service.classify_day(record, orgtime.day_kind(storage, day), day, today, window)
        status = (record["status"] if record and cls != "not_employed"
                  else {"not_employed": orgtime.STATUS_NOT_EMPLOYED, "absent": orgtime.STATUS_ABSENT,
                        "week_off": orgtime.STATUS_WEEK_OFF, "holiday": orgtime.STATUS_HOLIDAY}
                  .get(cls, orgtime.STATUS_PENDING))
        days.append(
            {
                "day": day,
                "iso": iso,
                "weekday": day.strftime("%a"),
                "status": status,
                "status_label": orgtime.STATUS_LABELS.get(status, status),
                "check_in": orgtime.format_clock(record["check_in"]) if record else "—",
                "check_out": orgtime.format_clock(record["check_out"]) if record else "—",
                "worked": orgtime.format_minutes(record["worked_minutes"]) if record else "—",
                "late_minutes": record["late_minutes"] if record else 0,
                "late_fine": record.get("late_fine") if record else 0,
                "leave_type": orgtime.LEAVE_LABELS.get(record["leave_type"], "") if record else "",
                "note": orgtime.user_note(record) if record else None,
                "source": orgtime.source_label(record) if record and cls != "not_employed" else None,
                "is_today": day == today,
                "is_future": day > today,
            }
        )

    return render(
        request,
        "me_attendance.html",
        active="me-attendance",
        user=user,
        employee=employee,
        period=period,
        period_label=orgtime.month_label(period),
        days=days,
        summary=attendance_service.employee_month_summary(storage, settings, employee["id"], period),
        requests=regularization.list_requests(storage, status="all", employee_id=employee["id"], limit=30),
        pending_dates={
            row["work_date"] for row in regularization.list_requests(
                storage, status="pending", employee_id=employee["id"])
        },
        shift_start=orgtime.shift_for(storage, employee)["start_time"],
        shift_end=orgtime.shift_for(storage, employee)["end_time"],
        oldest_fixable=(orgtime.today(settings) - timedelta(days=regularization.MAX_AGE_DAYS)).isoformat(),
        today_iso=orgtime.today_str(settings),
        open_request=bool(request.query_params.get("request")),
    )


@router.post("/attendance/regularize")
def request_regularization(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    work_date: str = Form(...),
    requested_status: str = Form("present"),
    check_in: str = Form(""),
    check_out: str = Form(""),
    reason: str = Form(""),
):
    verify_form_csrf(user, csrf)
    period = work_date[:7] if len(work_date) >= 7 else None
    try:
        regularization.submit(
            storage, settings, employee_id=int(user.employee_id or 0), work_date=work_date,
            check_in=check_in, check_out=check_out, reason=reason, requested_status=requested_status,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect("/me/attendance", err=str(exc), period=period, request="1")
    return redirect("/me/attendance", period=period,
                    ok=f"Correction for {orgtime.human_date(work_date)} sent to HR.")


@router.post("/attendance/regularize/{request_id}/withdraw")
def withdraw_regularization(
    request: Request,
    request_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        regularization.withdraw(storage, request_id, employee_id=int(user.employee_id or 0), actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect("/me/attendance", err=str(exc))
    return redirect("/me/attendance", ok="Request withdrawn.")


@router.get("/payslips")
def my_payslips(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    employee = _me(storage, user)
    slips = storage.query(
        """
        SELECT p.*, r.status AS run_status FROM payslips p
        JOIN payroll_runs r ON r.id = p.run_id
        WHERE p.employee_id = ? 
          AND (p.status IN ('finalized', 'paid') OR (r.status IN ('finalized', 'paid') AND p.status != 'on_hold'))
        ORDER BY p.period DESC
        """,
        (employee["id"],),
    )
    return render(
        request, "me_payslips.html", active="me-payslips", user=user, employee=employee, slips=slips
    )


@router.get("/payslips/{slip_id}.pdf")
def my_payslip_pdf(
    request: Request,
    slip_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Download my own payslip as a PDF."""
    slip = payroll.get_slip(storage, slip_id)
    if (slip is None or slip["employee_id"] != int(user.employee_id or 0)
            or not _is_final_slip(storage, slip)):
        return redirect("/me/payslips", err="That payslip was not found in your account.")
    data = render_payslip(
        slip, settings=settings,
        org_name=storage.get_setting("org_name", settings.org_name),
        period_label=orgtime.month_label(slip["period"]),
    )
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename_for(slip)}"'},
    )


@router.get("/payslips/{slip_id}")
def my_payslip(
    request: Request,
    slip_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    slip = payroll.get_slip(storage, slip_id)
    # A payslip is a private document: someone else's id must look like it does not exist.
    if (slip is None or slip["employee_id"] != int(user.employee_id or 0)
            or not _is_final_slip(storage, slip)):
        return redirect("/me/payslips", err="That payslip was not found in your account.")
    run = payroll.get_run(storage, slip["period"])
    return render(
        request,
        "payslip.html",
        active="me-payslips",
        user=user,
        slip=slip,
        run=run,
        label=orgtime.month_label(slip["period"]),
        org_name=storage.get_setting("org_name", settings.org_name),
        currency=storage.get_setting("currency_symbol", "₹"),
        back_url="/me/payslips",
        pdf_url=f"/me/payslips/{slip_id}.pdf",
    )


@router.get("/leave")
def my_leave(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    employee = _me(storage, user)
    return render(
        request,
        "me_leave.html",
        active="me-leave",
        user=user,
        employee=employee,
        leaves=leave_service.employee_leave_history(storage, employee["id"]),
        balance=leave_service.balance(storage, settings, employee["id"]),
        leave_types=orgtime.ALL_LEAVE_TYPES,
        leave_labels=orgtime.LEAVE_LABELS,
        today=orgtime.today(settings),
    )


@router.post("/leave")
def request_leave(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    leave_type: str = Form(...),
    from_date: str = Form(...),
    to_date: str = Form(""),
    half_day: str = Form(""),
    reason: str = Form(""),
):
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        result = leave_service.apply_leave(
            storage,
            settings,
            employee_id=employee["id"],
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date or from_date,
            half_day=bool(half_day),
            reason=reason or None,
            actor=user.username,
            status="pending",           # HR decides; attendance is untouched for now
            requested_by=user.id,
        )
    except employees_service.ValidationError as exc:
        return redirect("/me/leave", err=str(exc))

    leave = result["leave"]

    if settings.notify_leave_email:
        notify.notify_hr(
            storage, settings, "leave_requested",
            category="leave_requested", name=employee["name"], code=employee["code"],
            leave_type=orgtime.LEAVE_LABELS.get(leave_type, leave_type),
            from_date=leave["from_date"], to_date=leave["to_date"],
            days=leave["days"], reason=reason or "",
        )
        if employee.get("email"):
            notify.queue_email(
                storage, employee["email"], "leave_applied_confirmation",
                settings=settings, category="leave_requested",
                name=(employee.get("name") or "").split(" ")[0] or "there",
                leave_type=orgtime.LEAVE_LABELS.get(leave_type, leave_type),
                from_date=leave["from_date"], to_date=leave["to_date"],
                days=leave["days"],
            )

    return redirect(
        "/me/leave",
        ok=f"Request sent — {leave['days']:g} day(s) {orgtime.LEAVE_LABELS.get(leave_type, leave_type).lower()}. HR will review it.",
    )


@router.post("/leave/{leave_id}/cancel")
def cancel_my_request(
    request: Request,
    leave_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None or leave["employee_id"] != int(user.employee_id or 0):
        return redirect("/me/leave", err="That request was not found in your account.")
    if leave["status"] == "approved":
        return redirect("/me/leave", err="That leave is already approved — please speak to HR to cancel it.")

    leave_service.cancel_leave(storage, leave_id, actor=user.username)
    return redirect("/me/leave", ok="Request withdrawn.")


@router.get("/team")
def team_page(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    employee = _me(storage, user)
    team = storage.query(
        "SELECT id, code, name, designation, department, email, phone FROM employees WHERE manager_id = ? AND status = 'active' ORDER BY name",
        (employee["id"],),
    )
    if not team:
        return redirect("/me", err="You have no direct reportees assigned to you.")

    pending_leaves = leave_service.list_manager_team_leave(storage, employee["id"], status="pending_manager")
    return render(
        request,
        "me_team.html",
        active="me-team",
        user=user,
        employee=employee,
        team=team,
        pending_leaves=pending_leaves,
        is_manager=True,
    )


@router.post("/team/leave/{leave_id}/approve")
def approve_team_leave(
    leave_id: int,
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    note: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        leave_service.manager_decide_leave(
            storage, leave_id, employee["id"], decision="approved", note=note, actor=user.username
        )
    except Exception as exc:
        return redirect("/me/team", err=str(exc))
    return redirect("/me/team", ok="Leave recommended and forwarded to HR for final sign-off.")


@router.post("/team/leave/{leave_id}/reject")
def reject_team_leave(
    leave_id: int,
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    note: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        leave_service.manager_decide_leave(
            storage, leave_id, employee["id"], decision="rejected", note=note, actor=user.username
        )
    except Exception as exc:
        return redirect("/me/team", err=str(exc))
    return redirect("/me/team", ok="Leave request rejected.")


@router.get("/loans")
def my_loans(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    employee = _me(storage, user)
    all_loans = loans_service.list_loans(storage, employee_id=employee["id"])
    active_loan = loans_service.get_active_loan_for_employee(storage, employee["id"])
    if active_loan:
        active_loan["repaid_pct"] = (
            round((active_loan["total_repaid"] / active_loan["principal_amount"]) * 100, 1)
            if active_loan["principal_amount"] > 0
            else 100.0
        )

    for l in all_loans:
        repayments = storage.query(
            "SELECT * FROM loan_repayments WHERE loan_id = ? ORDER BY repaid_at DESC",
            (l["id"],),
        )
        l["repayments"] = [dict(r) for r in repayments]

    return render(
        request,
        "me_loans.html",
        active="me-loans",
        user=user,
        employee=employee,
        loans=all_loans,
        active_loan=active_loan,
        loan_types=loans_service.LOAN_TYPES,
    )


@router.post("/loans/apply")
def apply_loan_route(
    request: Request,
    principal_amount: float = Form(...),
    monthly_emi: float = Form(0.0),
    loan_type: str = Form("salary_advance"),
    reason: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        loan_id = loans_service.apply_loan(
            storage,
            employee_id=employee["id"],
            principal_amount=principal_amount,
            monthly_emi=monthly_emi,
            loan_type=loan_type,
            reason=reason,
        )
    except employees_service.ValidationError as exc:
        return redirect("/me/loans", err=str(exc))
    except Exception as exc:
        return redirect("/me/loans", err=f"Failed to submit loan request: {exc}")

    return redirect("/me/loans", ok=f"Loan request #{loan_id} for ₹{principal_amount:,.2f} submitted. Awaiting HR approval.")



