"""Employee records: onboarding, salary structure and face enrolment."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..db import Storage, utcnow_iso
from ..face import EnrolmentResult, FaceService, FaceServiceError
from . import orgtime

logger = logging.getLogger("hrms.employees")

EMPLOYMENT_TYPES = ("full_time", "part_time", "intern", "contract")
EMPLOYMENT_LABELS = {
    "full_time": "Full time",
    "part_time": "Part time",
    "intern": "Intern",
    "contract": "Contract",
}

EDITABLE_FIELDS = (
    "code", "name", "email", "phone", "department", "designation",
    "joined_on", "employment_type", "status", "notes",
)

#: Personal / bank fields an employee may request to change (approved by HR).
PROFILE_FIELDS = (
    "email", "phone", "address", "bank_name", "account_holder",
    "account_number", "ifsc", "pan", "aadhaar_last4", "emergency_contact",
)


class ValidationError(ValueError):
    """User-facing validation failure (shown as a form error)."""


def next_employee_code(storage: Storage, prefix: str = "FITS") -> str:
    """Suggest the next sequential employee code."""
    rows = storage.query("SELECT code FROM employees WHERE code LIKE ?", (f"{prefix}%",))
    highest = 0
    for row in rows:
        digits = "".join(ch for ch in row["code"][len(prefix):] if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return f"{prefix}{highest + 1:03d}"


# ------------------------------------------------------------------ queries
def _employee_filters(q: str | None, department: str | None, status: str) -> tuple[str, list[Any]]:
    """Shared WHERE clause, so a list and its count always agree."""
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if department:
        clauses.append("department = ? COLLATE NOCASE")
        params.append(department.strip())
    if q:
        clauses.append("(name LIKE ? OR code LIKE ? OR email LIKE ? OR phone LIKE ? OR designation LIKE ?)")
        like = f"%{q}%"
        params.extend([like] * 5)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def list_employees(
    storage: Storage,
    *,
    q: str | None = None,
    department: str | None = None,
    status: str = "active",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _employee_filters(q, department, status)
    params.extend([limit, offset])
    return storage.query(
        f"SELECT * FROM employees {where} ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?", params
    )


def count_employees(
    storage: Storage, *, status: str = "active", q: str | None = None, department: str | None = None
) -> int:
    where, params = _employee_filters(q, department, status)
    return int(storage.scalar(f"SELECT COUNT(*) AS n FROM employees {where}", params))


def get_employee(storage: Storage, employee_id: int) -> dict[str, Any] | None:
    return storage.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


def face_key_for(employee_id: int) -> str:
    return f"emp-{employee_id}"


def get_employee_by_face_key(storage: Storage, external_id: str) -> dict[str, Any] | None:
    """Resolve a face-collection id: ``emp-<id>`` today, the employee code for older enrolments."""
    if external_id.startswith("emp-") and external_id[4:].isdigit():
        return get_employee(storage, int(external_id[4:]))
    return get_employee_by_code(storage, external_id)


def get_employee_by_code(storage: Storage, code: str) -> dict[str, Any] | None:
    return storage.query_one("SELECT * FROM employees WHERE code = ?", (code,))


def require_employee(storage: Storage, employee_id: int) -> dict[str, Any]:
    employee = get_employee(storage, employee_id)
    if employee is None:
        raise ValidationError(f"Employee #{employee_id} not found.")
    return employee


def normalise_department(storage: Storage, value: str | None) -> str | None:
    """``'  sales '`` → ``'Sales'`` when "Sales" already exists (ATT-04)."""
    text = " ".join((value or "").split())
    if not text:
        return None
    existing = storage.query_one(
        "SELECT department FROM employees WHERE department = ? COLLATE NOCASE "
        "GROUP BY department ORDER BY COUNT(*) DESC LIMIT 1",
        (text,),
    )
    return existing["department"] if existing else text


def departments(storage: Storage) -> list[str]:
    rows = storage.query(
        "SELECT DISTINCT department FROM employees WHERE department IS NOT NULL AND department != '' "
        "ORDER BY department COLLATE NOCASE"
    )
    return [row["department"] for row in rows]


def stats(storage: Storage) -> dict[str, Any]:
    total = int(storage.scalar("SELECT COUNT(*) AS n FROM employees"))
    active = int(storage.scalar("SELECT COUNT(*) AS n FROM employees WHERE status = 'active'"))
    enrolled = int(storage.scalar("SELECT COUNT(*) AS n FROM employees WHERE face_id IS NOT NULL"))
    payroll = float(storage.scalar("SELECT COALESCE(SUM(monthly_gross),0) AS n FROM employees WHERE status = 'active'"))
    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "face_enrolled": enrolled,
        "missing_face": active - enrolled,
        "monthly_payroll": payroll,
    }


# ------------------------------------------------------- identity & bank (C1)
import re as _re

_PAN = _re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_IFSC = _re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

# Verhoeff tables (the checksum Aadhaar numbers carry)
_V_D = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
        [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
        [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_V_P = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
        [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]


def verhoeff_ok(number: str) -> bool:
    check = 0
    for index, digit in enumerate(reversed(number)):
        check = _V_D[check][_V_P[index % 8][int(digit)]]
    return check == 0


def clean_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise PAN, Aadhaar, IFSC and account number in ``data`` (in place).

    Aadhaar: the full 12-digit number is checked (length + Verhoeff checksum) and
    then **only the last four digits** are kept — the full number is never stored
    (UIDAI rules); the card itself can be uploaded as a KYC document.
    """
    if data.get("pan"):
        pan = str(data["pan"]).strip().upper().replace(" ", "")
        if not _PAN.match(pan):
            raise ValidationError("PAN must look like ABCDE1234F.")
        data["pan"] = pan
    if data.get("ifsc"):
        ifsc = str(data["ifsc"]).strip().upper().replace(" ", "")
        if not _IFSC.match(ifsc):
            raise ValidationError("IFSC must look like HDFC0001234 (4 letters, 0, then 6 letters/digits).")
        data["ifsc"] = ifsc
    if data.get("account_number"):
        account = str(data["account_number"]).replace(" ", "").replace("-", "")
        if not account.isdigit() or not 9 <= len(account) <= 18:
            raise ValidationError("Bank account number must be 9–18 digits.")
        data["account_number"] = account
    aadhaar = str(data.pop("aadhaar", "") or "").replace(" ", "").replace("-", "")
    if aadhaar:
        if not aadhaar.isdigit() or len(aadhaar) != 12 or aadhaar[0] in "01" or not verhoeff_ok(aadhaar):
            raise ValidationError("That Aadhaar number is not valid — check the 12 digits.")
        data["aadhaar_last4"] = aadhaar[-4:]
    return data


