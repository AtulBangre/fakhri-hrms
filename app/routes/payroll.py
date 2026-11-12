"""Payroll pages: monthly runs, payslips, adjustments and exports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi import Response
from fastapi.responses import PlainTextResponse

from ..auth import SessionUser, require_hr, verify_form_csrf
from .. import notify
from ..deps import get_settings_dep, get_storage, redirect, render
from ..pdf import filename_for, render_payslip
from ..services import employees as employees_service
from ..services import attendance as attendance_service
from ..services import orgtime, payroll

router = APIRouter(prefix="/payroll", tags=["payroll"])


def _as_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@router.get("")
def index(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    runs = payroll.list_runs(storage)
    run = payroll.get_run(storage, period)
    slips = payroll.slips_for_run(storage, run["id"]) if run else []
    unmarked = (
        attendance_service.unmarked_working_days(storage, settings, period)
        if not run or run["status"] == "draft" else []
    )
    return render(
        request,
        "payroll.html",
        active="payroll",
        user=user,
        runs=runs,
        period=period,
        period_label=orgtime.month_label(period),
        run=run,
        slips=slips,
        totals=payroll.slip_totals(slips) if slips else {},
        basis_days=payroll.basis_days_for(storage, period),
        unmarked=unmarked,
        state=payroll.period_state(storage, settings, period),
        unmarked_days=sum(len(row["days"]) for row in unmarked),
        employees_count=employees_service.count_employees(storage),
        previous_period=orgtime.period_of(
            (orgtime.month_bounds(period)[0].fromordinal(orgtime.month_bounds(period)[0].toordinal() - 1))
        ),
        next_period=orgtime.period_of(
            (orgtime.month_bounds(period)[1].fromordinal(orgtime.month_bounds(period)[1].toordinal() + 1))
        ),
    )


@router.post("/generate")
def generate(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
    include_inactive: str = Form(""),
    reset_adjustments: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        run = payroll.generate_run(
            storage, settings, period, actor=user.username, include_inactive=bool(include_inactive),
            reset_adjustments=bool(reset_adjustments),
        )
    except employees_service.ValidationError as exc:
        return redirect("/payroll", err=str(exc), period=period)
    message = f"{orgtime.month_label(period)} payroll calculated — {run['status']}."
    if run.get("kept_adjustments"):
        message += f" {run['kept_adjustments']} payslip(s) kept their bonus/deduction adjustments."
    return redirect("/payroll", period=period, ok=message + " Review the payslips below.")


@router.post("/status")
def set_status(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
    status: str = Form(...),
):
    verify_form_csrf(user, csrf)
    try:
        if status == "draft":
            payroll.reopen_run(storage, period, actor=user.username)
        else:
            payroll.set_run_status(storage, period, status, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect("/payroll", err=str(exc), period=period)
    if status == "finalized":
        # No amounts here: this text can show on a lock screen.
        months_label = orgtime.month_label(period)
        for slip in payroll.list_slips(storage, period):
            notify.push_employee(storage, settings, slip["employee_id"], "Your payslip is ready",
                                 f"Your payslip for {months_label} is available.", "/me/payslips",
                                 category="payslip")
            # Automatically queue WhatsApp PDF payslip if enabled
            if getattr(settings, "notify_whatsapp_payslip", True):
                emp = employees_service.get_employee(storage, slip["employee_id"])
                phone = (emp or {}).get("phone")
                if phone and float(slip.get("monthly_gross") or 0) > 0:
                    wa_id = notify.queue_whatsapp(
                        storage, phone, "payslip", settings=settings,
                        name=(emp.get("name") or "").split(" ")[0] or "there",
                        period=months_label,
                    )
                    if wa_id:
                        storage.execute(
                            "UPDATE notifications SET meta=? WHERE id=?",
                            (f'{{"template":"payslip","slip_id":{slip["id"]}}}', wa_id),
                        )
    return redirect("/payroll", period=period, ok=f"{orgtime.month_label(period)} marked {status}.")


@router.post("/delete")
def delete_run(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
):
    verify_form_csrf(user, csrf)
    try:
        payroll.delete_run(storage, period, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect("/payroll", err=str(exc), period=period)
    return redirect("/payroll", period=period, ok=f"{orgtime.month_label(period)} payroll deleted.")


@router.post("/slip/{slip_id}")
def update_slip(
    request: Request,
    slip_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    bonus: str = Form("0"),
    advance: str = Form("0"),
    other_deduction: str = Form("0"),
    tds: str = Form(""),
    note: str = Form(""),
    bonus_note: str = Form(""),
    advance_note: str = Form(""),
    other_deduction_note: str = Form(""),
    tds_note: str = Form(""),
    period: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        payroll.update_slip(
            storage, slip_id, bonus=_as_float(bonus), advance=_as_float(advance),
            other_deduction=_as_float(other_deduction),
            tds=_as_float(tds) if tds.strip() else None,
            note=note or None,
            bonus_note=bonus_note.strip() or None,
            advance_note=advance_note.strip() or None,
            other_deduction_note=other_deduction_note.strip() or None,
            tds_note=tds_note.strip() or None,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect("/payroll", err=str(exc), period=period or None)
    return redirect("/payroll", period=period or None, ok="Payslip adjusted.")


@router.get("/slip/{slip_id}.pdf")
def payslip_pdf(
    request: Request,
    slip_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    slip = payroll.get_slip(storage, slip_id)
    if slip is None:
        return redirect("/payroll", err="Payslip not found.")
    data = render_payslip(
        slip, settings=settings,
        org_name=storage.get_setting("org_name", settings.org_name),
        period_label=orgtime.month_label(slip["period"]),
    )
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename_for(slip)}"'},
    )


@router.get("/slip/{slip_id}")
def view_slip(
    request: Request,
    slip_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    slip = payroll.get_slip(storage, slip_id)
    if slip is None:
        return redirect("/payroll", err="Payslip not found.")
    run = payroll.get_run(storage, slip["period"])
    return render(
        request, "payslip.html", active="payroll", user=user, slip=slip,
        run=run, label=orgtime.month_label(slip["period"]),
        org_name=storage.get_setting("org_name", settings.org_name),
        currency=storage.get_setting("currency_symbol", "₹"),
    )


@router.get("/export.csv")
def export(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    return PlainTextResponse(
        payroll.payroll_csv(storage, period),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="payroll-register-{period}.csv"'},
    )


@router.get("/export-payout.csv")
def export_payout(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    csv_text = payroll.export_bank_payout_csv(storage, period)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="bank-payout-neft-{period}.csv"'},
    )


@router.post("/slip/{slip_id}/status")
def set_single_slip_status(
    request: Request,
    slip_id: int,
    status: str = Form(...),
    reason: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    slip = payroll.get_slip(storage, slip_id)
    if not slip:
        return redirect("/payroll", err="Payslip not found.")
    try:
        payroll.set_slip_status(storage, slip_id, status, actor=user.username, reason=reason)
    except employees_service.ValidationError as exc:
        return redirect(f"/payroll?period={slip['period']}", err=str(exc))
    return redirect(f"/payroll?period={slip['period']}", ok=f"Payslip for {slip['name']} marked {status}.")


@router.post("/slip/{slip_id}/email")
def email_single_slip(
    request: Request,
    slip_id: int,
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    verify_form_csrf(user, csrf)
    slip = payroll.get_slip(storage, slip_id)
    if not slip:
        return redirect("/payroll", err="Payslip not found.")
    emp = employees_service.get_employee(storage, slip["employee_id"])
    email = (emp or {}).get("email")
    phone = (emp or {}).get("phone")
    has_whatsapp = bool(getattr(settings, "notify_whatsapp_payslip", True) and phone)
    if not email and not has_whatsapp:
        return redirect(
            f"/payroll?period={slip['period']}",
            err=f"Cannot send payslip: {slip['name']} has neither an email address nor a phone number on file.",
        )
    months = orgtime.month_label(slip["period"])
    queued_channels = []
    if email:
        row_id = notify.queue_email(
            storage, email, "payslip", settings=settings, category="payslip",
            name=(emp.get("name") or "").split(" ")[0] or "there",
            period=months,
            gross=f"Rs {float(slip['monthly_gross']):,.2f}",
            deductions=f"Rs {float(slip['total_deductions']):,.2f}",
            net=f"Rs {float(slip['net_pay']):,.2f}",
        )
        if row_id:
            storage.execute(
                "UPDATE notifications SET meta=? WHERE id=?",
                (f'{{"template":"payslip","slip_id":{slip["id"]}}}', row_id),
            )
            queued_channels.append(f"Email ({email})")
    if has_whatsapp:
        wa_id = notify.queue_whatsapp(
            storage, phone, "payslip", settings=settings,
            name=(emp.get("name") or "").split(" ")[0] or "there",
            period=months,
        )
        if wa_id:
            storage.execute(
                "UPDATE notifications SET meta=? WHERE id=?",
                (f'{{"template":"payslip","slip_id":{slip["id"]}}}', wa_id),
            )
            queued_channels.append(f"WhatsApp ({phone})")

    results = notify.dispatch(storage, settings, limit=len(queued_channels) or 2)
    channels_label = " & ".join(queued_channels) if queued_channels else "notification queue"
    return redirect(f"/payroll?period={slip['period']}", ok=f"Payslip dispatched successfully to {slip['name']} via {channels_label}.")


@router.post("/slips/bulk-email")
def email_bulk_slips(
    request: Request,
    period: str = Form(...),
    slip_ids: list[int] = Form(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    verify_form_csrf(user, csrf)
    months = orgtime.month_label(period)
    queued_count = 0
    missing_email = 0
    smtp_host = storage.get_setting("smtp_host", "")

    for sid in slip_ids:
        slip = payroll.get_slip(storage, sid)
        if not slip or slip.get("status") == "on_hold":
            continue
        emp = employees_service.get_employee(storage, slip["employee_id"])
        email = (emp or {}).get("email")
        phone = (emp or {}).get("phone")
        has_wa = bool(getattr(settings, "notify_whatsapp_payslip", True) and phone)
        if not email and not has_wa:
            missing_email += 1
            continue
        if email:
            row_id = notify.queue_email(
                storage, email, "payslip", settings=settings, category="payslip",
                name=(emp.get("name") or "").split(" ")[0] or "there",
                period=months,
                gross=f"Rs {float(slip['monthly_gross']):,.2f}",
                deductions=f"Rs {float(slip['total_deductions']):,.2f}",
                net=f"Rs {float(slip['net_pay']):,.2f}",
            )
            if row_id:
                storage.execute(
                    "UPDATE notifications SET meta=? WHERE id=?",
                    (f'{{"template":"payslip","slip_id":{slip["id"]}}}', row_id),
                )
                queued_count += 1
        if has_wa:
            wa_id = notify.queue_whatsapp(
                storage, phone, "payslip", settings=settings,
                name=(emp.get("name") or "").split(" ")[0] or "there",
                period=months,
            )
            if wa_id:
                storage.execute(
                    "UPDATE notifications SET meta=? WHERE id=?",
                    (f'{{"template":"payslip","slip_id":{slip["id"]}}}', wa_id),
                )
                queued_count += 1

    if queued_count > 0:
        if smtp_host:
            results = notify.dispatch(storage, settings, limit=queued_count)
            sent = sum(1 for r in results if r.get("ok"))
            msg = f"Sent {sent} payslip email(s) for {months}."
        else:
            msg = f"Queued {queued_count} payslip email(s). (Note: Configure SMTP in Settings → Email & Notifications to send live emails)."
    else:
        msg = "No payslips could be queued."
    if missing_email:
        msg += f" {missing_email} employee(s) skipped (no email on file)."
    return redirect(f"/payroll?period={period}", ok=msg)


@router.post("/slips/bulk-status")
def set_bulk_slips_status(
    request: Request,
    period: str = Form(...),
    status: str = Form(...),
    slip_ids: list[int] = Form(...),
    reason: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    try:
        count = payroll.bulk_set_slip_status(storage, period, slip_ids, status, actor=user.username, reason=reason)
    except employees_service.ValidationError as exc:
        return redirect(f"/payroll?period={period}", err=str(exc))
    return redirect(f"/payroll?period={period}", ok=f"{count} payslip(s) marked {status}.")


@router.get("/export.zip")
def export_zip(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    zip_bytes = payroll.export_payslips_zip(storage, settings, period)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="payslips-{period}.zip"'},
    )


@router.get("/export-ecr.txt")
def export_ecr(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    ecr_text = payroll.export_epfo_ecr(storage, period)
    return PlainTextResponse(
        ecr_text,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="epfo-ecr-{period}.txt"'},
    )


@router.get("/export-esic.csv")
def export_esic(
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    period = period or orgtime.period_of(orgtime.today(settings))
    csv_text = payroll.export_esic_return(storage, period)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="esic-return-{period}.csv"'},
    )


@router.post("/email")
def email_payslips(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    period: str = Form(...),
    only_missing: str = Form(""),
    send_now: str = Form(""),
):
    """Queue one payslip email per employee for a month, then optionally send."""
    verify_form_csrf(user, csrf)
    run = payroll.get_run(storage, period)
    if run is None:
        return redirect("/payroll", err=f"No payroll run for {period} yet.", period=period)
    if run["status"] == "draft":
        return redirect(
            "/payroll", period=period,
            err="Finalise the run before emailing payslips — draft figures can still change.",
        )

    slips = payroll.list_slips(storage, period)
    queued, skipped, already = 0, 0, 0
    months = orgtime.month_label(period)

    for slip in slips:
        if slip.get("status") == "on_hold":
            skipped += 1
            continue
        employee = employees_service.get_employee(storage, slip["employee_id"])
        address = (employee or {}).get("email")
        phone = (employee or {}).get("phone")
        has_wa = bool(getattr(settings, "notify_whatsapp_payslip", True) and phone)
        if not address and not has_wa:
            skipped += 1
            continue
        if address and only_missing:
            # match this exact payslip only — not any old email that mentions the month (NTF-02)
            previous = storage.query_one(
                "SELECT id FROM notifications WHERE category='payslip' AND recipient=? "
                "AND (meta LIKE ? OR meta LIKE ?) LIMIT 1",
                (address, f'%"slip_id":{slip["id"]}}}%', f'%"slip_id":{slip["id"]},%'),
            )
            if previous:
                already += 1
                continue
        if address:
            row_id = notify.queue_email(
                storage, address, "payslip", settings=settings, category="payslip",
                name=(employee.get("name") or "").split(" ")[0] or "there",
                period=months,
                gross=f"Rs {float(slip['monthly_gross']):,.2f}",
                deductions=f"Rs {float(slip['total_deductions']):,.2f}",
                net=f"Rs {float(slip['net_pay']):,.2f}",
            )
            if row_id:
                queued += 1
                # keep the payslip id so the PDF can be regenerated as an attachment
                storage.execute(
                    "UPDATE notifications SET meta=? WHERE id=?",
                    (f'{{"template":"payslip","slip_id":{slip["id"]}}}', row_id),
                )
        if has_wa:
            wa_id = notify.queue_whatsapp(
                storage, phone, "payslip", settings=settings,
                name=(employee.get("name") or "").split(" ")[0] or "there",
                period=months,
            )
            if wa_id:
                storage.execute(
                    "UPDATE notifications SET meta=? WHERE id=?",
                    (f'{{"template":"payslip","slip_id":{slip["id"]}}}', wa_id),
                )
                queued += 1

    message = f"Queued {queued} payslip email(s) for {months}"
    if skipped:
        message += f", {skipped} skipped (no email address)"
    if already:
        message += f", {already} already sent"
    if queued and send_now:
        results = notify.dispatch(storage, settings, limit=queued)
        message += f". Sent {sum(1 for r in results if r.get('ok'))} now."
    return redirect("/payroll", ok=message + ".", period=period)
