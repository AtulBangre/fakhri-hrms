"""Leave: paid, sick, casual and unpaid (LOP), including half days and balances.

Two ways leave enters the system, both landing in the same table:

* **HR books it directly** → created already ``approved`` and stamped onto the
  attendance calendar immediately (HR is the approver).
* **An employee requests it in the portal** → created ``pending`` and *nothing*
  touches attendance until HR approves it. That ordering matters: an unapproved
  day must not quietly become "paid leave" in a payroll run.

Approving (or HR booking) writes the attendance rows, which is what makes payroll
correct without a second source of truth: a day is either worked, on leave or
absent. A range never consumes weekly offs or holidays — those days are skipped
and reported back so HR can see exactly which dates were charged.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from . import attendance as attendance_service
from . import employees as employees_service
from . import locks, orgtime

STATUSES = ("pending", "approved", "rejected", "cancelled")


# --------------------------------------------------------------- attendance
def stamp_attendance(
    storage: Storage, leave: dict[str, Any], *, actor: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Write one leave record onto the attendance calendar.

    Returns ``(applied, skipped)``. Weekly offs and holidays are skipped (never
    charged). A *full* day that already carries a real check-in is skipped too —
    HR should decide that case rather than have the system erase a worked day.
    A *half* day on a worked day is the normal case (worked one half, on leave the
    other), so it keeps the scan times and becomes a half day with the leave type.
    Every stamped row carries ``leave_id`` so cancelling touches only its own days.
    """
    employee_id = leave["employee_id"]
    leave_id = leave["id"]
    half_day = float(leave["days"]) == 0.5
    start = orgtime.parse_date(leave["from_date"])
    end = orgtime.parse_date(leave["to_date"])
    targets = [start] if half_day else orgtime.each_day(start, end)
    note = f"leave #{leave_id}" + (f" — {leave['reason']}" if leave.get("reason") else "")
    now = utcnow_iso()

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for day in targets:
        kind = orgtime.day_kind(storage, day)
        if kind != "working":
            skipped.append(
                {
                    "date": day.isoformat(),
                    "why": "weekly off" if kind == orgtime.STATUS_WEEK_OFF else "holiday",
                }
            )
            continue

        existing = attendance_service.get_row(storage, employee_id, day)
        worked = bool(existing and existing.get("check_in") and existing.get("source") != "leave")
        if worked and not half_day:
            skipped.append({"date": day.isoformat(), "why": "already has a check-in"})
            continue

        status = orgtime.STATUS_HALF_DAY if half_day else orgtime.STATUS_LEAVE
        if worked:
            storage.execute(
                "UPDATE attendance SET status = ?, leave_type = ?, leave_id = ?, updated_at = ? WHERE id = ?",
                (status, leave["leave_type"], leave_id, now, existing["id"]),
            )
        elif existing:
            storage.execute(
                """
                UPDATE attendance SET status = ?, leave_type = ?, source = 'leave', note = ?, leave_id = ?,
                                      check_in = NULL, check_out = NULL, worked_minutes = NULL,
                                      updated_at = ?
                WHERE id = ?
                """,
                (status, leave["leave_type"], note, leave_id, now, existing["id"]),
            )
        else:
            storage.execute(
                """
                INSERT INTO attendance (employee_id, work_date, status, leave_type, source, note, leave_id,
                                        created_at, updated_at)
                VALUES (?,?,?,?, 'leave', ?,?,?,?)
                """,
                (employee_id, day.isoformat(), status, leave["leave_type"], note, leave_id, now, now),
            )
        applied.append({"date": day.isoformat(), "status": status})

    return applied, skipped


def unstamp_attendance(storage: Storage, leave: dict[str, Any]) -> int:
    """Undo what a leave record wrote (used on cancel).

    Rows the leave created are deleted; a worked day it was laid over (half-day
    leave on a scanned day) keeps its scan and goes back to present.
    """
    removed = storage.execute(
        "DELETE FROM attendance WHERE leave_id = ? AND check_in IS NULL", (leave["id"],)
    ).rowcount or 0
    storage.execute(
        "UPDATE attendance SET status = ?, leave_type = NULL, leave_id = NULL, updated_at = ? "
        "WHERE leave_id = ?",
        (orgtime.STATUS_PRESENT, utcnow_iso(), leave["id"]),
    )
    return removed


