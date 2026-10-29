"""Employee management: onboarding, editing, salary structure and face enrolment."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from .. import auth, notify
from ..auth import SessionUser, generate_password, get_user_for_employee, require_hr
from ..deps import get_face, get_settings_dep, get_storage, paginate, redirect, render
from ..face import FaceServiceError, FaceServiceUnavailable
from ..services import records
from ..uploads import UploadError, read_capped
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


@router.get("/chart")
def org_chart(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    rows = storage.query(
        "SELECT id, code, name, department, designation, manager_id, status FROM employees WHERE status = 'active' ORDER BY department, name"
    )
    dept_map: dict[str, list[dict]] = {}
    for r in rows:
        d = r["department"] or "General"
        dept_map.setdefault(d, []).append(dict(r))

    return render(
        request,
        "org_chart.html",
        active="employees",
        user=user,
        departments=dept_map,
        total_active=len(rows),
    )


@router.get("/bulk-update")
def bulk_update_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    department: str | None = None,
):
    where = "WHERE status = 'active'"
    params = []
    if department:
        where += " AND department = ?"
        params.append(department)
    people = storage.query(f"SELECT id, code, name, department, designation, shift_id FROM employees {where} ORDER BY name", params)
    shifts = storage.query("SELECT id, name, start_time, end_time FROM shifts ORDER BY name")
    return render(
        request,
        "employee_bulk_update.html",
        active="employees",
        user=user,
        employees=people,
        shifts=shifts,
        departments=employees_service.departments(storage),
        selected_dept=department or "",
    )


@router.post("/bulk-update")
def bulk_update_apply(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    employee_ids: list[int] = Form(...),
    new_department: str = Form(""),
    new_shift_id: str = Form(""),
):
    auth.verify_form_csrf(user, csrf)
    with storage.transaction():
        for emp_id in employee_ids:
            if new_department.strip():
                storage.execute_tx("UPDATE employees SET department = ? WHERE id = ?", (new_department.strip(), emp_id))
            if new_shift_id.strip():
                storage.execute_tx("UPDATE employees SET shift_id = ? WHERE id = ?", (int(new_shift_id), emp_id))
    storage.audit(actor=user.username, action="bulk_update_employees", detail=f"Updated {len(employee_ids)} employees")
    return redirect("/employees", ok=f"Successfully updated {len(employee_ids)} employees.")


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
        shifts=storage.query("SELECT id, name, start_time, end_time FROM shifts ORDER BY name"),
        form={},
    )


IDENTITY_FIELDS = ("address", "emergency_contact", "pan", "aadhaar", "bank_name", "account_holder",
                   "account_number", "ifsc")
KYC_UPLOADS = (("doc_aadhaar", "aadhaar"), ("doc_pan", "pan"), ("doc_resume", "resume"))


def _form_error(request, user, storage, data, message):
    return render(
        request, "employee_form.html", active="employees", user=user, employee=None,
        form=data, error=message, suggested_code=data.get("code") or employees_service.next_employee_code(storage),
        departments=employees_service.departments(storage),
        employment_types=employees_service.EMPLOYMENT_TYPES,
        employment_labels=employees_service.EMPLOYMENT_LABELS,
        shifts=storage.query("SELECT id, name, start_time, end_time FROM shifts ORDER BY name"),
        status_code=422,
    )


@router.post("")
async def create(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    """Onboard an employee: details, salary, optional KYC files and a portal login (C1–C3).

    The employee is saved first; a rejected document never loses the form — it is
    reported on the employee page instead.
    """
    from ..auth import verify_form_csrf

    form = await request.form()
    verify_form_csrf(user, str(form.get("_csrf") or ""))
    text = lambda key, default="": str(form.get(key) or default).strip()  # noqa: E731
    data = {
        "name": text("name"), "code": text("code"), "email": text("email"), "phone": text("phone"),
        "department": text("department"), "designation": text("designation"),
        "joined_on": text("joined_on") or None, "employment_type": text("employment_type", "full_time"),
        "monthly_gross": _as_float(text("monthly_gross")), "basic": _as_float(text("basic")),
        "hra": _as_float(text("hra")), "other_allowances": _as_float(text("other_allowances")),
        "pf": _as_float(text("pf")), "esi": _as_float(text("esi")),
        "professional_tax": _as_float(text("professional_tax")), "notes": text("notes"),
        "statutory_mode": text("statutory_mode", "auto"), "_actor": user.username,
        "shift_id": text("shift_id") or None,
        "custom_pl_accrual": text("custom_pl_accrual") or None,
        "custom_sl_quota": text("custom_sl_quota") or None,
        "custom_cl_quota": text("custom_cl_quota") or None,
        **{field: text(field) for field in IDENTITY_FIELDS},
        "skip_documents": text("skip_documents"), "create_login": text("create_login"),
        "send_welcome": text("send_welcome"), "portal_username": text("portal_username"),
    }
    missing = [label for key, label in (("name", "name"), ("code", "employee code"), ("joined_on", "date of joining"))
               if not data.get(key)]
    if data["monthly_gross"] <= 0:
        missing.append("monthly gross")
    if missing:
        return _form_error(request, user, storage, data, "Please fill in: " + ", ".join(missing) + ".")
    try:
        employee_id = employees_service.create_employee(storage, dict(data))
    except employees_service.ValidationError as exc:
        return _form_error(request, user, storage, data, str(exc))
    storage.audit(actor=user.username, action="create_employee", entity="employee", entity_id=employee_id,
                  detail=data["name"])
    employee = employees_service.get_employee(storage, employee_id)

    # KYC documents (C2)
    uploaded, failed = 0, []
    if not data["skip_documents"]:
        uploads = list(KYC_UPLOADS)
        extra_category = text("doc_extra_category", "photo")
        uploads.append(("doc_extra", extra_category if extra_category in ("photo", "offer_letter") else "other"))
        for field, category in uploads:
            item = form.get(field)
            if not getattr(item, "filename", ""):
                continue
            try:
                records.add_document(storage, settings, employee_id=employee_id, data=read_capped(item.file),
                                     original_name=item.filename, category=category, uploaded_by=user.username)
                uploaded += 1
            except (UploadError, ValueError) as exc:
                failed.append(f"{item.filename}: {exc}")

    # Portal login (C3)
    credentials = None
    login_note = ""
    should_create_login = data.get("create_login")
    if should_create_login is None or should_create_login in ("1", "true", "on"):
        try:
            portal_user, password = auth.ensure_employee_login(
                storage, employee, username=data.get("portal_username") or None)
            credentials = {
                "username": portal_user["username"],
                "password": password,
                "email": employee.get("email"),
                "phone": employee.get("phone"),
            }
            if employee.get("email") and data.get("send_welcome", "1") in ("1", "true", "on"):
                notify.queue_email(
                    storage, employee["email"], "welcome", settings=settings, category="welcome",
                    name=employee["name"].split(" ")[0],
                    code=employee.get("code") or "",
                    username=portal_user["username"],
                    password=password,
                    email=employee["email"],
                    phone=employee.get("phone"),
                )
                login_note = f" Login credentials (email, mobile & password) emailed to {employee['email']}."
            if employee.get("phone") and data.get("send_welcome", "1") in ("1", "true", "on"):
                try:
                    notify.queue_whatsapp(
                        storage, employee["phone"], "welcome", settings=settings,
                        name=employee["name"].split(" ")[0],
                        code=employee.get("code") or "",
                        username=portal_user["username"],
                        password=password,
                        email=employee.get("email"),
                        phone=employee["phone"],
                    )
                except Exception:
                    pass
        except ValueError as exc:
            failed.append(f"Portal login: {exc}")

    message = f"{employee['name']} added."
    if uploaded:
        message += f" {uploaded} document(s) uploaded."
    message += login_note
    if credentials and not employee.get("email"):
        # shown exactly once, in the response body — never in a URL or the log
        return render(request, "employee_created.html", active="employees", user=user, employee=employee,
                      credentials=credentials, message=message, failed=failed)
    if failed:
        return redirect(f"/employees/{employee_id}", ok=message,
                        err="Not saved: " + "; ".join(failed[:4]))
    return redirect(f"/employees/{employee_id}", ok=message + " Now enrol their face for gate attendance.")


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
        shifts=storage.query("SELECT id, name, start_time, end_time FROM shifts ORDER BY name"),
        form={},
        masked_aadhaar=employees_service.masked_aadhaar(employee.get("aadhaar_last4")),
        self_punch_labels=employees_service.SELF_PUNCH_LABELS,
        company_punch_mode=storage.get_setting("allow_employee_self_punch", "disabled"),
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
    parsed_period = orgtime.parse_period(period[:7] if period else None)
    period = parsed_period or orgtime.period_of(today)
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
        today=today,
        today_iso=today.isoformat(),
        today_kind=orgtime.day_kind(storage, today),
        today_row=attendance_service.get_row(storage, employee_id, today),
        attendance_days=attendance_service.employee_month_days(storage, settings, employee, period),
        shift_start=storage.get_setting("shift_start", "09:30"),
        shift_end=storage.get_setting("shift_end", "18:30"),
        leave_types=orgtime.ALL_LEAVE_TYPES,
        leave_labels=orgtime.LEAVE_LABELS,
        all_statuses=orgtime.ALL_STATUSES,
        status_labels=orgtime.STATUS_LABELS,
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
    address: str = Form(""),
    emergency_contact: str = Form(""),
    pan: str = Form(""),
    aadhaar: str = Form(""),
    bank_name: str = Form(""),
    account_holder: str = Form(""),
    account_number: str = Form(""),
    ifsc: str = Form(""),
    enable_portal: str = Form(""),
    self_punch_mode: str = Form(""),
    shift_id: str = Form(""),
    custom_pl_accrual: str = Form(""),
    custom_sl_quota: str = Form(""),
    custom_cl_quota: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    before = employees_service.get_employee(storage, employee_id)
    if before is None:
        return redirect("/employees", err="Employee not found.")
    data = {
        "name": name, "code": code, "email": email, "phone": phone, "department": department,
        "designation": designation, "joined_on": joined_on or None, "employment_type": employment_type,
        "notes": notes, "_actor": user.username, "address": address, "emergency_contact": emergency_contact,
        "pan": pan, "bank_name": bank_name, "account_holder": account_holder,
        "account_number": account_number, "ifsc": ifsc,
        "shift_id": int(shift_id) if shift_id.strip() else None,
        "custom_pl_accrual": float(custom_pl_accrual) if custom_pl_accrual.strip() else None,
        "custom_sl_quota": float(custom_sl_quota) if custom_sl_quota.strip() else None,
        "custom_cl_quota": float(custom_cl_quota) if custom_cl_quota.strip() else None,
    }
    if aadhaar.strip():  # empty means "keep what is on file"
        data["aadhaar"] = aadhaar
    if self_punch_mode:
        data["self_punch_mode"] = self_punch_mode
    try:
        employees_service.update_employee(storage, employee_id, data)
        if status != before["status"]:
            # through set_status, so going inactive also closes the portal login (SEC-05)
            employees_service.set_status(storage, employee_id, status, actor=user.username)
            if status == "active" and enable_portal:
                portal = auth.get_user_for_employee(storage, employee_id)
                if portal:
                    auth.set_user_active(storage, portal["id"], True)
    except employees_service.ValidationError as exc:
        return redirect(f"/employees/{employee_id}/edit", err=str(exc))
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
    except employees_service.DuplicateFaceError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
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
    enable_portal: str = Form(""),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.set_status(storage, employee_id, status, actor=user.username)
    except employees_service.ValidationError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    message = f"Marked {status}."
    if status == "inactive":
        message += " Their portal login is disabled."
    elif enable_portal:
        portal = auth.get_user_for_employee(storage, employee_id)
        if portal:
            auth.set_user_active(storage, portal["id"], True)
            message += " Portal login re-enabled."
    return redirect(f"/employees/{employee_id}", ok=message)


@router.post("/{employee_id}/delete")
def delete(
    request: Request,
    employee_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
    csrf: str = Form("", alias="_csrf"),
):
    from ..auth import verify_form_csrf

    verify_form_csrf(user, csrf)
    try:
        employees_service.delete_employee(storage, employee_id, actor=user.username,
                                          settings=settings, face_service=face)
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
    settings=Depends(get_settings_dep),
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
        delivery_note = ""
        if employee.get("email"):
            notify.queue_email(
                storage, employee["email"], "welcome", settings=settings, category="welcome",
                name=employee["name"].split(" ")[0],
                username=portal_user["username"],
                password=password,
                email=employee["email"],
                phone=employee.get("phone"),
            )
            delivery_note = f" Login details emailed to {employee['email']}."
        if employee.get("phone"):
            try:
                notify.queue_whatsapp(
                    storage, employee["phone"], "welcome", settings=settings,
                    name=employee["name"].split(" ")[0],
                    username=portal_user["username"],
                    password=password,
                    email=employee.get("email"),
                    phone=employee["phone"],
                )
            except Exception:
                pass
    except ValueError as exc:
        return redirect(f"/employees/{employee_id}", err=str(exc))
    storage.audit(
        actor=user.username, action="ess_login", entity="employee", entity_id=employee_id,
        detail=f"username={portal_user['username']}",
    )
    first_name = employee["name"].split()[0]
    tail = delivery_note or f" Share the password with {first_name} privately."
    return redirect(
        f"/employees/{employee_id}",
        ok=f"Portal login ready: username '{portal_user['username']}'.{tail}",
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
