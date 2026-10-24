"""Attendance regularization: an employee asks HR to correct a missed or wrong punch.

    employee  →  request (date, in, out, reason)        status = pending
    HR        →  approve  → attendance row is written   status = approved
              →  reject   → nothing changes             status = rejected
    employee  →  withdraw while still pending           status = withdrawn

Approval goes through ``attendance.mark_manual`` so late minutes, worked minutes
and the audit trail are computed exactly as for any other HR correction. A day
inside a finalised or paid payroll month cannot be changed — reopen the run first.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from .. import notify
from . import attendance as attendance_service
from . import employees as employees_service
from . import orgtime

ValidationError = employees_service.ValidationError

#: How far back an employee may ask for a correction.
MAX_AGE_DAYS = 60
REQUESTABLE_STATUSES = (orgtime.STATUS_PRESENT, orgtime.STATUS_HALF_DAY)


def _clock(value: str | None, label: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    parsed = orgtime.parse_clock(value)
    if parsed is None:
        raise ValidationError(f"'{value}' is not a valid {label} time.")
    return parsed.strftime("%H:%M:%S")


def _payroll_locked(storage: Storage, work_date: str) -> str | None:
    run = storage.query_one("SELECT status FROM payroll_runs WHERE period = ?", (work_date[:7],))
    return run["status"] if run and run["status"] != "draft" else None


def submit(
    storage: Storage,
    settings: Settings,
    *,
    employee_id: int,
    work_date: str,
    check_in: str | None,
    check_out: str | None,
    reason: str,
    requested_status: str = orgtime.STATUS_PRESENT,
    actor: str | None = None,
) -> dict[str, Any]:
    employees_service.require_employee(storage, employee_id)
    try:
        day = orgtime.parse_date(work_date)
    except ValueError as exc:
        raise ValidationError("Pick the date to correct.") from exc
    today = orgtime.today(settings)
    if day > today:
        raise ValidationError("You can only correct today or a past day.")
    if day < today - timedelta(days=MAX_AGE_DAYS):
        raise ValidationError(f"Corrections older than {MAX_AGE_DAYS} days must be raised with HR directly.")
    if requested_status not in REQUESTABLE_STATUSES:
        raise ValidationError("Request either a full day (present) or a half day.")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("Tell HR what happened (at least a few words).")
    check_in, check_out = _clock(check_in, "check-in"), _clock(check_out, "check-out")
    if not check_in:
        raise ValidationError("Enter the time you actually arrived.")
    if check_out and check_out <= check_in:
        raise ValidationError("Check-out must be after check-in.")
    locked = _payroll_locked(storage, day.isoformat())
    if locked:
        raise ValidationError(f"Payroll for {orgtime.month_label(day.isoformat()[:7])} is already {locked}. Please speak to HR.")
    if storage.query_one(
        "SELECT id FROM attendance_regularizations WHERE employee_id = ? AND work_date = ? AND status = 'pending'",
        (employee_id, day.isoformat()),
    ):
        raise ValidationError("You already have a pending request for that day — withdraw it first to change it.")

    current = attendance_service.get_row(storage, employee_id, day)
    cursor = storage.execute(
        """
        INSERT INTO attendance_regularizations (employee_id, work_date, requested_status, check_in, check_out,
                                                reason, previous_status, requested_by, requested_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, day.isoformat(), requested_status, check_in, check_out, reason[:500],
         current["status"] if current else None, actor, utcnow_iso()),
    )
    storage.audit(actor=actor, action="regularization_requested", entity="employee",
                  entity_id=employee_id, detail=day.isoformat())
    employee = employees_service.get_employee(storage, employee_id) or {}
    notify.notify_hr(
        storage, settings, "hr_alert", category="regularization",
        subject=f"Attendance correction: {employee.get('name')} · {orgtime.human_date(day)}",
        message=f"{employee.get('name')} ({employee.get('code')}) asks to correct {orgtime.human_date(day)}: "
                f"in {orgtime.format_clock(check_in)}, out {orgtime.format_clock(check_out)}. Reason: {reason}",
        link="/attendance/regularizations",
    )
    return get(storage, int(cursor.lastrowid))


def get(storage: Storage, request_id: int) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT r.*, e.name, e.code, e.department FROM attendance_regularizations r
        JOIN employees e ON e.id = r.employee_id WHERE r.id = ?
        """,
        (request_id,),
    )
    if row is None:
        raise ValidationError("Request not found.")
    return row


def list_requests(
    storage: Storage, *, status: str | None = "pending", employee_id: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status and status != "all":
        clauses.append("r.status = ?")
        params.append(status)
    if employee_id:
        clauses.append("r.employee_id = ?")
        params.append(employee_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return storage.query(
        f"""
        SELECT r.*, e.name, e.code, e.department FROM attendance_regularizations r
        JOIN employees e ON e.id = r.employee_id {where}
        ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.work_date DESC, r.id DESC LIMIT ?
        """,
        (*params, limit),
    )


def count_pending(storage: Storage) -> int:
    return int(storage.scalar("SELECT COUNT(*) AS n FROM attendance_regularizations WHERE status = 'pending'"))


def _require_pending(row: dict[str, Any]) -> None:
    if row["status"] != "pending":
        raise ValidationError(f"That request is already {row['status']}.")


def approve(storage: Storage, request_id: int, *, actor: str | None = None, note: str | None = None,
            release_leave: bool = False) -> dict[str, Any]:
    row = get(storage, request_id)
    _require_pending(row)
    locked = _payroll_locked(storage, row["work_date"])
    if locked:
        raise ValidationError(
            f"Payroll for {orgtime.month_label(row['work_date'][:7])} is {locked}. Reopen the run before approving."
        )
    attendance_service.mark_manual(
        storage, employee_id=row["employee_id"], work_date=row["work_date"], status=row["requested_status"],
        check_in=row["check_in"], check_out=row["check_out"], leave_type=None,
        note=f"Regularized: {row['reason'][:120]}", actor=actor, release_leave=release_leave,
    )
    storage.execute(
        "UPDATE attendance_regularizations SET status = 'approved', decided_by = ?, decided_at = ?, "
        "decision_note = ? WHERE id = ?",
        (actor, utcnow_iso(), note, request_id),
    )
    storage.audit(actor=actor, action="regularization_approved", entity="employee",
                  entity_id=row["employee_id"], detail=row["work_date"])
    return get(storage, request_id)


def reject(storage: Storage, request_id: int, *, actor: str | None = None, note: str | None = None) -> dict[str, Any]:
    row = get(storage, request_id)
    _require_pending(row)
    storage.execute(
        "UPDATE attendance_regularizations SET status = 'rejected', decided_by = ?, decided_at = ?, "
        "decision_note = ? WHERE id = ?",
        (actor, utcnow_iso(), note, request_id),
    )
    storage.audit(actor=actor, action="regularization_rejected", entity="employee",
                  entity_id=row["employee_id"], detail=row["work_date"])
    return get(storage, request_id)


def withdraw(storage: Storage, request_id: int, *, employee_id: int, actor: str | None = None) -> None:
    row = get(storage, request_id)
    if row["employee_id"] != employee_id:
        raise ValidationError("Request not found.")
    _require_pending(row)
    storage.execute("UPDATE attendance_regularizations SET status = 'withdrawn' WHERE id = ?", (request_id,))
    storage.audit(actor=actor, action="regularization_withdrawn", entity="employee",
                  entity_id=employee_id, detail=row["work_date"])
