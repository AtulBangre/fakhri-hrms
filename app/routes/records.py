"""Documents, assets and expense claims — HR screens and employee self-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from ..auth import SessionUser, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..uploads import UploadError, human_size, path_for
from ..services import employees as employees_service
from ..services import orgtime
from ..services import records

router = APIRouter(tags=["records"])


def _as_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


# ============================================================== DOCUMENTS (HR)
@router.get("/documents")
def documents_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    employee_id: int | None = None,
    category: str = "all",
):
    rows = records.list_documents(storage, employee_id=employee_id, category=category)
    people = employees_service.list_employees(storage, status="all", limit=500)
    return render(
        request, "documents.html", active="documents", user=user,
        rows=rows, employees=people, categories=records.DOC_CATEGORIES,
        summary=records.document_summary(storage),
        filters={"employee_id": employee_id or "", "category": category},
        sizes={row["id"]: human_size(row["size"]) for row in rows},
    )


@router.post("/documents")
def upload_document(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    category: str = Form("other"),
    title: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
):
    verify_form_csrf(user, csrf)
    try:
        records.add_document(
            storage, settings, employee_id=employee_id, data=file.file.read(),
            original_name=file.filename, category=category, title=title or None,
            notes=notes or None, uploaded_by=user.username,
        )
    except (UploadError, ValueError) as exc:
        return redirect("/documents", err=str(exc))
    return redirect("/documents", ok="Document uploaded.")


@router.get("/documents/{document_id}/file")
def download_document(
    request: Request,
    document_id: int,
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Download a document. HR and the owning employee only."""
    session = request.state.user if hasattr(request.state, "user") else None
    from ..auth import current_user

    session = current_user(request)
    row = records.get_document(storage, document_id)
    if not session.is_hr and session.employee_id != row["employee_id"]:
        return redirect("/me/documents", err="That document is not yours.")
    try:
        path = path_for(settings, "documents", row["stored_name"])
    except UploadError as exc:
        return redirect("/documents" if session.is_hr else "/me/documents", err=str(exc))
    return FileResponse(path, media_type=row["mime"], filename=row["filename"])