# --------------------------------------------------------------------- apply
def _chargeable_days(storage: Storage, start: date, end: date, half_day: bool) -> float:
    if half_day:
        if start != end:
            raise employees_service.ValidationError("A half day applies to a single date.")
        kind = orgtime.day_kind(storage, start)
        if kind != "working":
            label = "a weekly off" if kind == orgtime.STATUS_WEEK_OFF else "a holiday"
            raise employees_service.ValidationError(f"{orgtime.human_date(start)} is {label} — no leave is needed.")
        return 0.5
    chargeable = orgtime.working_days(storage, start, end)
    if chargeable == 0:
        raise employees_service.ValidationError(
            "That range contains no working days (weekly offs / holidays only)."
        )
    return chargeable


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
    status: str = "approved",
    requested_by: int | None = None,
    allow_negative: bool = False,
) -> dict[str, Any]:
    """Create a leave record (optionally pending) and, if approved, stamp it.

    Paid, sick and casual leave may not exceed the available balance (accrued −
    taken − still pending) unless HR passes ``allow_negative``. Overlapping an
    existing pending/approved leave is refused.
    """
    employee = employees_service.require_employee(storage, employee_id)
    if leave_type not in orgtime.ALL_LEAVE_TYPES:
        raise employees_service.ValidationError(f"Unknown leave type '{leave_type}'.")
    if status not in ("approved", "pending"):
        raise employees_service.ValidationError("Leave can only be created as approved or pending.")

    start = orgtime.parse_date(from_date)
    end = orgtime.parse_date(to_date or from_date)
    if end < start:
        raise employees_service.ValidationError("Leave end date cannot be before the start date.")

    charged = _chargeable_days(storage, start, end, half_day)
    days = float(days) if days is not None else charged
    if days <= 0:
        raise employees_service.ValidationError("Leave days must be greater than zero.")

    locks.assert_period_open(storage, start, end)
    _check_overlap(storage, employee_id, start, end)
    if leave_type in orgtime.PAID_LEAVE_TYPES and not allow_negative:
        _check_balance(storage, settings, employee_id, days)

    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, reason, status,
                                    applied_by, created_at, decided_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, leave_type, start.isoformat(), end.isoformat(), days, reason, status,
         requested_by, now, now if status == "approved" else None),
    )
    leave_id = int(cursor.lastrowid)
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert leave is not None

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if status == "approved":
        applied, skipped = stamp_attendance(storage, leave, actor=actor)
        if not applied:
            storage.execute("DELETE FROM leave_requests WHERE id = ?", (leave_id,))
            raise employees_service.ValidationError(
                "There is no day left to charge: " + ", ".join(f"{s['date']} ({s['why']})" for s in skipped) + "."
            )
        leave = _charge_applied(storage, leave, applied)
        days = float(leave["days"])

    storage.audit(
        actor=actor,
        action="apply_leave" if status == "approved" else "request_leave",
        entity="employee",
        entity_id=employee_id,
        detail=f"{leave_type} {start.isoformat()}..{end.isoformat()} days={days:g} [{status}]"
               + (" — balance override" if allow_negative and leave_type in orgtime.PAID_LEAVE_TYPES else ""),
    )
    leave["employee_name"] = employee["name"]
    leave["employee_code"] = employee["code"]
    return {"leave": leave, "applied": applied, "skipped": skipped}


def storage_settings(storage: Storage) -> Settings:
    return storage.settings


def _check_overlap(storage: Storage, employee_id: int, start: date, end: date) -> None:
    clash = storage.query_one(
        "SELECT from_date, to_date, status FROM leave_requests WHERE employee_id = ? "
        "AND status IN ('pending','approved') AND from_date <= ? AND to_date >= ? ORDER BY from_date LIMIT 1",
        (employee_id, end.isoformat(), start.isoformat()),
    )
    if clash:
        span = orgtime.human_date(clash["from_date"]) + (
            "" if clash["from_date"] == clash["to_date"] else f" – {orgtime.human_date(clash['to_date'])}"
        )
        raise employees_service.ValidationError(
            f"There is already {'a pending request' if clash['status'] == 'pending' else 'approved leave'} on {span}."
        )


def _check_balance(storage: Storage, settings: Settings, employee_id: int, days: float,
                   *, ignore_pending_id: int | None = None) -> None:
    info = balance(storage, settings, employee_id)
    available = info["available"]
    if ignore_pending_id is not None:
        pending_self = storage.scalar(
            "SELECT days FROM leave_requests WHERE id = ? AND status = 'pending' AND leave_type IN ('paid','sick','casual')",
            (ignore_pending_id,), 0,
        )
        available += float(pending_self or 0)
    if days > available + 1e-9:
        raise employees_service.ValidationError(
            f"Not enough leave balance: {days:g} day(s) requested, {max(available, 0):g} available "
            f"({info['balance']:g} earned and unused, {info['pending_paid']:g} already requested)."
        )


