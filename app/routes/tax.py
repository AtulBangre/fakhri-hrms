"""Income tax screens: HR's TDS register, per-employee profiles, Form 16, and ESS declarations.

The maths lives in ``services/tax.py``; these routes only collect input and show
results. Two rules carry over from that module and are visible in the UI:

* the slab calculation is a **projection** — HR (or the CA) confirms the real
  annual TDS, and only that confirmed figure drives payroll and Form 16;
* an employee can declare investments and pick a regime for themselves, but can
  never see or touch anyone else's profile (every ``/me/tax`` query is keyed on
  the session's employee id).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from ..auth import SessionUser, current_user, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import tax
from ..uploads import UploadError, path_for, read_capped

router = APIRouter(tags=["tax"])


def _as_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _fy(settings, fy: str | None) -> str:
    """Accept only a well-formed FY like ``2026-27``; anything else means "current"."""
    if fy and len(fy) == 7 and fy[4] == "-" and fy[:4].isdigit() and fy[5:].isdigit():
        if int(fy[5:]) == (int(fy[:4]) + 1) % 100:
            return fy
    return tax.current_fy(settings)


async def _read_upload(file: UploadFile | None) -> tuple[bytes, str] | None:
    if file is None or not file.filename:
        return None
    data = read_capped(file.file)
    return (data, file.filename) if data else None


# ====================================================================== HR
@router.get("/tax")
def register_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    fy: str | None = None,
):
    fy = _fy(settings, fy)
    rows = tax.register(storage, settings, fy)
    pending = storage.query(
        """
        SELECT d.*, e.name, e.code FROM tax_declarations d JOIN employees e ON e.id = d.employee_id
        WHERE d.fiscal_year = ? AND d.status = 'declared' ORDER BY d.declared_at
        """,
        (fy,),
    )
    for row in pending:
        row["section_label"] = tax.SECTIONS.get(row["section"], (row["section"], None))[0]
    return render(
        request, "tax.html", active="tax", user=user, fy=fy, fys=tax.list_fys(storage, settings),
        rows=rows, summary={
            "employees": len(rows),
            "confirmed": sum(1 for r in rows if r["confirmed"]),
            "pending": sum(1 for r in rows if not r["confirmed"]),
            "projected": round(sum(r["projected_tax"] for r in rows), 2),
            "confirmed_total": round(sum(r["confirmed_tds"] or 0 for r in rows), 2),
            "deducted": round(sum(r["deducted"] for r in rows), 2),
        },
        pending=pending,
    )


@router.post("/tax/import")
async def import_schedule(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    csv_text: str = Form(""),
    step: str = Form("preview"),
    file: UploadFile | None = File(None),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    text = csv_text
    if file is not None and file.filename:
        try:
            raw = read_capped(file.file, limit=2 * 1024 * 1024)
        except UploadError as exc:
            return redirect("/tax", fy=fy, err=str(exc))
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return redirect("/tax", fy=fy, err="That file is not a UTF-8 CSV. Save it as CSV (UTF-8) and retry.")
    if not text.strip():
        return redirect("/tax", fy=fy, err="Paste the CA's schedule or choose a CSV file.")
    if step != "apply":
        # Always show what will change first; nothing is written on a preview.
        parsed = tax.parse_tds_schedule(storage, text)
        for row in parsed["rows"]:
            row["current"] = tax.get_profile(storage, row["employee_id"], fy).get("confirmed_tds")
        return render(request, "tax_import_preview.html", active="tax", user=user, fy=fy,
                      parsed=parsed, csv_text=text)
    result = tax.import_tds_schedule(storage, settings, csv_text=text, fy=fy, actor=user.username)
    if result["errors"]:
        return redirect("/tax", fy=fy, err="Nothing was applied — fix these lines: " + "; ".join(result["errors"][:5]))
    return redirect("/tax", fy=fy, ok=f"TDS schedule applied to {result['applied']} employee(s).")


@router.get("/tax/employee/{employee_id}")
def employee_tax_page(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    fy: str | None = None,
):
    fy = _fy(settings, fy)
    employee = employees_service.get_employee(storage, employee_id)
    if employee is None:
        return redirect("/tax", fy=fy, err="Employee not found.")
    comparison = tax.compare_regimes(storage, settings, employee=employee, fy=fy)
    profile = tax.get_profile(storage, employee_id, fy)
    return render(
        request, "tax_employee.html", active="tax", user=user, fy=fy,
        fys=tax.list_fys(storage, settings), employee=employee, profile=profile,
        declarations=tax.list_declarations(storage, employee_id, fy),
        comparison=comparison,
        current=comparison[profile.get("regime") or "new"],
        sections=tax.SECTIONS,
    )


@router.post("/tax/employee/{employee_id}/profile")
def save_profile(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    regime: str = Form("new"),
    metro: str = Form(""),
    annual_rent: str = Form("0"),
    basic_annual: str = Form("0"),
    hra_annual: str = Form("0"),
    monthly_tds: str = Form("0"),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    back = f"/tax/employee/{employee_id}"
    try:
        tax.ensure_profile(storage, settings, employee_id=employee_id, fy=fy)
        tax.update_profile(
            storage, employee_id=employee_id, fy=fy, regime=regime, metro=1 if metro else 0,
            annual_rent=_as_float(annual_rent), basic_annual=_as_float(basic_annual),
            hra_annual=_as_float(hra_annual), monthly_tds=_as_float(monthly_tds),
        )
    except ValueError as exc:
        return redirect(back, fy=fy, err=str(exc))
    storage.audit(actor=user.username, action="tax_profile", entity="employee", entity_id=employee_id,
                  detail=f"{fy} regime={regime}")
    return redirect(back, fy=fy, ok="Tax profile saved.")


@router.post("/tax/employee/{employee_id}/confirm")
def confirm(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    annual_tds: str = Form(...),
    taxable_income: str = Form(""),
    note: str = Form(""),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    back = f"/tax/employee/{employee_id}"
    try:
        tax.ensure_profile(storage, settings, employee_id=employee_id, fy=fy)
        tax.confirm_tds(
            storage, employee_id=employee_id, fy=fy, annual_tds=_as_float(annual_tds),
            taxable_income=_as_float(taxable_income) if taxable_income.strip() else None,
            actor=user.username, note=note.strip() or None,
        )
    except ValueError as exc:
        return redirect(back, fy=fy, err=str(exc))
    return redirect(back, fy=fy, ok="Annual TDS confirmed — the next payroll run deducts it.")


@router.post("/tax/employee/{employee_id}/declarations")
async def hr_add_declaration(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    section: str = Form(...),
    amount: str = Form(...),
    description: str = Form(""),
    proof: UploadFile | None = File(None),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    back = f"/tax/employee/{employee_id}"
    try:
        tax.add_declaration(
            storage, settings, employee_id=employee_id, fy=fy, section=section,
            amount=_as_float(amount), description=description.strip() or None,
            proof=await _read_upload(proof), declared_by=user.username,
        )
    except ValueError as exc:
        return redirect(back, fy=fy, err=str(exc))
    return redirect(back, fy=fy, ok="Declaration added.")


@router.post("/tax/declarations/{declaration_id}/{decision}")
def decide_declaration(
    request: Request,
    declaration_id: int,
    decision: str,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    return_to: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        row = tax.get_declaration(storage, declaration_id)
    except ValueError as exc:
        return redirect("/tax", err=str(exc))
    back = "/tax" if return_to == "/tax" else f"/tax/employee/{row['employee_id']}"
    if decision == "delete":
        tax.delete_declaration(storage, settings, declaration_id, actor=user.username)
        return redirect(back, fy=row["fiscal_year"], ok="Declaration deleted.")
    if decision not in ("verify", "reject"):
        return redirect(back, fy=row["fiscal_year"], err="Unknown action.")
    tax.verify_declaration(storage, declaration_id, verified=decision == "verify", actor=user.username)
    storage.audit(actor=user.username, action=f"tax_declaration_{decision}", entity="employee",
                  entity_id=row["employee_id"], detail=f"{row['section']} {row['amount']:.0f}")
    return redirect(back, fy=row["fiscal_year"],
                    ok="Declaration verified." if decision == "verify" else "Declaration rejected.")


@router.get("/tax/employee/{employee_id}/form16")
def form16(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    fy: str | None = None,
):
    fy = _fy(settings, fy)
    try:
        data = tax.form16_data(storage, settings, employee_id=employee_id, fy=fy)
    except ValueError as exc:
        return redirect(f"/tax/employee/{employee_id}", fy=fy, err=str(exc))
    return render(
        request, "form16.html", active="tax", user=user, data=data, fy=fy,
        back_url=f"/tax/employee/{employee_id}?fy={fy}",
        org_name=storage.get_setting("org_name", settings.org_name),
    )


@router.get("/tax/declarations/{declaration_id}/proof")
def download_proof(
    request: Request,
    declaration_id: int,
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Proof file for a declaration — HR, or the employee who owns it."""
    session = current_user(request)
    home = "/tax" if session.is_hr else "/me/tax"
    try:
        row = tax.get_declaration(storage, declaration_id)
    except ValueError:
        return redirect(home, err="Declaration not found.")
    if not session.is_hr and int(session.employee_id or 0) != row["employee_id"]:
        return redirect(home, err="Declaration not found.")
    if not row.get("proof_name"):
        return redirect(home, err="No proof was attached to that declaration.")
    try:
        path = path_for(settings, "proofs", row["proof_name"])
    except UploadError as exc:
        return redirect(home, err=str(exc))
    return FileResponse(path, media_type=row["proof_mime"] or "application/octet-stream",
                        filename=f"{row['section']}-proof-{declaration_id}{path.suffix}")


