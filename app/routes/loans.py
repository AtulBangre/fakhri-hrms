"""Loans and Salary Advances management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import SessionUser, require_hr
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import loans as loans_service
from ..services import employees as employees_service
from ..services import orgtime

router = APIRouter(prefix="/loans", tags=["loans"])


@router.get("", response_class=HTMLResponse)
def loans_dashboard(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    status: str | None = None,
):
    loans = loans_service.list_loans(storage, status=status)
    summary = loans_service.loan_summary(storage)
    employees = storage.query("SELECT id, code, name, department, designation FROM employees WHERE status = 'active' ORDER BY name")
    today = orgtime.today(settings)

    return render(
        request,
        "loans.html",
        active="loans",
        user=user,
        loans=loans,
        summary=summary,
        employees=employees,
        today=today,
        status_filter=status or "all",
    )


@router.post("")
def disburse_loan(
    employee_id: int = Form(...),
    loan_type: str = Form("salary_advance"),
    principal_amount: float = Form(...),
    monthly_emi: float = Form(0.0),
    disbursed_on: str = Form(...),
    reason: str = Form(""),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        loan_id = loans_service.create_loan(
            storage,
            employee_id=employee_id,
            principal_amount=principal_amount,
            monthly_emi=monthly_emi,
            disbursed_on=disbursed_on,
            loan_type=loan_type,
            reason=reason,
            actor=user.username,
        )
    except employees_service.ValidationError as exc:
        return redirect("/loans", err=str(exc))
    except Exception as exc:
        return redirect("/loans", err=f"Error disbursing loan: {exc}")

    return redirect("/loans", ok=f"Loan #{loan_id} of ₹{principal_amount:,.2f} recorded successfully.")


@router.post("/{loan_id}/close")
def close_loan_route(
    loan_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    reason: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        loans_service.close_loan(storage, loan_id, actor=user.username, reason=reason)
    except Exception as exc:
        return redirect("/loans", err=f"Failed to close loan: {exc}")

    return redirect("/loans", ok=f"Loan #{loan_id} has been marked as closed.")


@router.post("/{loan_id}/approve")
def approve_loan_route(
    loan_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        loans_service.approve_loan(storage, loan_id, actor=user.username)
    except Exception as exc:
        return redirect("/loans", err=f"Failed to approve loan: {exc}")

    return redirect("/loans", ok=f"Loan #{loan_id} has been approved and activated.")


@router.post("/{loan_id}/reject")
def reject_loan_route(
    loan_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    reason: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        loans_service.reject_loan(storage, loan_id, actor=user.username, reason=reason)
    except Exception as exc:
        return redirect("/loans", err=f"Failed to reject loan: {exc}")

    return redirect("/loans", ok=f"Loan #{loan_id} has been rejected.")