def masked_aadhaar(last4: str | None) -> str:
    return f"XXXX-XXXX-{last4}" if last4 else ""


# ------------------------------------------------------------------ mutation
def _clean_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise ValidationError(f"'{value}' is not a valid date (expected YYYY-MM-DD).") from exc


def create_employee(storage: Storage, data: dict[str, Any]) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValidationError("Employee name is required.")

    code = (data.get("code") or "").strip().upper() or next_employee_code(storage)
    if get_employee_by_code(storage, code):
        raise ValidationError(f"Employee code '{code}' is already used.")

    employment_type = data.get("employment_type") or "full_time"
    if employment_type not in EMPLOYMENT_TYPES:
        raise ValidationError(f"Unknown employment type '{employment_type}'.")

    gross = float(data.get("monthly_gross") or 0)
    if gross < 0:
        raise ValidationError("Monthly salary cannot be negative.")
    clean_identity(data)

    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO employees (code, name, email, phone, department, designation, joined_on,
                               employment_type, status, monthly_gross, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            code, name, (data.get("email") or "").strip() or None, (data.get("phone") or "").strip() or None,
            normalise_department(storage, data.get("department")), (data.get("designation") or "").strip() or None,
            _clean_date(data.get("joined_on")), employment_type,
            data.get("status") or "active", gross, (data.get("notes") or "").strip() or None, now, now,
        ),
    )
    employee_id = int(cursor.lastrowid)

    extra = {field: data[field] for field in PROFILE_FIELDS
             if field not in ("email", "phone") and data.get(field)}
    if extra:
        update_employee(storage, employee_id, extra)

    if gross > 0:
        set_salary(
            storage,
            employee_id,
            monthly_gross=gross,
            basic=float(data.get("basic") or 0),
            hra=float(data.get("hra") or 0),
            other_allowances=float(data.get("other_allowances") or 0),
            pf=float(data.get("pf") or 0),
            esi=float(data.get("esi") or 0),
            professional_tax=float(data.get("professional_tax") or 0),
            effective_from=data.get("joined_on") or orgtime.today_str(storage.settings),
            note="Initial salary on joining",
            actor=data.get("_actor"),
            statutory_mode=data.get("statutory_mode") or "fixed",
        )
    return employee_id