def _charge_applied(storage: Storage, leave: dict[str, Any], applied: list[dict[str, Any]]) -> dict[str, Any]:
    """Charge only the days that were actually stamped (worked days are skipped)."""
    charged = 0.5 if float(leave["days"]) == 0.5 else float(len(applied))
    if charged != float(leave["days"]):
        storage.execute("UPDATE leave_requests SET days = ? WHERE id = ?", (charged, leave["id"]))
    refreshed = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave["id"],))
    assert refreshed is not None
    return refreshed


# -------------------------------------------------------------------- decide
def approve_leave(
    storage: Storage, *, leave_id: int, actor: str | None = None, force: bool = False,
    allow_negative: bool = False,
) -> dict[str, Any]:
    """Approve a pending request and stamp it onto attendance.

    ``force`` re-stamps an already-approved record (useful after fixing a clash).
    """
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if leave["status"] == "cancelled":
        raise employees_service.ValidationError("That leave was cancelled — ask the employee to re-apply.")
    if leave["status"] == "rejected":
        raise employees_service.ValidationError("That leave was rejected — ask the employee to re-apply.")
    if leave["status"] == "approved" and not force:
        raise employees_service.ValidationError("That leave is already approved.")

    locks.assert_period_open(storage, leave["from_date"], leave["to_date"])
    if leave["leave_type"] in orgtime.PAID_LEAVE_TYPES and leave["status"] == "pending" and not allow_negative:
        _check_balance(storage, storage_settings(storage), leave["employee_id"], float(leave["days"]),
                       ignore_pending_id=leave_id)
    applied, skipped = stamp_attendance(storage, leave, actor=actor)
    if not applied:
        raise employees_service.ValidationError(
            "Nothing to approve — every day is already worked or off: "
            + ", ".join(f"{s['date']} ({s['why']})" for s in skipped) + ". Reject the request instead."
        )
    _charge_applied(storage, leave, applied)
    storage.execute(
        "UPDATE leave_requests SET status = 'approved', decided_at = ? WHERE id = ?",
        (utcnow_iso(), leave_id),
    )
    storage.audit(
        actor=actor, action="approve_leave", entity="employee", entity_id=leave["employee_id"],
        detail=f"leave #{leave_id} days={leave['days']:g}",
    )
    updated = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert updated is not None
    return {"leave": updated, "applied": applied, "skipped": skipped}


