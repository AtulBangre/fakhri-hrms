"""HR Letters router: Offer, Appointment, Increment, and Relieving Letters."""

from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, Form, Request, Response

from ..auth import SessionUser, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..services import employees as employees_service
from ..services import letters as letters_service

router = APIRouter(prefix="/letters", tags=["letters"])


@router.get("")
def letters_index(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    employee_id: int | None = None,
):
    storage._seed_letter_templates()
    templates = letters_service.list_templates(storage)
    history = letters_service.list_generated_letters(storage, employee_id=employee_id, limit=100)
    employees = employees_service.list_employees(storage, status="all", limit=500)
    return render(
        request,
        "letters.html",
        active="letters",
        user=user,
        templates=templates,
        history=history,
        employees=employees,
        selected_employee_id=employee_id,
    )


@router.get("/generate")
def generate_page(
    request: Request,
    slug: str = "offer",
    employee_id: int | None = None,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    storage._seed_letter_templates()
    template = letters_service.get_template(storage, slug)
    if not template:
        return redirect("/letters", err=f"Template '{slug}' not found.")
    
    employees = employees_service.list_employees(storage, status="all", limit=500)
    templates = letters_service.list_templates(storage)
    
    context = {}
    rendered_subject = template["subject"] or ""
    rendered_body = template["body_text"] or ""
    
    if employee_id:
        try:
            context = letters_service.build_employee_context(storage, employee_id)
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
        employee_id=employee_id,
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
        context=context,
    )


@router.post("/download")
def download_letter(
    request: Request,
    slug: str = Form(...),
    employee_id: int = Form(...),
    subject: str = Form(""),
    body_text: str = Form(...),
    csrf: str = Form("", alias="_csrf"),
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    verify_form_csrf(user, csrf)
    emp = employees_service.require_employee(storage, employee_id)
    org_name = storage.get_setting("org_name", settings.org_name) or "Fakhri IT Services"
    ref_no = f"FITS/HR/{slug.upper()}/{date.today().year}/{emp['code']}"
    letter_date = date.today().strftime("%d %b %Y")

    pdf_bytes = letters_service.render_letter_pdf(
        org_name=org_name,
        ref_no=ref_no,
        letter_date=letter_date,
        subject=subject,
        body_text=body_text,
    )

    filename = f"{emp['code']}_{slug}_{date.today().isoformat()}.pdf"
    template = letters_service.get_template(storage, slug)
    title = template["title"] if template else slug.title()

    letters_service.record_generated_letter(
        storage,
        employee_id=employee_id,
        template_slug=slug,
        title=title,
        pdf_filename=filename,
        created_by=user.username,
    )

    storage.audit(
        actor=user.username,
        action="generate_letter",
        entity="letter",
        entity_id=employee_id,
        detail=f"{title} generated for {emp['name']} ({emp['code']})",
    )

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
