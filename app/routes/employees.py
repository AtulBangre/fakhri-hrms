"""Employee management: onboarding, editing, salary structure and face enrolment."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from .. import auth
from ..auth import SessionUser, generate_password, get_user_for_employee, require_hr
from ..deps import get_face, get_settings_dep, get_storage, paginate, redirect, render
from ..face import FaceServiceError, FaceServiceUnavailable
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import leave as leave_service
from ..services import appraisal as appraisal_service
from ..services import exits as exits_service
from ..services import orgtime

router = APIRouter(prefix="/employees", tags=["employees"])

ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/bmp")
MAX_FACE_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
PER_PAGE = 50


def _as_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@router.get("")
def list_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    q: str | None = None,
    department: str | None = None,
    status: str = "active",
    page: int = 1,
):
    pager = paginate(
        employees_service.count_employees(storage, q=q, department=department, status=status), page, PER_PAGE
    )
    people = employees_service.list_employees(
        storage, q=q, department=department, status=status, limit=PER_PAGE, offset=pager["offset"]
    )
    return render(
        request,
        "employees.html",
        active="employees",
        user=user,
        employees=people,
        stats=employees_service.stats(storage),
        departments=employees_service.departments(storage),
        filters={"q": q or "", "department": department or "", "status": status},
        pager=pager,
        missing_face={row["id"] for row in employees_service.employees_missing_face(storage)},
    )


@router.get("/new")
def new_page(request: Request, user: SessionUser = Depends(require_hr), storage=Depends(get_storage)):
    return render(
        request,
        "employee_form.html",
        active="employees",
        user=user,
        employee=None,
        suggested_code=employees_service.next_employee_code(storage),
        departments=employees_service.departments(storage),
        employment_types=employees_service.EMPLOYMENT_TYPES,
        employment_labels=employees_service.EMPLOYMENT_LABELS,
        form={},
    )


@router.post("")
def create(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(...),
    code: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    department: str = Form(""),
    designation: str = Form(""),
    joined_on: str = Form(""),
    employment_type: str = Form("full_time"),
    monthly_gross: str = Form("0"),
    basic: str = Form("0"),
    hra: str = Form("0"),
    other_allowances: str = Form("0"),
    pf: str = Form("0"),
    esi: str = Form("0"),
    professional_tax: str = Form("0"),
    statutory_mode: str = Form("auto"),
    notes: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    data = {
        "name": name, "code": code, "email": email, "phone": phone, "department": department,
        "designation": designation, "joined_on": joined_on or None, "employment_type": employment_type,
        "monthly_gross": _as_float(monthly_gross), "basic": _as_float(basic), "hra": _as_float(hra),
        "other_allowances": _as_float(other_allowances), "pf": _as_float(pf), "esi": _as_float(esi),
        "professional_tax": _as_float(professional_tax), "notes": notes, "_actor": user.username,
        "statutory_mode": statutory_mode,
    }
    try:
        employee_id = employees_service.create_employee(storage, data)
    except employees_service.ValidationError as exc:
        return render(
            request, "employee_form.html", active="employees", user=user, employee=None,
            form=data, error=str(exc), suggested_code=code or employees_service.next_employee_code(storage),
            departments=employees_service.departments(storage),
            employment_types=employees_service.EMPLOYMENT_TYPES,
            employment_labels=employees_service.EMPLOYMENT_LABELS,
            status_code=422,
        )
    storage.audit(actor=user.username, action="create_employee", entity="employee", entity_id=employee_id,
                  detail=name)
    return redirect(f"/employees/{employee_id}", ok=f"{name} added. Now enrol their face for kiosk attendance.")


@router.get("/{employee_id}/edit")
def edit_page(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    employee = employees_service.get_employee(storage, employee_id)
    if employee is None:
        return redirect("/employees", err="Employee not found.")
    return render(
        request,
        "employee_form.html",
        active="employees",
        user=user,
        employee=employee,
        suggested_code=employee["code"],
        departments=employees_service.departments(storage),
        employment_types=employees_service.EMPLOYMENT_TYPES,
        employment_labels=employees_service.EMPLOYMENT_LABELS,
        form={},
    )


@router.get("/{employee_id}")
def detail(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    period: str | None = None,
):
    employee = employees_service.get_employee(storage, employee_id)
    if employee is None:
        return redirect("/employees", err="Employee not found.")

    today = orgtime.today(settings)
    period = period or orgtime.period_of(today)
    summary = attendance_service.employee_month_summary(storage, settings, employee_id, period)
    history = employees_service.salary_history(storage, employee_id)
    latest = history[0] if history else {}
    salary_defaults = {
        "basic": latest.get("basic", 0) or 0,
        "hra": latest.get("hra", 0) or 0,
        "other_allowances": latest.get("other_allowances", 0) or 0,
        "pf": latest.get("pf", 0) or 0,
        "esi": latest.get("esi", 0) or 0,
        "professional_tax": latest.get("professional_tax", 0) or 0,
        "statutory_mode": latest.get("statutory_mode") or "fixed",
    }
    return render(
        request,
        "employee_detail.html",
        active="employees",
        user=user,
        employee=employee,
        period=period,
        period_label=orgtime.month_label(period),
        summary=summary,
        balance=leave_service.balance(storage, settings, employee_id),
        salaries=history,
        salary_defaults=salary_defaults,
        leaves=leave_service.list_leave(storage, employee_id=employee_id, limit=25),
        exit_record=exits_service.latest_for(storage, employee_id),
        appraisals=appraisal_service.for_employee(storage, employee_id)[:1],
        today_row=attendance_service.get_row(storage, employee_id, today),
        leave_types=orgtime.ALL_LEAVE_TYPES,
        leave_labels=orgtime.LEAVE_LABELS,

        all_statuses=orgtime.ALL_STATUSES,
        ess_user=auth.get_user_for_employee(storage, employee_id),
        suggested_password=auth.generate_password(),
    )


@router.post("/{employee_id}")
def update(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(...),
    code: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    department: str = Form(""),
    designation: str = Form(""),
    joined_on: str = Form(""),
    employment_type: str = Form("full_time"),
    status: str = Form("active"),
    notes: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.update_employee(
            storage,
            employee_id,
            {
                "name": name, "code": code, "email": email, "phone": phone, "department": department,
                "designation": designation, "joined_on": joined_on or None, "employment_type": employment_type,
                "status": status, "notes": notes, "_actor": user.username,
            },
        )
    except employees_service.ValidationError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    return redirect(f"/employees/{employee_id}", ok="Employee details updated.")


@router.post("/{employee_id}/salary")
def set_salary(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    monthly_gross: str = Form(...),
    basic: str = Form("0"),
    hra: str = Form("0"),
    other_allowances: str = Form("0"),
    pf: str = Form("0"),
    esi: str = Form("0"),
    professional_tax: str = Form("0"),
    statutory_mode: str = Form("fixed"),
    effective_from: str = Form(""),
    note: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.set_salary(
            storage, employee_id, monthly_gross=_as_float(monthly_gross), basic=_as_float(basic),
            hra=_as_float(hra), other_allowances=_as_float(other_allowances), pf=_as_float(pf),
            esi=_as_float(esi), professional_tax=_as_float(professional_tax),
            effective_from=effective_from or None, note=note or None, actor=user.username,
            statutory_mode=statutory_mode,
        )
    except employees_service.ValidationError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    return redirect(f"/employees/{employee_id}", ok="Salary updated — previous structure kept in history.")


@router.post("/{employee_id}/face")
def enrol_face(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    face=Depends(get_face),
    csrf: str = Form("", alias="_csrf"),
    image: UploadFile = File(...),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    payload = image.file.read(MAX_FACE_IMAGE_BYTES + 1)
    if not payload:
        return redirect(f"/employees/{employee_id}", err="Empty capture — try again in better light.")
    if len(payload) > MAX_FACE_IMAGE_BYTES:
        return redirect(f"/employees/{employee_id}", err=f"Image too large ({len(payload) / 1_048_576:.1f} MB); limit is 8 MB.")
    if image.content_type and image.content_type not in ALLOWED_IMAGE_TYPES:
        return redirect(f"/employees/{employee_id}", err=f"Unsupported image type {image.content_type}.")

    try:
        result = employees_service.enrol_face(
            storage, face, employee_id, payload, actor=user.username
        )
    except FaceServiceUnavailable as exc:
        return redirect(f"/employees/{employee_id}", err=f"Face service offline: {exc}")
    except FaceServiceError as exc:
        return redirect(f"/employees/{employee_id}", err=f"Enrolment failed: {exc.message}")

    score = f"{result.det_score:.2f}" if result.det_score is not None else "n/a"
    return redirect(f"/employees/{employee_id}", ok=f"Face enrolled (detection score {score}).")


@router.post("/{employee_id}/face/delete")
def remove_face(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    face=Depends(get_face),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.unenrol_face(storage, face, employee_id, actor=user.username)
    except FaceServiceUnavailable as exc:
        return redirect(f"/employees/{employee_id}", err=f"Face service offline: {exc}")
    return redirect(f"/employees/{employee_id}", ok="Face removed — this person can no longer scan.")


@router.post("/{employee_id}/status")
def toggle_status(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    status: str = Form(...),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.set_status(storage, employee_id, status, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    return redirect(f"/employees/{employee_id}", ok=f"Marked {status}.")


@router.post("/{employee_id}/delete")
def delete(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.delete_employee(storage, employee_id, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect("/employees", err=str(exc))
    return redirect("/employees", ok="Employee and all related records deleted.")


@router.get("/{employee_id}/payslips")
def payslips(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    employee = employees_service.get_employee(storage, employee_id)
    if employee is None:
        return redirect("/employees", err="Employee not found.")
    slips = storage.query(
        """
        SELECT p.*, r.status AS run_status FROM payslips p
        JOIN payroll_runs r ON r.id = p.run_id
        WHERE p.employee_id = ? ORDER BY p.period DESC
        """,
        (employee_id,),
    )
    return render(
        request, "employee_payslips.html", active="employees", user=user, employee=employee, slips=slips
    )


@router.post("/{employee_id}/portal")
def create_portal_login(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    password: str = Form(...),
):
    """Create (or reset) the employee's self-service login.

    HR sets or accepts the password in the form, so it is POSTed rather than put
    in a redirect URL — nothing sensitive lands in a query string or access log.
    """
    from ..auth import ensure_employee_login, verify_form_csrf

    verify_form_csrf(user, csrf)
    if len(password) < 8:
        return redirect(f"/employees/{employee_id}", err="Portal password must be at least 8 characters.")
    employee = employees_service.get_employee(storage, employee_id)
    if employee is None:
        return redirect("/employees", err="Employee not found.")
    try:
        portal_user, _ = ensure_employee_login(storage, employee, password=password)
    except ValueError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    storage.audit(
        actor=user.username, action="ess_login", entity="employee", entity_id=employee_id,
        detail=f"username={portal_user['username']}",
    )
    return redirect(
        f"/employees/{employee_id}",
        ok=f"Portal login ready: username '{portal_user['username']}'. Share the password with {employee['name'].split()[0]} privately.",
    )


@router.post("/{employee_id}/portal/disable")
def disable_portal_login(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import disable_employee_login, verify_form_csrf

    verify_form_csrf(user, csrf)
    disabled = disable_employee_login(storage, employee_id)
    storage.audit(actor=user.username, action="ess_login_disabled", entity="employee", entity_id=employee_id)
    if not disabled:
        return redirect(f"/employees/{employee_id}", err="This employee has no portal login yet.")
    return redirect(f"/employees/{employee_id}", ok="Portal login disabled — they can no longer sign in.")
