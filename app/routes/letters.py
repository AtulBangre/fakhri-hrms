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
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    storage._seed_letter_templates()
    templates = letters_service.list_templates(storage)
    return render(
        request,
        "letters_templates.html",
        active="letters",
        user=user,
        templates=templates,
    )


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
    return redirect("/letters/templates", msg=f"Template '{title}' updated successfully.")