def reject_leave(
    storage: Storage, *, leave_id: int, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if leave["status"] == "approved":
        raise employees_service.ValidationError(
            "This leave is already approved — cancel it instead, which also restores the attendance days."
        )
    storage.execute(
        "UPDATE leave_requests SET status = 'rejected', decided_at = ?, reason = COALESCE(?, reason) "
        "WHERE id = ?",
        (utcnow_iso(), reason, leave_id),
    )
    storage.audit(
        actor=actor, action="reject_leave", entity="employee", entity_id=leave["employee_id"],
        detail=f"leave #{leave_id}" + (f" — {reason}" if reason else ""),
    )
    updated = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert updated is not None
    return updated


def cancel_leave(storage: Storage, leave_id: int, *, actor: str | None = None) -> dict[str, Any]:
    """Cancel a leave record and remove the attendance rows it created."""
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if leave["status"] == "cancelled":
        return leave

    if leave["status"] == "approved":
        locks.assert_period_open(storage, leave["from_date"], leave["to_date"])
        unstamp_attendance(storage, leave)
    storage.execute(
        "UPDATE leave_requests SET status = 'cancelled', decided_at = ? WHERE id = ?",
        (utcnow_iso(), leave_id),
    )
    storage.audit(
        actor=actor, action="cancel_leave", entity="employee", entity_id=leave["employee_id"],
        detail=f"leave #{leave_id}",
    )
    updated = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert updated is not None
    return updated


# ---------------------------------------------------------------------- read
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
        ORDER BY
            CASE l.status WHEN 'pending' THEN 0 ELSE 1 END,
            l.from_date DESC, l.id DESC
        LIMIT ?
        """,
        params,
    )
    for row in rows:
        row["type_label"] = orgtime.LEAVE_LABELS.get(row["leave_type"], row["leave_type"])
        row["from_label"] = orgtime.human_date(row["from_date"])
        row["to_label"] = orgtime.human_date(row["to_date"])
        row["is_paid"] = row["leave_type"] in orgtime.PAID_LEAVE_TYPES
    return rows


def pending_requests(storage: Storage, *, limit: int = 50) -> list[dict[str, Any]]:
    return list_leave(storage, status="pending", limit=limit)


def count_pending(storage: Storage) -> int:
    return int(
        storage.scalar("SELECT COUNT(*) AS n FROM leave_requests WHERE status = 'pending'", (), 0)
    )


def balance(storage: Storage, settings: Settings, employee_id: int, *, as_of: date | None = None) -> dict[str, Any]:
    """Accrued vs taken paid leave, counted from the joining date."""
    employee = employees_service.require_employee(storage, employee_id)
    as_of = as_of or orgtime.today(settings)
    custom_pl = employee.get("custom_pl_accrual")
    accrual = float(custom_pl) if custom_pl is not None and str(custom_pl).strip() != "" else storage.get_float("pl_accrual_per_month", 1.5)

    joined = orgtime.parse_date(employee["joined_on"]) if employee.get("joined_on") else as_of.replace(day=1)
    if as_of < joined:
        months = 0
    else:
        months = (as_of.year - joined.year) * 12 + (as_of.month - joined.month)
        import calendar
        max_day = calendar.monthrange(as_of.year, as_of.month)[1]
        effective_join_day = min(joined.day, max_day)
        if as_of.day >= effective_join_day:
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
    pending = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) AS n FROM leave_requests "
            "WHERE employee_id = ? AND status = 'pending'",
            (employee_id,),
        )
    )
    pending_paid = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) AS n FROM leave_requests "
            "WHERE employee_id = ? AND status = 'pending' AND leave_type IN ('paid','sick','casual')",
            (employee_id,),
        )
    )

    used_pl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'paid'",
            (employee_id,),
        ) or 0
    )
    used_sl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'sick'",
            (employee_id,),
        ) or 0
    )
    used_cl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'casual'",
            (employee_id,),
        ) or 0
    )
    custom_sl = employee.get("custom_sl_quota")
    annual_sl = float(custom_sl) if custom_sl is not None and str(custom_sl).strip() != "" else storage.get_float("sl_annual_quota", 7.0)

    custom_cl = employee.get("custom_cl_quota")
    annual_cl = float(custom_cl) if custom_cl is not None and str(custom_cl).strip() != "" else storage.get_float("cl_annual_quota", 8.0)

    quotas = [
        {
            "code": "PL",
            "name": "Paid Leave",
            "used": round(used_pl, 1),
            "allocated": round(accrued, 1),
            "remaining": max(round(accrued - used_pl, 1), 0),
            "pct": min(int(round((used_pl / max(accrued, 1)) * 100)), 100),
            "color": "var(--primary)",
            "bg": "var(--primary-soft)",
            "border": "var(--primary-soft-border)",
        },
        {
            "code": "SL",
            "name": "Sick Leave",
            "used": round(used_sl, 1),
            "allocated": round(annual_sl, 1),
            "remaining": max(round(annual_sl - used_sl, 1), 0),
            "pct": min(int(round((used_sl / max(annual_sl, 1)) * 100)), 100),
            "color": "#0D9488",
            "bg": "#F0FDFA",
            "border": "#99F6E4",
        },
        {
            "code": "CL",
            "name": "Casual Leave",
            "used": round(used_cl, 1),
            "allocated": round(annual_cl, 1),
            "remaining": max(round(annual_cl - used_cl, 1), 0),
            "pct": min(int(round((used_cl / max(annual_cl, 1)) * 100)), 100),
            "color": "#D97706",
            "bg": "#FFFBEB",
            "border": "#FDE68A",
        },
    ]

    return {
        "accrual_per_month": accrual,
        "months_of_service": months,
        "accrued": accrued,
        "used_paid": round(used_paid, 2),
        "used_unpaid": round(used_unpaid, 2),
        "pending": round(pending, 2),
        "balance": round(accrued - used_paid, 2),
        "pending_paid": round(pending_paid, 2),
        "available": round(accrued - used_paid - pending_paid, 2),
        "quotas": quotas,
        "joined_on": employee.get("joined_on"),
        "as_of": as_of.isoformat(),
    }


def summary_for_period(storage: Storage, period: str) -> list[dict[str, Any]]:
    """Leave taken per employee for a month, split by type (approved only)."""
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


def employee_leave_history(storage: Storage, employee_id: int, *, limit: int = 40) -> list[dict[str, Any]]:
    return list_leave(storage, employee_id=employee_id, status="all", limit=limit)
