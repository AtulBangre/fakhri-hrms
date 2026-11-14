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

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

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

    emp_shift = orgtime.shift_for(storage, employee)
    shift_start = emp_shift["start_time"]
    shift_end = emp_shift["end_time"]

    # Calculate shift total minutes
    st_clock = orgtime.parse_clock(shift_start)
    et_clock = orgtime.parse_clock(shift_end)
    if st_clock and et_clock:
        shift_minutes = (et_clock.hour * 60 + et_clock.minute) - (st_clock.hour * 60 + st_clock.minute)
        if shift_minutes <= 0:
            shift_minutes += 24 * 60
    else:
        shift_minutes = 480

    worked_pct = 0.0
    current_worked_label = "0h 0m"
    if row and row.get("check_in") and not row.get("check_out") and row.get("status") != "leave":
        now_dt = orgtime.local_now(settings)
        ci_clock = orgtime.parse_clock(row["check_in"])
        if ci_clock:
            ci_dt = datetime.combine(now_dt.date(), ci_clock).replace(tzinfo=settings.tz)
            elapsed_seconds = max(0, int((now_dt - ci_dt).total_seconds()))
            elapsed_mins = elapsed_seconds // 60
            worked_pct = min(100.0, max(0.0, round((elapsed_mins / shift_minutes) * 100, 1)))
            current_worked_label = orgtime.format_minutes(elapsed_mins)
    elif row and row.get("worked_minutes"):
        elapsed_mins = row["worked_minutes"]
        worked_pct = min(100.0, max(0.0, round((elapsed_mins / shift_minutes) * 100, 1)))
        current_worked_label = orgtime.format_minutes(elapsed_mins)

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
        shift_start=shift_start,
        shift_end=shift_end,
        shift_minutes=shift_minutes,
        worked_pct=worked_pct,
        current_worked_label=current_worked_label,
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
        storage=storage,
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
async def request_leave(
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
    attachments: list[UploadFile] = File(default=[]),
):
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)

    # Process and save any uploaded supporting documents safely
    stored_attachments: list[dict[str, Any]] = []
    if attachments:
        from ..uploads import save_upload
        for upload in attachments:
            if upload and upload.filename and upload.filename.strip():
                try:
                    data = await upload.read()
                    if data:
                        stored = save_upload(settings, "leave", data, original_name=upload.filename)
                        stored_attachments.append({
                            "stored_name": stored.stored_name,
                            "filename": stored.filename,
                            "size": stored.size,
                            "mime": stored.mime,
                        })
                except Exception as exc:
                    pass

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
            status="pending",           # HR / Manager decides
            requested_by=user.id,
            attachments=stored_attachments,
        )
    except employees_service.ValidationError as exc:
        return redirect("/me/leave", err=str(exc))

    leave = result["leave"]

    manager = (
        storage.query_one("SELECT id, name, code, email FROM employees WHERE id = ?", (employee.get("manager_id"),))
        if employee.get("manager_id") else None
    )
    cc_list = [manager["email"]] if manager and manager.get("email") else None

    if settings.notify_leave_email:
        notify.notify_hr(
            storage, settings, "leave_requested",
            category="leave_requested",
            name=employee["name"],
            code=employee["code"],
            department=employee.get("department") or "",
            manager_name=manager["name"] if manager else None,
            leave_type=orgtime.LEAVE_LABELS.get(leave_type, leave_type),
            from_date=orgtime.human_date(leave["from_date"]),
            to_date=orgtime.human_date(leave["to_date"]),
            days=leave["days"],
            reason=reason or "",
            attachments=stored_attachments,
            cc=cc_list,
            reply_to=employee.get("email"),
            file_attachments=stored_attachments,
        )
        if employee.get("email"):
            notify.queue_email(
                storage, employee["email"], "leave_applied_confirmation",
                settings=settings, category="leave_requested",
                name=employee["name"],
                leave_type=orgtime.LEAVE_LABELS.get(leave_type, leave_type),
                from_date=orgtime.human_date(leave["from_date"]),
                to_date=orgtime.human_date(leave["to_date"]),
                days=leave["days"],
            )

    if getattr(settings, "notify_whatsapp_leave", True):
        if employee.get("phone"):
            notify.queue_whatsapp(
                storage, employee["phone"], "leave_applied_confirmation",
                settings=settings,
                days=leave["days"],
                from_date=orgtime.human_date(leave["from_date"]),
                to_date=orgtime.human_date(leave["to_date"]),
            )
        hr_phone = getattr(settings, "hr_whatsapp_phone", "")
        if hr_phone:
            notify.queue_whatsapp(
                storage, hr_phone, "leave_requested",
                settings=settings,
                name=employee["name"],
                days=leave["days"],
                from_date=orgtime.human_date(leave["from_date"]),
                to_date=orgtime.human_date(leave["to_date"]),
            )

    return redirect(
        "/me/leave",
        ok=f"Request sent — {leave['days']:g} day(s) {orgtime.LEAVE_LABELS.get(leave_type, leave_type).lower()}. HR & Manager notified.",
    )