# ===================================================================== ESS
@router.get("/me/tax")
def my_tax(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    fy: str | None = None,
):
    fy = _fy(settings, fy)
    employee = employees_service.get_employee(storage, int(user.employee_id or 0))
    if employee is None:
        return redirect("/me", err="Your employee record is no longer available. Please ask HR.")
    comparison = tax.compare_regimes(storage, settings, employee=employee, fy=fy)
    profile = tax.get_profile(storage, employee["id"], fy)
    return render(
        request, "me_tax.html", active="me-tax", user=user, fy=fy,
        fys=tax.list_fys(storage, settings), employee=employee, profile=profile,
        declarations=tax.list_declarations(storage, employee["id"], fy),
        comparison=comparison, current=comparison[profile.get("regime") or "new"],
        sections=tax.SECTIONS,
    )


@router.post("/me/tax/profile")
def my_tax_profile(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    regime: str = Form("new"),
    metro: str = Form(""),
    annual_rent: str = Form("0"),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    employee_id = int(user.employee_id or 0)
    try:
        tax.ensure_profile(storage, settings, employee_id=employee_id, fy=fy)
        # Only the employee's own choices; salary split and TDS stay with HR.
        tax.update_profile(storage, employee_id=employee_id, fy=fy, regime=regime,
                           metro=1 if metro else 0, annual_rent=_as_float(annual_rent))
    except ValueError as exc:
        return redirect("/me/tax", fy=fy, err=str(exc))
    storage.audit(actor=user.username, action="tax_regime_choice", entity="employee",
                  entity_id=employee_id, detail=f"{fy} regime={regime}")
    return redirect("/me/tax", fy=fy, ok="Saved. HR will use this when confirming your TDS.")


@router.post("/me/tax/declarations")
async def my_declaration(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    fy: str = Form(""),
    section: str = Form(...),
    amount: str = Form(...),
    description: str = Form(""),
    proof: UploadFile | None = File(None),
):
    verify_form_csrf(user, csrf)
    fy = _fy(settings, fy)
    try:
        tax.add_declaration(
            storage, settings, employee_id=int(user.employee_id or 0), fy=fy, section=section,
            amount=_as_float(amount), description=description.strip() or None,
            proof=await _read_upload(proof), declared_by=user.username,
        )
    except ValueError as exc:
        return redirect("/me/tax", fy=fy, err=str(exc))
    return redirect("/me/tax", fy=fy, ok="Declaration submitted — HR will verify the proof.")


@router.post("/me/tax/declarations/{declaration_id}/delete")
def delete_my_declaration(
    request: Request,
    declaration_id: int,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    try:
        row = tax.get_declaration(storage, declaration_id)
    except ValueError:
        return redirect("/me/tax", err="Declaration not found.")
    if row["employee_id"] != int(user.employee_id or 0):
        return redirect("/me/tax", err="Declaration not found.")
    if row["status"] == "verified":
        return redirect("/me/tax", fy=row["fiscal_year"],
                        err="HR has already verified that declaration — ask HR to change it.")
    tax.delete_declaration(storage, settings, declaration_id, actor=user.username)
    return redirect("/me/tax", fy=row["fiscal_year"], ok="Declaration withdrawn.")


@router.get("/me/tax/form16")
def my_form16(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    fy: str | None = None,
):
    fy = _fy(settings, fy)
    try:
        data = tax.form16_data(storage, settings, employee_id=int(user.employee_id or 0), fy=fy)
    except ValueError:
        return redirect("/me/tax", fy=fy,
                        err="Form 16 is available once HR has confirmed your TDS for this year.")
    return render(
        request, "form16.html", active="me-tax", user=user, data=data, fy=fy,
        back_url=f"/me/tax?fy={fy}",
        org_name=storage.get_setting("org_name", settings.org_name),
    )
