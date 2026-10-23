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
    return render(
        request,
        "payroll.html",
        active="payroll",
        user=user,
        runs=runs,
        period=period,
        period_label=orgtime.month_label(period),
        run=run,
        slips=payroll.list_slips(storage, period) if run else [],
        totals=payroll.period_summary(storage, period)["totals"],
        basis_days=payroll.basis_days_for(storage, period),
        employees_count=len(employees_service.list_employees(storage, limit=500)),
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
):
    verify_form_csrf(user, csrf)
    try:
        run = payroll.generate_run(
            storage, settings, period, actor=user.username, include_inactive=bool(include_inactive)
        )
    except employees_service.ValidationError as exc:
        return redirect("/payroll", err=str(exc), period=period)
    return redirect(
        "/payroll", period=period,
        ok=f"{orgtime.month_label(period)} payroll calculated — {run['status']}. Review the payslips below.",
    )


@router.post("/status")
def set_status(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
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
    payroll.delete_run(storage, period, actor=user.username)
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
    note: str = Form(""),
    period: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        payroll.update_slip(
            storage, slip_id, bonus=_as_float(bonus), advance=_as_float(advance),
            other_deduction=_as_float(other_deduction), note=note or None, actor=user.username,
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
        headers={"Content-Disposition": f'attachment; filename="payroll-{period}.csv"'},
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
        employee = employees_service.get_employee(storage, slip["employee_id"])
        address = (employee or {}).get("email")
        if not address:
            skipped += 1
            continue
        if only_missing:
            previous = storage.query_one(
                "SELECT id FROM notifications WHERE category='payslip' AND recipient=? "
                "AND (meta LIKE ? OR body LIKE ?) LIMIT 1",
                (address, f'%"slip_id":{slip["id"]}%', f"%{months}%"),
            )
            if previous:
                already += 1
                continue
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

    message = f"Queued {queued} payslip email(s) for {months}"
    if skipped:
        message += f", {skipped} skipped (no email address)"
    if already:
        message += f", {already} already sent"
    if queued and send_now:
        results = notify.dispatch(storage, settings, limit=queued)
        message += f". Sent {sum(1 for r in results if r.get('ok'))} now."
    return redirect("/payroll", ok=message + ".", period=period)
