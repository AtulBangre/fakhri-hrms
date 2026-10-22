"""Leave: paid, sick, casual and unpaid (LOP), including half days and balances.

HR applies leave directly, so a request is created already approved. The service
also writes the matching attendance rows, which is what makes payroll correct
without a second source of truth: a day is either worked, on leave or absent.

A leave range never consumes weekly offs or holidays — those days are skipped and
reported back, so HR can see exactly which dates were charged.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from . import attendance as attendance_service
from . import employees as employees_service
from . import orgtime

STATUSES = ("pending", "approved", "rejected", "cancelled")


def apply_leave(
    storage: Storage,
    settings: Settings,
    *,
    employee_id: int,
    leave_type: str,
    from_date: str,
    to_date: str | None = None,
    days: float | None = None,
    half_day: bool = False,
    reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Record leave and stamp it onto the attendance calendar.

    Returns ``{"leave": row, "applied": [dates], "skipped": [{"date", "why"}]}``.
    """
    employee = employees_service.require_employee(storage, employee_id)
    if leave_type not in orgtime.ALL_LEAVE_TYPES:
        raise employees_service.ValidationError(f"Unknown leave type '{leave_type}'.")

    start = orgtime.parse_date(from_date)
    end = orgtime.parse_date(to_date or from_date)
    if end < start:
        raise employees_service.ValidationError("Leave end date cannot be before the start date.")

    if half_day:
        if start != end:
            raise employees_service.ValidationError("A half day applies to a single date.")
        days = 0.5
    else:
        chargeable = orgtime.working_days(storage, start, end)
        if chargeable == 0:
            raise employees_service.ValidationError(
                "That range contains no working days (weekly offs / holidays only)."
            )
        if days is None:
            days = chargeable

    days = float(days)
    if days <= 0:
        raise employees_service.ValidationError("Leave days must be greater than zero.")

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    targets = [start] if half_day else orgtime.each_day(start, end)

    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, reason, status,
                                    applied_by, created_at, decided_at)
        VALUES (?,?,?,?,?,?, 'approved', NULL, ?, ?)
        """,
        (employee_id, leave_type, start.isoformat(), end.isoformat(), days, reason, now, now),
    )
    leave_id = int(cursor.lastrowid)

    for day in targets:
        kind = orgtime.day_kind(storage, day)
        if kind != "working":
            skipped.append({"date": day.isoformat(), "why": "weekly off" if kind == orgtime.STATUS_WEEK_OFF else "holiday"})
            continue

        existing = attendance_service.get_row(storage, employee_id, day)
        if existing and existing.get("check_in") and existing.get("source") != "leave":
            skipped.append({"date": day.isoformat(), "why": "already has a check-in"})
            continue

        status = orgtime.STATUS_HALF_DAY if half_day else orgtime.STATUS_LEAVE
        if existing:
            storage.execute(
                """
                UPDATE attendance SET status = ?, leave_type = ?, source = 'leave', note = ?,
                                      check_in = NULL, check_out = NULL, worked_minutes = NULL,
                                      updated_at = ?
                WHERE id = ?
                """,
                (status, leave_type, f"leave #{leave_id}" + (f" — {reason}" if reason else ""), now, existing["id"]),
            )
        else:
            storage.execute(
                """
                INSERT INTO attendance (employee_id, work_date, status, leave_type, source, note,
                                        created_at, updated_at)
                VALUES (?,?,?,?, 'leave', ?,?,?)
                """,
                (employee_id, day.isoformat(), status, leave_type,
                 f"leave #{leave_id}" + (f" — {reason}" if reason else ""), now, now),
            )
        applied.append({"date": day.isoformat(), "status": status})

    storage.audit(
        actor=actor, action="apply_leave", entity="employee", entity_id=employee_id,
        detail=f"{leave_type} {start.isoformat()}..{end.isoformat()} days={days:g}",
    )
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert leave is not None
    leave["employee_name"] = employee["name"]
    leave["employee_code"] = employee["code"]
    return {"leave": leave, "applied": applied, "skipped": skipped}


def cancel_leave(storage: Storage, leave_id: int, *, actor: str | None = None) -> dict[str, Any]:
    """Cancel a leave record and remove the attendance rows it created."""
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if leave["status"] == "cancelled":
        return leave

    storage.execute(
        "DELETE FROM attendance WHERE employee_id = ? AND work_date BETWEEN ? AND ? "
        "AND source = 'leave' AND note LIKE ?",
        (leave["employee_id"], leave["from_date"], leave["to_date"], f"leave #{leave_id}%"),
    )
    storage.execute(
        "UPDATE leave_requests SET status = 'cancelled', decided_at = ? WHERE id = ?", (utcnow_iso(), leave_id)
    )
    storage.audit(actor=actor, action="cancel_leave", entity="employee", entity_id=leave["employee_id"],
                  detail=f"leave #{leave_id}")
    updated = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert updated is not None
    return updated


def list_leave(
    storage: Storage,
    *,
    employee_id: int | None = None,
    status: str | None = None,
    period: str | None = None,
    leave_type: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if employee_id:
        clauses.append("l.employee_id = ?")
        params.append(employee_id)
    if status and status != "all":
        clauses.append("l.status = ?")
        params.append(status)
    if leave_type and leave_type != "all":
        clauses.append("l.leave_type = ?")
        params.append(leave_type)
    if period:
        start, end = orgtime.month_bounds(period)
        clauses.append("l.from_date <= ? AND l.to_date >= ?")
        params.extend([end.isoformat(), start.isoformat()])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = storage.query(
        f"""
        SELECT l.*, e.name AS employee_name, e.code AS employee_code, e.department
        FROM leave_requests l JOIN employees e ON e.id = l.employee_id
        {where}
        ORDER BY l.from_date DESC, l.id DESC LIMIT ?
        """,
        params,
    )
    for row in rows:
        row["type_label"] = orgtime.LEAVE_LABELS.get(row["leave_type"], row["leave_type"])
        row["from_label"] = orgtime.human_date(row["from_date"])
        row["to_label"] = orgtime.human_date(row["to_date"])
        row["is_paid"] = row["leave_type"] in orgtime.PAID_LEAVE_TYPES
    return rows


def balance(storage: Storage, settings: Settings, employee_id: int, *, as_of: date | None = None) -> dict[str, Any]:
    """Accrued vs taken paid leave, counted from the joining date."""
    employee = employees_service.require_employee(storage, employee_id)
    as_of = as_of or orgtime.today(settings)
    accrual = storage.get_float("pl_accrual_per_month", 1.5)

    joined = orgtime.parse_date(employee["joined_on"]) if employee.get("joined_on") else as_of.replace(day=1)
    months = max(0, (as_of.year - joined.year) * 12 + (as_of.month - joined.month))
    if as_of.day >= joined.day:
        months += 1  # the current month counts once its joining day has passed
    accrued = round(months * accrual, 2)

    used_paid = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) AS n FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type IN ('paid','sick','casual')",
            (employee_id,),
        )
    )
    used_unpaid = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) AS n FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'unpaid'",
            (employee_id,),
        )
    )
    return {
        "accrual_per_month": accrual,
        "months_of_service": months,
        "accrued": accrued,
        "used_paid": round(used_paid, 2),
        "used_unpaid": round(used_unpaid, 2),
        "balance": round(accrued - used_paid, 2),
        "joined_on": employee.get("joined_on"),
        "as_of": as_of.isoformat(),
    }


def summary_for_period(storage: Storage, period: str) -> list[dict[str, Any]]:
    """Leave taken per employee for a month, split by type."""
    start, end = orgtime.month_bounds(period)
    return storage.query(
        """
        SELECT e.id AS employee_id, e.code, e.name, e.department,
               COALESCE(SUM(CASE WHEN l.leave_type = 'paid'   THEN l.days END), 0) AS paid,
               COALESCE(SUM(CASE WHEN l.leave_type = 'sick'   THEN l.days END), 0) AS sick,
               COALESCE(SUM(CASE WHEN l.leave_type = 'casual' THEN l.days END), 0) AS casual,
               COALESCE(SUM(CASE WHEN l.leave_type = 'unpaid' THEN l.days END), 0) AS unpaid,
               COALESCE(SUM(l.days), 0) AS total
        FROM leave_requests l JOIN employees e ON e.id = l.employee_id
        WHERE l.status = 'approved' AND l.from_date <= ? AND l.to_date >= ?
        GROUP BY e.id ORDER BY e.name COLLATE NOCASE
        """,
        (end.isoformat(), start.isoformat()),
    )
