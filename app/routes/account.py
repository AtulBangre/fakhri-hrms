"""Password reset by email OTP, and the profile/bank-details approval workflow."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import SessionUser, current_user, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import account as account_service
from ..services import employees as employees_service
from ..services import records
from ..uploads import human_size

router = APIRouter(tags=["account"])


# ------------------------------------------------------------- forgot password
@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return render(request, "forgot_password.html", active="login", user=None)


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    username: str = Form(...),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Always answers the same way, whether or not the account exists."""
    clean_user = username.strip().lower()
    _account, message = account_service.request_reset(storage, settings, clean_user)
    return redirect("/reset-password", ok=message, u=clean_user)


@router.get("/reset-password")
def reset_password_page(request: Request, u: str | None = None):
    return render(
        request, "reset_password.html", active="login", user=None, username_value=u or ""
    )


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    username: str = Form(...),
    code: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(""),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    if confirm and confirm != password:
        return render(
            request, "reset_password.html", active="login", user=None,
            username_value=username, error="Both passwords must match.",
        )
    ok, message = account_service.complete_reset(
        storage, settings, username=username.strip().lower(), code=code, new_password=password
    )
    if not ok:
        return render(
            request, "reset_password.html", active="login", user=None,
            username_value=username, error=message,
        )
    return redirect("/login", ok=message)


# ------------------------------------------------------- employee: my profile
@router.get("/me/profile")
def my_profile(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    tab: str = "details",
):
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    documents = records.list_documents(storage, employee_id=employee["id"])
    return render(
        request,
        "me_profile.html",
        active="me-profile",
        user=user,
        employee=employee,
        tab="documents" if tab == "documents" else "details",
        rows=documents,
        summary=records.document_summary(storage, employee["id"]),
        doc_summary=records.document_summary(storage, employee["id"]),
        categories=records.DOC_CATEGORIES,
        sizes={row["id"]: human_size(row["size"]) for row in documents},
        required=records.REQUIRED_DOCS,
        fields=account_service.CHANGEABLE_FIELDS,
        pending=account_service.pending_changes(storage, employee_id=employee["id"]),
        history=storage.query(
            "SELECT * FROM profile_change_requests WHERE employee_id = ? AND status != 'pending' "
            "ORDER BY id DESC LIMIT 10",
            (employee["id"],),
        ),
    )


@router.post("/me/profile")
def submit_profile_changes(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    phone: str = Form(None),
    email: str = Form(None),
    address: str = Form(None),
    bank_name: str = Form(None),
    account_holder: str = Form(None),
    account_number: str = Form(None),
    ifsc: str = Form(None),
    emergency_contact: str = Form(None),
):
    verify_form_csrf(user, csrf)
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))

    # None means "the form did not include this field"; "" means "unchanged"
    values = {
        key: value
        for key, value in {
            "phone": phone, "email": email, "address": address,
            "bank_name": bank_name, "account_holder": account_holder,
            "account_number": account_number, "ifsc": ifsc,
            "emergency_contact": emergency_contact,
        }.items()
        if value is not None
    }
    created = account_service.submit_change_requests(
        storage, settings, employee=employee, values=values, requested_by=user.id
    )
    if not created:
        return redirect("/me/profile", ok="Nothing changed.")
    labels = ", ".join(account_service.CHANGEABLE_FIELDS.get(row["field"], row["field"]) for row in created)
    return redirect("/me/profile", ok=f"Request sent to HR for: {labels}.")


@router.post("/me/profile/request/{request_id}/cancel")
def cancel_profile_request(
    request: Request,
    request_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    row = storage.query_one("SELECT * FROM profile_change_requests WHERE id = ?", (request_id,))
    if row is None or row["employee_id"] != int(user.employee_id or 0):
        return redirect("/me/profile", err="That request was not found in your account.")
    if row["status"] != "pending":
        return redirect("/me/profile", err="That request has already been decided.")
    storage.execute("DELETE FROM profile_change_requests WHERE id = ?", (request_id,))
    return redirect("/me/profile", ok="Request withdrawn.")


# --------------------------------------------------- HR: approve the requests
@router.get("/profile-requests")
def profile_requests(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    return render(
        request,
        "profile_requests.html",
        active="profile-requests",
        user=user,
        pending=account_service.pending_changes(storage),
        decided=storage.query(
            """
            SELECT r.*, e.name AS employee_name, e.code AS employee_code
            FROM profile_change_requests r JOIN employees e ON e.id = r.employee_id
            WHERE r.status != 'pending' ORDER BY r.decided_at DESC, r.id DESC LIMIT 25
            """
        ),
        fields=account_service.CHANGEABLE_FIELDS,
    )


@router.post("/profile-requests/{request_id}/{decision}")
def decide_profile_request(
    request: Request,
    request_id: int,
    decision: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    if decision not in ("approve", "reject"):
        return redirect("/profile-requests", err="Unknown decision.")
    try:
        row = account_service.decide(
            storage, settings, request_id=request_id, approve=decision == "approve",
            decided_by=user.username, note=note or None,
        )
    except ValueError as exc:
        return redirect("/profile-requests", err=str(exc))
    return redirect(
        "/profile-requests",
        ok=f"{row['field']} {'updated' if row['status'] == 'approved' else 'change rejected'}.",
    )