@router.post("/leave/{leave_id}/cancel")
def cancel_my_request(
    request: Request,
    leave_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None or leave["employee_id"] != int(user.employee_id or 0):
        return redirect("/me/leave", err="That request was not found in your account.")
    if leave["status"] == "approved":
        return redirect("/me/leave", err="That leave is already approved — please speak to HR to cancel it.")

    employee = _me(storage, user)
    leave_service.cancel_leave(storage, leave_id, actor=user.username)

    if settings.notify_leave_email:
        manager = (
            storage.query_one("SELECT id, name, code, email FROM employees WHERE id = ?", (employee.get("manager_id"),))
            if employee.get("manager_id") else None
        )
        cc_list = [manager["email"]] if manager and manager.get("email") else None
        notify.notify_hr(
            storage, settings, "leave_cancelled",
            category="leave_cancelled",
            name=employee["name"],
            code=employee["code"],
            leave_type=orgtime.LEAVE_LABELS.get(leave["leave_type"], leave["leave_type"]),
            from_date=orgtime.human_date(leave["from_date"]),
            to_date=orgtime.human_date(leave["to_date"]),
            days=leave["days"],
            cc=cc_list,
        )

    return redirect("/me/leave", ok="Leave request withdrawn.")


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
    settings=Depends(get_settings_dep),
    note: str = Form("Recommended by reporting manager"),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        updated = leave_service.manager_decide_leave(
            storage, leave_id, employee["id"], decision="approved", note=note, actor=user.username
        )
    except Exception as exc:
        return redirect("/me/team", err=str(exc))

    # Notify HR that reporting manager has approved and forwarded
    applicant = employees_service.get_employee(storage, updated["employee_id"])
    if settings.notify_leave_email:
        manager_email = employee.get("email")
        notify.notify_hr(
            storage, settings, "leave_requested",
            category="leave_requested",
            name=applicant["name"] if applicant else "Employee",
            code=applicant["code"] if applicant else "",
            department=applicant.get("department") or "",
            manager_name=employee["name"],
            leave_type=orgtime.LEAVE_LABELS.get(updated["leave_type"], updated["leave_type"]),
            from_date=orgtime.human_date(updated["from_date"]),
            to_date=orgtime.human_date(updated["to_date"]),
            days=updated["days"],
            reason=f"Recommended by Manager ({employee['name']})" + (f": {note}" if note else "") + f"\n\nOriginal Reason: {updated.get('reason') or '—'}",
            cc=[manager_email] if manager_email else None,
        )

    return redirect("/me/team", ok="Leave recommended and forwarded to HR for final sign-off.")


@router.post("/team/leave/{leave_id}/reject")
def reject_team_leave(
    leave_id: int,
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    note: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf
    verify_form_csrf(user, csrf)
    employee = _me(storage, user)
    try:
        updated = leave_service.manager_decide_leave(
            storage, leave_id, employee["id"], decision="rejected", note=note, actor=user.username
        )
    except Exception as exc:
        return redirect("/me/team", err=str(exc))

    # Notify employee with HR and Manager in CC
    applicant = employees_service.get_employee(storage, updated["employee_id"])
    if applicant and applicant.get("email") and settings.notify_leave_email:
        hr_cc = notify.hr_emails(storage, settings)
        manager_email = employee.get("email")
        cc_list = list(set(hr_cc + ([manager_email] if manager_email else [])))
        notify.queue_email(
            storage, applicant["email"], "leave_decided", settings=settings,
            category="leave_update", name=applicant["name"],
            from_date=orgtime.human_date(updated["from_date"]),
            to_date=orgtime.human_date(updated["to_date"]),
            days=updated["days"], decision="rejected",
            note=note or "Declined by reporting manager",
            leave_type=orgtime.LEAVE_LABELS.get(updated["leave_type"], updated["leave_type"]),
            decided_by=f"Reporting Manager ({employee['name']})",
            cc=cc_list,
        )

    return redirect("/me/team", ok="Leave request rejected and employee notified.")


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
    settings=Depends(get_settings_dep),
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

    # Send multi-channel notifications
    loan_label = loan_type.replace("_", " ").title()
    amt_str = f"{principal_amount:,.2f}"

    # In-app push
    notify.push_employee(
        storage, settings, employee["id"],
        f"{loan_label} submitted",
        f"Your request for ₹{amt_str} has been submitted to HR.",
        "/me/loans", category="loan",
    )
    notify.push_hr(
        storage, settings,
        f"New {loan_label} request",
        f"{employee['name']} requested ₹{amt_str}.",
        "/loans", category="loan",
    )

    # Email
    if settings.notify_email_enabled:
        notify.notify_hr(
            storage, settings, "hr_alert", category="loan",
            subject=f"Loan Request: {employee['name']} · ₹{amt_str}",
            message=f"{employee['name']} ({employee['code']}) requested {loan_label} of ₹{amt_str}. Reason: {reason or '—'}",
            link="/loans",
        )
        if employee.get("email"):
            notify.queue_email(
                storage, employee["email"], "hr_alert", settings=settings, category="loan",
                name=employee["name"],
                subject=f"{loan_label} Request Submitted (₹{amt_str})",
                message=f"Hi {employee['name']}, your request for {loan_label} of ₹{amt_str} has been submitted to HR.",
            )

    # WhatsApp
    if getattr(settings, "notify_whatsapp_leave", True):
        if employee.get("phone"):
            notify.queue_whatsapp(
                storage, employee["phone"], "loan_applied", settings=settings,
                loan_type=loan_label, amount=amt_str,
            )
        hr_phone = storage.get_setting("hr_whatsapp_phone", getattr(settings, "hr_whatsapp_phone", ""))
        if hr_phone:
            notify.queue_whatsapp(
                storage, hr_phone, "loan_requested_hr", settings=settings,
                name=employee["name"], loan_type=loan_label, amount=amt_str, reason=reason,
            )

    return redirect("/me/loans", ok=f"Loan request #{loan_id} for ₹{principal_amount:,.2f} submitted. Awaiting HR approval.")