def update_employee(storage: Storage, employee_id: int, data: dict[str, Any]) -> None:
    employee = require_employee(storage, employee_id)
    data = clean_identity(dict(data))
    updates: dict[str, Any] = {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValidationError("Employee name is required.")
        updates["name"] = name

    if "code" in data:
        code = (data.get("code") or "").strip().upper()
        if not code:
            raise ValidationError("Employee code is required.")
        clash = get_employee_by_code(storage, code)
        if clash and clash["id"] != employee_id:
            raise ValidationError(f"Employee code '{code}' is already used.")
        if code != employee["code"] and employee.get("face_id") and not employee.get("face_key"):
            raise ValidationError(
                "This person's face was enrolled under the old code. Remove the face enrolment (or re-enrol it) "
                "before changing the code, otherwise the gate scanner can no longer find them."
            )
        updates["code"] = code

    if "employment_type" in data and data["employment_type"] not in EMPLOYMENT_TYPES:
        raise ValidationError(f"Unknown employment type '{data['employment_type']}'.")
    if "status" in data and data["status"] not in ("active", "inactive"):
        raise ValidationError("Status must be active or inactive.")

    text_fields = ("department", "designation", "employment_type", "status", "notes") + PROFILE_FIELDS
    for field in text_fields:
        if field in data:
            value = data[field]
            text = (value or "").strip() or None if isinstance(value, str) or value is None else value
            if field == "ifsc" and isinstance(text, str):
                text = text.upper()
            if field == "department":
                text = normalise_department(storage, text)
            updates[field] = text

    if "joined_on" in data:
        updates["joined_on"] = _clean_date(data.get("joined_on"))

    if not updates:
        return

    updates["updated_at"] = utcnow_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    storage.execute(
        f"UPDATE employees SET {assignments} WHERE id = ?", (*updates.values(), employee_id)
    )

    if updates.get("name") or updates.get("code"):
        storage.audit(
            actor=data.get("_actor"), action="update_employee", entity="employee", entity_id=employee_id,
            detail=f"{employee['code']} → {updates.get('code', employee['code'])}",
        )


def set_status(storage: Storage, employee_id: int, status: str, *, actor: str | None = None) -> None:
    if status not in ("active", "inactive"):
        raise ValidationError("Status must be active or inactive.")
    storage.execute(
        "UPDATE employees SET status = ?, updated_at = ? WHERE id = ?",
        (status, utcnow_iso(), employee_id),
    )
    if status == "inactive":
        # their portal login goes too, and any open session ends now (SEC-05);
        # reactivating leaves it off — HR re-enables it from the employee page
        storage.execute(
            "UPDATE users SET active = 0, session_version = session_version + 1 WHERE employee_id = ?",
            (employee_id,),
        )
    storage.audit(actor=actor, action="set_employee_status", entity="employee", entity_id=employee_id, detail=status)


def delete_employee(storage: Storage, employee_id: int, *, actor: str | None = None,
                    settings: Any = None, face_service: Any = None) -> None:
    """Delete an employee and everything attached to them — rows, files and face template.

    Refused once the person is on a finalised or paid payslip: those are statutory
    records, so mark the employee inactive instead (EMP-03).
    """
    from ..uploads import delete_file

    employee = require_employee(storage, employee_id)
    on_record = storage.query_one(
        "SELECT p.period FROM payslips p JOIN payroll_runs r ON r.id = p.run_id "
        "WHERE p.employee_id = ? AND r.status IN ('finalized','paid') LIMIT 1",
        (employee_id,),
    )
    if on_record:
        raise ValidationError(
            f"{employee['name']} is on a finalised payslip ({on_record['period']}) — those are statutory records. "
            "Mark the employee inactive instead of deleting."
        )

    files = []
    if settings is not None:
        files += [("documents", r["stored_name"]) for r in storage.query(
            "SELECT stored_name FROM documents WHERE employee_id = ?", (employee_id,))]
        files += [("receipts", r["receipt_name"]) for r in storage.query(
            "SELECT receipt_name FROM expense_claims WHERE employee_id = ? AND receipt_name IS NOT NULL", (employee_id,))]
        files += [("selfies", r["selfie_name"]) for r in storage.query(
            "SELECT selfie_name FROM field_checks WHERE employee_id = ? AND selfie_name IS NOT NULL", (employee_id,))]
        files += [("proofs", r["proof_name"]) for r in storage.query(
            "SELECT proof_name FROM tax_declarations WHERE employee_id = ? AND proof_name IS NOT NULL", (employee_id,))]

    for table in ("attendance", "leave_requests", "salary_revisions", "payslips"):
        storage.execute(f"DELETE FROM {table} WHERE employee_id = ?", (employee_id,))
    storage.execute("DELETE FROM employees WHERE id = ?", (employee_id,))  # cascades the rest

    for kind, name in files:
        delete_file(settings, kind, name)
    if face_service is not None and employee.get("face_id"):
        try:
            face_service.delete_face(employee["face_id"])
        except Exception:  # noqa: BLE001 - best effort: the row is gone either way
            logger.warning("could not delete face template %s for deleted employee %s",
                           employee["face_id"], employee_id)
    storage.audit(actor=actor, action="delete_employee", entity="employee", entity_id=employee_id,
                  detail=f"{employee['code']} {employee['name']}")


# ------------------------------------------------------------------- salary
def set_salary(
    storage: Storage,
    employee_id: int,
    *,
    monthly_gross: float,
    basic: float = 0.0,
    hra: float = 0.0,
    other_allowances: float = 0.0,
    pf: float = 0.0,
    esi: float = 0.0,
    professional_tax: float = 0.0,
    effective_from: str | None = None,
    note: str | None = None,
    actor: str | None = None,
    statutory_mode: str = "fixed",
) -> dict[str, Any]:
    require_employee(storage, employee_id)
    if monthly_gross < 0:
        raise ValidationError("Monthly salary cannot be negative.")
    if statutory_mode not in ("fixed", "auto"):
        raise ValidationError("PF/ESI mode must be 'fixed' or 'auto'.")
    effective_from = _clean_date(effective_from) or orgtime.today_str(storage.settings)
    now = utcnow_iso()

    storage.execute(
        """
        INSERT INTO salary_revisions (employee_id, monthly_gross, basic, hra, other_allowances, pf, esi,
                                      professional_tax, effective_from, note, created_by, created_at,
                                      statutory_mode)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, monthly_gross, basic, hra, other_allowances, pf, esi, professional_tax,
         effective_from, note, None, now, statutory_mode),
    )
    storage.execute(
        "UPDATE employees SET monthly_gross = ?, updated_at = ? WHERE id = ?",
        (monthly_gross, now, employee_id),
    )
    storage.audit(
        actor=actor, action="set_salary", entity="employee", entity_id=employee_id,
        detail=f"gross={monthly_gross:.2f} from {effective_from}",
    )
    revision = storage.query_one(
        "SELECT * FROM salary_revisions WHERE employee_id = ? ORDER BY id DESC LIMIT 1", (employee_id,)
    )
    assert revision is not None
    return revision


def salary_history(storage: Storage, employee_id: int) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT * FROM salary_revisions WHERE employee_id = ? ORDER BY effective_from DESC, id DESC",
        (employee_id,),
    )


# -------------------------------------------------------------- face enrolment
def enrol_face(
    storage: Storage,
    face_service: FaceService,
    employee_id: int,
    image_bytes: bytes,
    *,
    actor: str | None = None,
) -> EnrolmentResult:
    """Register (or re-register) an employee's face in the Face API collection."""
    employee = require_employee(storage, employee_id)
    if employee.get("face_id"):
        # Re-enrolment: drop the old template first so one person cannot match twice.
        try:
            face_service.delete_face(employee["face_id"])
        except FaceServiceError:  # pragma: no cover - best effort cleanup
            pass

    # The template is keyed by the row id, not the editable employee code, so a
    # code change can never orphan it (EMP-01).
    key = face_key_for(employee_id)
    result = face_service.enrol_face(image_bytes, external_id=key)
    storage.execute(
        "UPDATE employees SET face_id = ?, face_key = ?, face_enrolled_at = ?, updated_at = ? WHERE id = ?",
        (result.face_id, key, utcnow_iso(), utcnow_iso(), employee_id),
    )
    storage.audit(actor=actor, action="enrol_face", entity="employee", entity_id=employee_id,
                  detail=f"face_id={result.face_id} score={result.det_score}")
    return result


def unenrol_face(
    storage: Storage, face_service: FaceService, employee_id: int, *, actor: str | None = None
) -> bool:
    employee = require_employee(storage, employee_id)
    removed = False
    if employee.get("face_id"):
        removed = face_service.delete_face(employee["face_id"])
    storage.execute(
        "UPDATE employees SET face_id = NULL, face_key = NULL, face_enrolled_at = NULL, updated_at = ? WHERE id = ?",
        (utcnow_iso(), employee_id),
    )
    storage.audit(actor=actor, action="unenrol_face", entity="employee", entity_id=employee_id)
    return removed


def employees_missing_face(storage: Storage) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT id, code, name, department FROM employees "
        "WHERE status = 'active' AND (face_id IS NULL OR face_id = '') ORDER BY name COLLATE NOCASE"
    )