@router.post("/documents/{document_id}/verify")
def verify_document(
    request: Request,
    document_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    records.set_verified(storage, document_id, verified=True, actor=user.username)
    return redirect("/documents", ok="Marked as verified.")


@router.post("/documents/{document_id}/delete")
def delete_document(
    request: Request,
    document_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    records.delete_document(storage, settings, document_id, actor=user.username)
    return redirect("/documents", ok="Document deleted.")


# ========================================================= DOCUMENTS (employee)
@router.get("/me/documents")
def my_documents(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    rows = records.list_documents(storage, employee_id=employee["id"])
    summary = records.document_summary(storage, employee["id"])
    return render(
        request, "me_documents.html", active="me-documents", user=user,
        employee=employee, rows=rows, summary=summary,
        categories=records.DOC_CATEGORIES,
        sizes={row["id"]: human_size(row["size"]) for row in rows},
        required=records.REQUIRED_DOCS,
    )


@router.post("/me/documents")
def upload_my_document(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    category: str = Form("other"),
    title: str = Form(""),
    file: UploadFile = File(...),
):
    verify_form_csrf(user, csrf)
    employee_id = int(user.employee_id or 0)
    try:
        records.add_document(
            storage, settings, employee_id=employee_id, data=file.file.read(),
            original_name=file.filename, category=category, title=title or None,
            uploaded_by=user.username,
        )
    except (UploadError, ValueError) as exc:
        return redirect("/me/documents", err=str(exc))
    return redirect("/me/documents", ok="Uploaded — HR will verify it.")


# ================================================================== ASSETS (HR)
@router.get("/assets")
def assets_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    status: str = "all",
    category: str = "all",
    q: str | None = None,
):
    return render(
        request, "assets.html", active="assets", user=user,
        rows=records.list_assets(storage, status=status, category=category, q=q),
        stats=records.asset_stats(storage),
        employees=employees_service.list_employees(storage, status="active", limit=500),
        categories=records.ASSET_CATEGORIES,
        statuses=records.ASSET_STATUSES,
        filters={"status": status, "category": category, "q": q or ""},
    )


@router.post("/assets")
def create_asset(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    tag: str = Form(...),
    name: str = Form(...),
    category: str = Form("laptop"),
    serial: str = Form(""),
    purchase_date: str = Form(""),
    cost: str = Form("0"),
    notes: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        records.add_asset(
            storage, tag=tag, name=name, category=category, serial=serial or None,
            purchase_date=purchase_date or None, cost=_as_float(cost), notes=notes or None,
            actor=user.username,
        )
    except ValueError as exc:
        return redirect("/assets", err=str(exc))
    return redirect("/assets", ok=f"Asset {tag.upper()} added.")


@router.post("/assets/{asset_id}/assign")
def assign_asset(
    request: Request,
    asset_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    employee_id: int = Form(...),
    condition_note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        records.assign_asset(
            storage, asset_id=asset_id, employee_id=employee_id,
            condition_note=condition_note or None, actor=user.username,
        )
    except ValueError as exc:
        return redirect("/assets", err=str(exc))
    return redirect("/assets", ok="Asset assigned.")


@router.post("/assets/{asset_id}/return")
def return_asset(
    request: Request,
    asset_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    condition_note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        records.return_asset(storage, asset_id=asset_id, condition_note=condition_note or None,
                             actor=user.username)
    except ValueError as exc:
        return redirect("/assets", err=str(exc))
    return redirect("/assets", ok="Asset returned to stock.")


@router.get("/assets/{asset_id}")
def asset_detail(
    request: Request,
    asset_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    try:
        asset = records.get_asset(storage, asset_id)
    except ValueError as exc:
        return redirect("/assets", err=str(exc))
    return render(
        request, "asset_detail.html", active="assets", user=user, asset=asset,
        history=records.asset_history(storage, asset_id),
        employees=employees_service.list_employees(storage, status="active", limit=500),
        categories=records.ASSET_CATEGORIES,
    )


@router.get("/me/assets")
def my_assets(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
):
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    return render(
        request, "me_assets.html", active="me-assets", user=user, employee=employee,
        rows=records.assets_for_employee(storage, employee["id"]),
        categories=records.ASSET_CATEGORIES,
    )


# ================================================================ EXPENSES (HR)
@router.get("/expenses")
def expenses_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    status: str = "all",
):
    return render(
        request, "expenses.html", active="expenses", user=user,
        rows=records.list_claims(storage, status=status),
        stats=records.expense_stats(storage),
        categories=records.EXPENSE_CATEGORIES,
        status_filter=status,
    )


@router.post("/expenses/{claim_id}/{decision}")
def decide_expense(
    request: Request,
    claim_id: int,
    decision: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    if decision not in ("approve", "reject"):
        return redirect("/expenses", err="Unknown decision.")
    try:
        records.decide_claim(
            storage, claim_id=claim_id, approve=decision == "approve",
            decided_by=user.username, note=note or None,
        )
    except ValueError as exc:
        return redirect("/expenses", err=str(exc))
    return redirect("/expenses", ok=f"Claim {decision}d.")


@router.get("/expenses/{claim_id}/receipt")
def claim_receipt(
    request: Request,
    claim_id: int,
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    from ..auth import current_user

    session = current_user(request)
    claim = records.get_claim(storage, claim_id)
    if not session.is_hr and session.employee_id != claim["employee_id"]:
        return redirect("/me/expenses", err="That receipt is not yours.")
    if not claim["receipt_name"]:
        return redirect("/expenses", err="No receipt was attached to this claim.")
    try:
        path = path_for(settings, "receipts", claim["receipt_name"])
    except UploadError as exc:
        return redirect("/expenses", err=str(exc))
    return FileResponse(path, media_type=claim["receipt_mime"] or "application/octet-stream",
                        filename=f"receipt-{claim_id}")


# ========================================================== EXPENSES (employee)
@router.get("/me/expenses")
def my_expenses(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    return render(
        request, "me_expenses.html", active="me-expenses", user=user, employee=employee,
        rows=records.list_claims(storage, employee_id=employee["id"]),
        stats=records.expense_stats(storage, employee_id=employee["id"]),
        categories=records.EXPENSE_CATEGORIES,
        today=orgtime.today(settings),
    )


@router.post("/me/expenses")
def submit_expense(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    category: str = Form("travel"),
    amount: str = Form(...),
    expense_date: str = Form(...),
    description: str = Form(""),
    receipt: UploadFile | None = File(None),
):
    verify_form_csrf(user, csrf)
    employee_id = int(user.employee_id or 0)
    payload = None
    if receipt is not None and receipt.filename:
        payload = (receipt.file.read(), receipt.filename)
    try:
        records.submit_claim(
            storage, settings, employee_id=employee_id, amount=_as_float(amount),
            expense_date=expense_date, category=category, description=description or None,
            receipt=payload, submitted_by=user.username,
        )
    except (UploadError, ValueError) as exc:
        return redirect("/me/expenses", err=str(exc))
    return redirect("/me/expenses", ok="Claim submitted — HR will review it.")
