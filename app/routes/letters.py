"""HR Letters router: Offer, Appointment, Increment, and Relieving Letters."""

from __future__ import annotations

import json
from datetime import date
from fastapi import APIRouter, Depends, Form, Request, Response

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import letters as letters_service

router = APIRouter(prefix="/letters", tags=["letters"])


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    s = str(val).strip()
    return int(s) if s.isdigit() else None


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip() or 0.0)
    except (ValueError, TypeError):
        return 0.0


@router.get("")
def letters_index(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    employee_id: str | None = None,
):
    storage._seed_letter_templates()
    emp_id = _safe_int(employee_id)
    templates = letters_service.list_templates(storage)
    history = letters_service.list_generated_letters(storage, employee_id=emp_id, limit=100)
    employees = employees_service.list_employees(storage, status="all", limit=500)
    return render(
        request,
        "letters.html",
        active="letters",
        user=user,
        templates=templates,
        history=history,
        employees=employees,
        selected_employee_id=emp_id,
    )


@router.get("/generate")
def generate_page(
    request: Request,
    slug: str = "offer",
    target_type: str = "",
    employee_id: str | None = None,
    candidate_name: str = "",
    candidate_email: str = "",
    candidate_phone: str = "",
    candidate_role: str = "",
    candidate_dept: str = "",
    candidate_gross: str | None = None,
    candidate_joining: str = "",
    probation_months: str = "3",
    notice_period: str = "30",
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    storage._seed_letter_templates()
    template = letters_service.get_template(storage, slug)
    if not template:
        return redirect("/letters", err=f"Template '{slug}' not found.")
    
    emp_id = _safe_int(employee_id)
    cand_gross = _safe_float(candidate_gross)
    employees = employees_service.list_employees(storage, status="all", limit=500)
    templates = letters_service.list_templates(storage)
    
    # If target_type not explicitly given: Offer defaults to candidate mode, others default to employee mode
    if not target_type:
        target_type = "candidate" if slug == "offer" else "employee"

    context = {}
    rendered_subject = template["subject"] or ""
    rendered_body = template["body_text"] or ""
    
    if target_type == "candidate":
        if candidate_name.strip():
            context = letters_service.build_candidate_context(
                storage,
                candidate_name=candidate_name,
                designation=candidate_role or "Associate",
                department=candidate_dept or "General",
                monthly_gross=cand_gross,
                joined_on=candidate_joining or None,
                probation_months=probation_months or "3",
                notice_period=notice_period or "30",
                custom_data={"email": candidate_email, "phone": candidate_phone},
            )
            rendered_subject = letters_service.render_tokens(template["subject"] or "", context)
            rendered_body = letters_service.render_tokens(template["body_text"] or "", context)
        else:
            rendered_subject = template["subject"] or ""
            rendered_body = template["body_text"] or ""
    elif target_type == "employee" and emp_id:
        try:
            context = letters_service.build_employee_context(storage, emp_id)
            rendered_subject = letters_service.render_tokens(template["subject"] or "", context)
            rendered_body = letters_service.render_tokens(template["body_text"] or "", context)
        except Exception:
            pass

    return render(
        request,
        "letters_generate.html",
        active="letters",
        user=user,
        letter_template=template,
        templates=templates,
        employees=employees,
        employee_id=emp_id,
        target_type=target_type,
        candidate_name=candidate_name,
        candidate_email=candidate_email,
        candidate_phone=candidate_phone,
        candidate_role=candidate_role,
        candidate_dept=candidate_dept,
        candidate_gross=cand_gross,
        candidate_joining=candidate_joining,
        probation_months=probation_months,
        notice_period=notice_period,
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
        context=context,
    )


@router.post("/download")
def download_letter(
    request: Request,
    slug: str = Form(...),
    target_type: str = Form("employee"),
    employee_id: str | None = Form(None),
    candidate_name: str = Form(""),
    candidate_email: str = Form(""),
    candidate_phone: str = Form(""),
    candidate_role: str = Form(""),
    candidate_dept: str = Form(""),
    candidate_gross: str | None = Form(None),
    candidate_joining: str = Form(""),
    probation_months: str = Form("3"),
    notice_period: str = Form("30"),
    subject: str = Form(""),
    body_text: str = Form(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    verify_form_csrf(user, csrf)
    org_name = storage.get_setting("org_name", settings.org_name) or "Fakhri IT Services"
    template = letters_service.get_template(storage, slug)
    title = template["title"] if template else slug.title()
    letter_date = date.today().strftime("%d %b %Y")
    cur_year = date.today().year
    emp_id = _safe_int(employee_id)
    cand_gross = _safe_float(candidate_gross)

    if target_type == "candidate":
        cand_name_clean = candidate_name.strip()
        if not cand_name_clean:
            return redirect(f"/letters/generate?slug={slug}&target_type=candidate", err="Candidate Name is required to generate letter.")
        
        cand_slug_name = "".join(c for c in cand_name_clean if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")[:25]
        ref_no = f"FITS/HR/{slug.upper()}/{cur_year}/CAND-{cand_slug_name.upper()[:10]}"
        filename = f"Candidate_{cand_slug_name}_{slug}_{date.today().isoformat()}.pdf"

        cand_data = {
            "name": cand_name_clean,
            "email": candidate_email.strip(),
            "phone": candidate_phone.strip(),
            "designation": candidate_role.strip(),
            "department": candidate_dept.strip(),
            "monthly_gross": cand_gross,
            "joined_on": candidate_joining.strip(),
            "probation_months": probation_months.strip(),
            "notice_period": notice_period.strip(),
        }

        pdf_bytes = letters_service.render_letter_pdf(
            org_name=org_name,
            ref_no=ref_no,
            letter_date=letter_date,
            subject=subject,
            body_text=body_text,
        )

        letters_service.record_generated_letter(
            storage,
            employee_id=None,
            candidate_name=cand_name_clean,
            candidate_email=candidate_email.strip() or None,
            candidate_phone=candidate_phone.strip() or None,
            candidate_data_json=json.dumps(cand_data),
            template_slug=slug,
            title=title,
            subject=subject,
            body_text=body_text,
            pdf_filename=filename,
            created_by=user.username,
        )

        storage.audit(
            actor=user.username,
            action="generate_letter",
            entity="letter",
            entity_id=None,
            detail=f"{title} generated for prospective candidate {cand_name_clean}",
        )
    else:
        if not emp_id:
            return redirect(f"/letters/generate?slug={slug}&target_type=employee", err="Please select an employee.")
        emp = employees_service.require_employee(storage, emp_id)
        ref_no = f"FITS/HR/{slug.upper()}/{cur_year}/{emp['code']}"
        filename = f"{emp['code']}_{slug}_{date.today().isoformat()}.pdf"

        pdf_bytes = letters_service.render_letter_pdf(
            org_name=org_name,
            ref_no=ref_no,
            letter_date=letter_date,
            subject=subject,
            body_text=body_text,
        )

        letters_service.record_generated_letter(
            storage,
            employee_id=emp_id,
            template_slug=slug,
            title=title,
            subject=subject,
            body_text=body_text,
            pdf_filename=filename,
            created_by=user.username,
        )

        storage.audit(
            actor=user.username,
            action="generate_letter",
            entity="letter",
            entity_id=emp_id,
            detail=f"{title} generated for {emp['name']} ({emp['code']})",
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{letter_id}/pdf")
def download_letter_by_id(
    letter_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    letter = letters_service.get_generated_letter(storage, letter_id)
    if not letter:
        return redirect("/letters", err="Generated letter not found.")
    
    org_name = storage.get_setting("org_name", settings.org_name) or "Fakhri IT Services"
    cur_year = (letter.get("created_at") or "")[:4] or str(date.today().year)
    slug = letter.get("template_slug") or "letter"
    code = letter.get("employee_code") or "CAND"
    ref_no = f"FITS/HR/{slug.upper()}/{cur_year}/{code}"
    letter_date = (letter.get("created_at") or "")[:10] or date.today().strftime("%d %b %Y")

    pdf_bytes = letters_service.render_letter_pdf(
        org_name=org_name,
        ref_no=ref_no,
        letter_date=letter_date,
        subject=letter.get("subject") or "",
        body_text=letter.get("body_text") or "",
    )
    filename = letter.get("pdf_filename") or f"Letter_{letter_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/templates")
def templates_page(
    request: Request,
    slug: str | None = None,
    tab: str = "letters",
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    storage._seed_letter_templates()
    templates = letters_service.list_templates(storage)
    selected_slug = slug or (templates[0]["slug"] if templates else "offer")
    selected_template = next((t for t in templates if t["slug"] == selected_slug), (templates[0] if templates else None))

    payslip_settings = {
        "subtitle": storage.get_setting("payslip_subtitle", "Salary slip - computer generated") or "Salary slip - computer generated",
        "footer_note": storage.get_setting(
            "payslip_footer_note",
            "Net pay = earned gross + bonus - total deductions. This is a computer-generated payslip from the HRMS; errors and omissions excepted."
        ) or "Net pay = earned gross + bonus - total deductions. This is a computer-generated payslip from the HRMS; errors and omissions excepted.",
        "brand_color": storage.get_setting("payslip_brand_color", "#870808") or "#870808",
        "show_bank": storage.get_setting("payslip_show_bank", "1") != "0",
        "show_attendance": storage.get_setting("payslip_show_attendance", "1") != "0",
    }

    return render(
        request,
        "letters_templates.html",
        active="letters",
        user=user,
        templates=templates,
        selected_slug=selected_slug,
        selected_template=selected_template,
        payslip_settings=payslip_settings,
        active_tab=tab,
        standard_slugs=letters_service.STANDARD_SLUGS,
    )


@router.post("/templates/new")
def create_template_action(
    request: Request,
    slug: str = Form(...),
    title: str = Form(...),
    subject: str = Form(""),
    body_text: str = Form(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    try:
        letters_service.create_template(storage, slug, title, subject, body_text)
    except Exception as exc:
        return redirect("/letters/templates?tab=letters", err=str(exc))

    storage.audit(
        actor=user.username,
        action="create_letter_template",
        entity="letter_template",
        entity_id=0,
        detail=f"Template '{slug}' ({title}) created by {user.username}",
    )
    return redirect(f"/letters/templates?slug={slug}&tab=letters", msg=f"Template '{title}' created successfully.")


@router.post("/templates/{slug}")
def update_template_action(
    request: Request,
    slug: str,
    title: str = Form(...),
    subject: str = Form(""),
    body_text: str = Form(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    letters_service.update_template(storage, slug, title, subject, body_text)
    storage.audit(
        actor=user.username,
        action="update_letter_template",
        entity="letter_template",
        entity_id=0,
        detail=f"Template '{slug}' updated by {user.username}",
    )
    return redirect(f"/letters/templates?slug={slug}&tab=letters", msg=f"Template '{title}' updated successfully.")


@router.post("/templates/{slug}/delete")
def delete_template_action(
    request: Request,
    slug: str,
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    if not letters_service.delete_template(storage, slug):
        return redirect("/letters/templates?tab=letters", err=f"Standard template '{slug}' cannot be deleted.")

    storage.audit(
        actor=user.username,
        action="delete_letter_template",
        entity="letter_template",
        entity_id=0,
        detail=f"Template '{slug}' deleted by {user.username}",
    )
    return redirect("/letters/templates?tab=letters", msg=f"Template '{slug}' deleted successfully.")


@router.post("/templates/payslip/update")
def update_payslip_template_action(
    request: Request,
    payslip_subtitle: str = Form("Salary slip - computer generated"),
    payslip_brand_color: str = Form("#870808"),
    payslip_show_bank: str = Form("0"),
    payslip_show_attendance: str = Form("0"),
    payslip_footer_note: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    verify_form_csrf(user, csrf)
    storage.set_setting("payslip_subtitle", payslip_subtitle.strip())
    storage.set_setting("payslip_brand_color", payslip_brand_color.strip() or "#870808")
    storage.set_setting("payslip_show_bank", "1" if payslip_show_bank == "1" else "0")
    storage.set_setting("payslip_show_attendance", "1" if payslip_show_attendance == "1" else "0")
    storage.set_setting("payslip_footer_note", payslip_footer_note.strip())

    storage.audit(
        actor=user.username,
        action="update_payslip_template",
        entity="payslip_template",
        entity_id=0,
        detail=f"Payslip format & color ({payslip_brand_color}) updated by {user.username}",
    )
    return redirect("/letters/templates?tab=payslip", msg="Payslip template & format saved successfully.")
