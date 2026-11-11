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

from datetime import date, datetime
import csv
import io
import json
import xml.etree.ElementTree as ET
import xml.sax.saxutils
import zipfile
import zoneinfo
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
    attachments: list[dict[str, Any]] | str | None = None,
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
        _check_balance(storage, settings, employee_id, days, leave_type=leave_type)

    manager_status = "approved"
    if status == "pending":
        if employee.get("manager_id"):
            manager_status = "pending"
            status = "pending_manager"

    now = utcnow_iso()
    att_json = json.dumps(attachments) if isinstance(attachments, list) else attachments
    cursor = storage.execute(
        """
        INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, reason, status,
                                    applied_by, created_at, decided_at, manager_status, attachments)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, leave_type, start.isoformat(), end.isoformat(), days, reason, status,
         requested_by, now, now if status == "approved" else None, manager_status, att_json),
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
                   *, leave_type: str | None = None, ignore_pending_id: int | None = None) -> None:
    info = balance(storage, settings, employee_id)
    if leave_type == "sick":
        quota_label = "sick leave"
        available = info["sl_available"]
        allocated = info["annual_sl"]
        used = info["used_sl"]
        if ignore_pending_id is not None:
            pending_self = storage.scalar(
                "SELECT days FROM leave_requests WHERE id = ? AND status = 'pending' AND leave_type = 'sick'",
                (ignore_pending_id,), 0,
            )
            available += float(pending_self or 0)
    elif leave_type == "casual":
        quota_label = "casual leave"
        available = info["cl_available"]
        allocated = info["annual_cl"]
        used = info["used_cl"]
        if ignore_pending_id is not None:
            pending_self = storage.scalar(
                "SELECT days FROM leave_requests WHERE id = ? AND status = 'pending' AND leave_type = 'casual'",
                (ignore_pending_id,), 0,
            )
            available += float(pending_self or 0)
    elif leave_type == "paid":
        quota_label = "paid leave"
        available = info["pl_available"]
        allocated = info["accrued"]
        used = info["used_pl"]
        if ignore_pending_id is not None:
            pending_self = storage.scalar(
                "SELECT days FROM leave_requests WHERE id = ? AND status = 'pending' AND leave_type = 'paid'",
                (ignore_pending_id,), 0,
            )
            available += float(pending_self or 0)
    else:
        quota_label = "leave"
        available = info["total_available"]
        allocated = info["total_allocated"]
        used = info["total_used"]
        if ignore_pending_id is not None:
            pending_self = storage.scalar(
                "SELECT days FROM leave_requests WHERE id = ? AND status = 'pending' AND leave_type IN ('paid','sick','casual')",
                (ignore_pending_id,), 0,
            )
            available += float(pending_self or 0)

    if days > available + 1e-9:
        raise employees_service.ValidationError(
            f"Not enough {quota_label} balance: {days:g} day(s) requested, {max(available, 0):g} available "
            f"({allocated:g} quota, {used:g} taken)."
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
    attachments: list[dict[str, Any]] | str | None = None,
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
                       leave_type=leave["leave_type"], ignore_pending_id=leave_id)
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


def cancel_leave(storage: Storage, leave_id: int, *, actor: str | None = None, allow_past: bool | None = None) -> dict[str, Any]:
    """Cancel a leave record and remove the attendance rows it created."""
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    if leave is None:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if leave["status"] == "cancelled":
        return leave

    if leave["status"] == "approved":
        today = orgtime.today(storage_settings(storage))
        to_d = orgtime.parse_date(leave["to_date"])
        enforce_future = (actor is not None) if allow_past is None else (not allow_past)
        if to_d < today and enforce_future:
            raise employees_service.ValidationError(
                f"Approved leave #{leave_id} ({leave['from_date']} to {leave['to_date']}) "
                "cannot be cancelled because the leave date has already passed."
            )
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


# ----------------------------------------------------------- manager approval
def manager_decide_leave(
    storage: Storage,
    leave_id: int,
    manager_employee_id: int,
    *,
    decision: str,
    note: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Reporting manager recommendation/rejection of subordinate leave request."""
    row = storage.query_one(
        """
        SELECT l.*, e.manager_id, e.name AS employee_name
        FROM leave_requests l
        JOIN employees e ON e.id = l.employee_id
        WHERE l.id = ?
        """,
        (leave_id,),
    )
    if not row:
        raise employees_service.ValidationError(f"Leave #{leave_id} not found.")
    if row["manager_id"] != manager_employee_id:
        raise employees_service.ValidationError("You are not the designated reporting manager for this employee.")
    if row["status"] != "pending_manager":
        raise employees_service.ValidationError(
            f"This leave is not awaiting manager approval (current status: {row['status']})."
        )

    now = utcnow_iso()
    new_status = "pending" if decision == "approved" else "rejected"
    storage.execute(
        """
        UPDATE leave_requests
        SET manager_status = ?, status = ?, manager_action_at = ?, manager_action_by = ?, manager_note = ?,
            decided_at = CASE WHEN ? = 'rejected' THEN ? ELSE decided_at END
        WHERE id = ?
        """,
        (decision, new_status, now, manager_employee_id, note or None, decision, now, leave_id),
    )
    storage.audit(
        actor=actor or f"manager:{manager_employee_id}",
        action=f"manager_{decision}_leave",
        entity="leave",
        entity_id=leave_id,
        detail=f"decision={decision} note={note or ''}",
    )
    res = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
    assert res is not None
    return res


def list_manager_team_leave(
    storage: Storage, manager_employee_id: int, *, status: str | None = None
) -> list[dict[str, Any]]:
    """List leave requests from direct subordinates for manager review."""
    clauses = ["e.manager_id = ?"]
    params: list[Any] = [manager_employee_id]
    if status and status != "all":
        clauses.append("l.status = ?")
        params.append(status)

    rows = storage.query(
        f"""
        SELECT l.*, e.name AS employee_name, e.code AS employee_code, e.department
        FROM leave_requests l
        JOIN employees e ON e.id = l.employee_id
        WHERE {' AND '.join(clauses)}
        ORDER BY CASE l.status WHEN 'pending_manager' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, l.created_at DESC
        """,
        params,
    )
    results = []
    for row in rows:
        d = dict(row)
        d["type_label"] = orgtime.LEAVE_LABELS.get(d["leave_type"], d["leave_type"])
        d["from_label"] = orgtime.human_date(d["from_date"])
        d["to_label"] = orgtime.human_date(d["to_date"])
        d["is_paid"] = d["leave_type"] in orgtime.PAID_LEAVE_TYPES
        created = d.get("created_at") or ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                ist_dt = dt.astimezone(zoneinfo.ZoneInfo("Asia/Kolkata"))
                d["applied_label"] = ist_dt.strftime("%d %b %Y, %I:%M %p")
                d["applied_date"] = ist_dt.strftime("%d %b %Y")
                d["applied_time"] = ist_dt.strftime("%I:%M %p")
            except Exception:
                d["applied_label"] = created
                d["applied_date"] = created
                d["applied_time"] = ""
        raw_att = d.get("attachments")
        d["attachment_files"] = []
        if raw_att:
            try:
                import json
                d["attachment_files"] = json.loads(raw_att) if isinstance(raw_att, str) else (raw_att or [])
            except Exception:
                d["attachment_files"] = []
        results.append(d)
    return results


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
        SELECT l.*, e.name AS employee_name, e.code AS employee_code, e.department, e.manager_id
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
        created = row.get("created_at") or ""
        if created:
            try:
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                tz = zoneinfo.ZoneInfo(storage.settings.timezone)
                local_dt = dt.astimezone(tz)
                row["applied_label"] = local_dt.strftime("%d %b %Y, %I:%M %p")
                row["applied_date"] = local_dt.strftime("%d %b %Y")
                row["applied_time"] = local_dt.strftime("%I:%M %p")
            except Exception:
                row["applied_label"] = str(created)[:10]
                row["applied_date"] = str(created)[:10]
                row["applied_time"] = ""
        else:
            row["applied_label"] = "—"
            row["applied_date"] = "—"
            row["applied_time"] = ""
        raw_att = row.get("attachments")
        row["attachment_files"] = []
        if raw_att:
            try:
                import json
                row["attachment_files"] = json.loads(raw_att) if isinstance(raw_att, str) else (raw_att or [])
            except Exception:
                row["attachment_files"] = []
    return rows


def pending_requests(storage: Storage, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = list_leave(storage, limit=limit * 2)
    return [r for r in rows if r["status"] in ("pending", "pending_manager")][:limit]


def count_pending(storage: Storage) -> int:
    return int(
        storage.scalar("SELECT COUNT(*) AS n FROM leave_requests WHERE status IN ('pending', 'pending_manager')", (), 0)
    )


def balance(storage: Storage, settings: Settings, employee_id: int, *, as_of: date | None = None) -> dict[str, Any]:
    """Accrued vs taken paid leave, counted from the joining date."""
    from datetime import timedelta
    employee = employees_service.require_employee(storage, employee_id)
    as_of = as_of or orgtime.today(settings)
    pl_policy_mode = storage.get_setting("pl_policy_mode", "accrual")
    carry_forward_enabled = storage.get_setting("leave_carry_forward", "no") == "yes"
    cycle_type = storage.get_setting("leave_carry_forward_cycle", "financial_year")
    max_carry_forward = storage.get_float("leave_max_carry_forward", 0.0)

    cycle_start, cycle_end = orgtime.leave_cycle_bounds(as_of, cycle_type)

    custom_pl = employee.get("custom_pl_quota") or employee.get("custom_pl_accrual")
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

    carried_forward = 0.0
    if pl_policy_mode == "fixed":
        annual_pl = float(custom_pl) if custom_pl is not None and str(custom_pl).strip() != "" else storage.get_float("pl_annual_quota", 6.0)
        # Check carry-forward from previous cycle if enabled
        if carry_forward_enabled and joined < cycle_start:
            prev_end = cycle_start - timedelta(days=1)
            prev_start, _ = orgtime.leave_cycle_bounds(prev_end, cycle_type)
            prev_used = float(
                storage.scalar(
                    "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                    "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'paid' "
                    "AND from_date >= ? AND from_date <= ?",
                    (employee_id, prev_start.isoformat(), prev_end.isoformat()),
                ) or 0
            )
            unused_prev = max(round(annual_pl - prev_used, 2), 0.0)
            carried_forward = min(unused_prev, max_carry_forward) if max_carry_forward > 0 else unused_prev

        accrued = round(annual_pl + carried_forward, 2)
        accrual = round(annual_pl / 12.0, 2)
        used_pl = float(
            storage.scalar(
                "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'paid' "
                "AND from_date >= ? AND from_date <= ?",
                (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
            ) or 0
        )
        pending_pl = float(
            storage.scalar(
                "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                "WHERE employee_id = ? AND status = 'pending' AND leave_type = 'paid' "
                "AND from_date >= ? AND from_date <= ?",
                (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
            ) or 0
        )
    else:
        accrual = float(custom_pl) if custom_pl is not None and str(custom_pl).strip() != "" else storage.get_float("pl_accrual_per_month", 1.5)
        annual_pl = round(accrual * 12.0, 1)
        accrued = round(months * accrual, 2)
        used_pl = float(
            storage.scalar(
                "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'paid'",
                (employee_id,),
            ) or 0
        )
        pending_pl = float(
            storage.scalar(
                "SELECT COALESCE(SUM(days),0) FROM leave_requests "
                "WHERE employee_id = ? AND status = 'pending' AND leave_type = 'paid'",
                (employee_id,),
            ) or 0
        )

    # Annual quotas (Sick Leave & Casual Leave) reset each cycle
    pending_sl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'pending' AND leave_type = 'sick' "
            "AND from_date >= ? AND from_date <= ?",
            (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
        ) or 0
    )
    pending_cl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'pending' AND leave_type = 'casual' "
            "AND from_date >= ? AND from_date <= ?",
            (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
        ) or 0
    )

    used_sl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'sick' "
            "AND from_date >= ? AND from_date <= ?",
            (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
        ) or 0
    )
    used_cl = float(
        storage.scalar(
            "SELECT COALESCE(SUM(days),0) FROM leave_requests "
            "WHERE employee_id = ? AND status = 'approved' AND leave_type = 'casual' "
            "AND from_date >= ? AND from_date <= ?",
            (employee_id, cycle_start.isoformat(), cycle_end.isoformat()),
        ) or 0
    )

    used_paid = round(used_pl + used_sl + used_cl, 2)
    pending_paid = round(pending_pl + pending_sl + pending_cl, 2)

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
    custom_sl = employee.get("custom_sl_quota")
    annual_sl = float(custom_sl) if custom_sl is not None and str(custom_sl).strip() != "" else storage.get_float("sl_annual_quota", 6.0)

    custom_cl = employee.get("custom_cl_quota")
    annual_cl = float(custom_cl) if custom_cl is not None and str(custom_cl).strip() != "" else storage.get_float("cl_annual_quota", 6.0)

    pl_available = max(round(accrued - used_pl - pending_pl, 1), 0.0)
    sl_available = max(round(annual_sl - used_sl - pending_sl, 1), 0.0)
    cl_available = max(round(annual_cl - used_cl - pending_cl, 1), 0.0)
    total_available = round(pl_available + sl_available + cl_available, 1)
    total_allocated = round(accrued + annual_sl + annual_cl, 1)
    total_used = round(used_pl + used_sl + used_cl, 1)

    quotas = [
        {
            "code": "PL",
            "name": "Paid Leave",
            "used": round(used_pl, 1),
            "allocated": round(accrued, 1),
            "remaining": max(round(accrued - used_pl, 1), 0),
            "available": pl_available,
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
            "available": sl_available,
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
            "available": cl_available,
            "pct": min(int(round((used_cl / max(annual_cl, 1)) * 100)), 100),
            "color": "#D97706",
            "bg": "#FFFBEB",
            "border": "#FDE68A",
        },
    ]

    return {
        "policy_mode": pl_policy_mode,
        "cycle_type": cycle_type,
        "carry_forward_enabled": carry_forward_enabled,
        "annual_pl": annual_pl,
        "accrual_per_month": accrual,
        "months_of_service": months,
        "accrued": accrued,
        "carried_forward": carried_forward,
        "used_paid": round(used_paid, 2),
        "used_unpaid": round(used_unpaid, 2),
        "pending": round(pending, 2),
        "balance": round(accrued - used_paid, 2),
        "pending_paid": round(pending_paid, 2),
        "available": round(accrued - used_paid - pending_paid, 2),
        "total_available": total_available,
        "total_allocated": total_allocated,
        "total_used": total_used,
        "pl_available": pl_available,
        "sl_available": sl_available,
        "cl_available": cl_available,
        "annual_sl": annual_sl,
        "annual_cl": annual_cl,
        "used_pl": used_pl,
        "used_sl": used_sl,
        "used_cl": used_cl,
        "pending_pl": pending_pl,
        "pending_sl": pending_sl,
        "pending_cl": pending_cl,
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


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def export_balances_xlsx(storage: Storage, settings: Settings) -> bytes:
    """Generate styled Excel (.xlsx) file with leave quotas and balances for all active employees."""
    escape = xml.sax.saxutils.escape
    people = employees_service.list_employees(storage, status="active", limit=1000)
    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)

    headers = [
        "Employee Code",
        "Employee Name",
        "Department",
        "PL Quota",
        "SL Quota",
        "CL Quota",
        "Unpaid LOP",
        "Total Available",
    ]

    rows_data: list[list[Any]] = []
    for p in people:
        bal = balance(storage, settings, p["id"])
        pl_val = p.get("custom_pl_quota") or p.get("custom_pl_accrual")
        sl_val = p.get("custom_sl_quota")
        cl_val = p.get("custom_cl_quota")
        pl_num = float(pl_val) if pl_val is not None and str(pl_val).strip() != "" else default_pl
        sl_num = float(sl_val) if sl_val is not None and str(sl_val).strip() != "" else default_sl
        cl_num = float(cl_val) if cl_val is not None and str(cl_val).strip() != "" else default_cl
        unpaid_lop = float(bal.get("used_unpaid") or 0.0)
        tot_avail = float(bal.get("total_available") or 0.0)

        rows_data.append([
            p["code"],
            p["name"],
            p.get("department") or "",
            pl_num,
            sl_num,
            cl_num,
            unpaid_lop,
            tot_avail,
        ])

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>""",
        )

        z.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )

        z.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )

        z.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Leave Balances" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )

        z.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><name val="Calibri"/><sz val="11"/><color rgb="FF141414"/></font>
    <font><b/><color rgb="FFFFFFFF"/><name val="Calibri"/><sz val="11"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF870808"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/></border>
    <border>
      <left style="thin"><color rgb="FFE2E8F0"/></left>
      <right style="thin"><color rgb="FFE2E8F0"/></right>
      <top style="thin"><color rgb="FFE2E8F0"/></top>
      <bottom style="thin"><color rgb="FFE2E8F0"/></bottom>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment horizontal="right" vertical="center"/></xf>
  </cellXfs>
</styleSheet>""",
        )

        sheet_lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            '<cols>',
            '  <col min="1" max="1" width="18" customWidth="1"/>',
            '  <col min="2" max="2" width="26" customWidth="1"/>',
            '  <col min="3" max="3" width="20" customWidth="1"/>',
            '  <col min="4" max="6" width="16" customWidth="1"/>',
            '  <col min="7" max="7" width="15" customWidth="1"/>',
            '  <col min="8" max="8" width="18" customWidth="1"/>',
            '</cols>',
            '<sheetData>',
        ]

        # Header row
        header_cells = []
        for c_idx, h in enumerate(headers):
            col_letter = chr(ord("A") + c_idx)
            header_cells.append(f'<c r="{col_letter}1" s="1" t="inlineStr"><is><t>{escape(h)}</t></is></c>')
        sheet_lines.append(f'<row r="1" ht="28" customHeight="1">{" ".join(header_cells)}</row>')

        # Data rows
        for r_idx, r in enumerate(rows_data, start=2):
            cells = []
            for c_idx, val in enumerate(r):
                col_letter = chr(ord("A") + c_idx)
                if isinstance(val, (int, float)):
                    cells.append(f'<c r="{col_letter}{r_idx}" s="3"><v>{val:g}</v></c>')
                else:
                    cells.append(f'<c r="{col_letter}{r_idx}" s="2" t="inlineStr"><is><t>{escape(str(val))}</t></is></c>')
            sheet_lines.append(f'<row r="{r_idx}" ht="20" customHeight="1">{" ".join(cells)}</row>')

        sheet_lines.append("</sheetData></worksheet>")
        z.writestr("xl/worksheets/sheet1.xml", "\n".join(sheet_lines))

    return out.getvalue()


def export_balances_csv(storage: Storage, settings: Settings) -> str:
    """Export leave quotas for all active employees as a clean, editable CSV ready for re-import."""
    people = employees_service.list_employees(storage, status="active", limit=1000)
    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)
    header = "Employee Code,Employee Name,Department,PL Quota,SL Quota,CL Quota,Unpaid LOP,Total Available"
    lines = [header]
    for p in people:
        bal = balance(storage, settings, p["id"])
        pl_val = p.get("custom_pl_quota") or p.get("custom_pl_accrual")
        sl_val = p.get("custom_sl_quota")
        cl_val = p.get("custom_cl_quota")
        pl_str = f"{float(pl_val):g}" if pl_val is not None and str(pl_val).strip() != "" else f"{default_pl:g}"
        sl_str = f"{float(sl_val):g}" if sl_val is not None and str(sl_val).strip() != "" else f"{default_sl:g}"
        cl_str = f"{float(cl_val):g}" if cl_val is not None and str(cl_val).strip() != "" else f"{default_cl:g}"
        unpaid_lop = f"{float(bal.get('used_unpaid') or 0.0):g}"
        tot_avail = f"{float(bal.get('total_available') or 0.0):g}"
        fields = [
            p["code"],
            p["name"],
            p.get("department") or "",
            pl_str,
            sl_str,
            cl_str,
            unpaid_lop,
            tot_avail,
        ]
        lines.append(",".join(_csv_safe(v) for v in fields))
    return "\n".join(lines) + "\n"


def get_balances_template_csv(storage: Storage) -> str:
    """Downloadable sample template pre-filled with all active employees to update their quotas."""
    people = employees_service.list_employees(storage, status="active", limit=1000)
    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)
    header = "Employee Code,Employee Name,Department,PL Quota,SL Quota,CL Quota,Unpaid LOP"
    lines = [header]
    for p in people:
        pl_val = p.get("custom_pl_quota") or p.get("custom_pl_accrual")
        sl_val = p.get("custom_sl_quota")
        cl_val = p.get("custom_cl_quota")
        pl_str = f"{float(pl_val):g}" if pl_val is not None and str(pl_val).strip() != "" else f"{default_pl:g}"
        sl_str = f"{float(sl_val):g}" if sl_val is not None and str(sl_val).strip() != "" else f"{default_sl:g}"
        cl_str = f"{float(cl_val):g}" if cl_val is not None and str(cl_val).strip() != "" else f"{default_cl:g}"
        fields = [
            p["code"],
            p["name"],
            p.get("department") or "",
            pl_str,
            sl_str,
            cl_str,
            "0",
        ]
        lines.append(",".join(_csv_safe(v) for v in fields))
    return "\n".join(lines) + "\n"


def parse_spreadsheet_rows(file_bytes: bytes, filename: str = "") -> list[list[str]]:
    """Parse raw bytes of an Excel (.xlsx) or CSV file into a list of string rows."""
    if file_bytes.startswith(b"PK\x03\x04") or filename.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
                # 1. Parse sharedStrings table if present
                sst: list[str] = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    root_sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in root_sst.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                        sst.append("".join(si.itertext()))

                # 2. Parse sheet1.xml
                sheet_name = "xl/worksheets/sheet1.xml"
                if sheet_name not in zf.namelist():
                    # fallback to first worksheet
                    sheets = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                    if sheets:
                        sheet_name = sheets[0]
                    else:
                        raise ValueError("No worksheets found in Excel file.")

                sheet_root = ET.fromstring(zf.read(sheet_name))
                parsed_rows: list[list[str]] = []
                for row_el in sheet_root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                    row_vals: dict[int, str] = {}
                    for c in row_el.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                        r_ref = c.attrib.get("r", "")
                        col_letters = "".join([ch for ch in r_ref if ch.isalpha()])
                        col_idx = 0
                        for ch in col_letters:
                            col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
                        col_idx -= 1

                        t_attr = c.attrib.get("t", "")
                        v_el = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        val = v_el.text.strip() if v_el is not None and v_el.text is not None else ""
                        if t_attr == "s" and val.isdigit():
                            idx = int(val)
                            val = sst[idx] if idx < len(sst) else val
                        elif t_attr in ("inlineStr", "str") or not val:
                            is_t = c.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                            if is_t is not None and is_t.text is not None:
                                val = is_t.text.strip()
                        row_vals[col_idx] = val

                    if row_vals:
                        max_c = max(row_vals.keys())
                        row_list = [row_vals.get(i, "") for i in range(max_c + 1)]
                        parsed_rows.append(row_list)
                return parsed_rows
        except Exception as exc:
            # Fallback to CSV if reading as zip failed
            pass

    # CSV parsing fallback
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1", errors="replace")

    reader = csv.reader(io.StringIO(text.strip()))
    return [row for row in reader if row and any(c.strip() for c in row)]


def import_balances_data(storage: Storage, rows: list[list[str]], *, actor: str = "") -> dict[str, Any]:
    """Import leave quotas from parsed rows (from Excel or CSV).

    Validates every row:
    - If a row has an invalid quota (e.g. negative number or non-numeric), logs detailed error and continues.
    - If a row has no changes compared to current employee DB values, increments unchanged (No Change).
    - If a row has valid changes, updates DB and increments updated.
    """
    header = None
    updated = 0
    unchanged = 0
    total_rows = 0
    errors: list[str] = []

    # Column index positions mapped from header
    col_code: int | None = None
    col_pl: int | None = None
    col_sl: int | None = None
    col_cl: int | None = None

    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)
    defaults = {
        "custom_pl_accrual": default_pl,
        "custom_sl_quota": default_sl,
        "custom_cl_quota": default_cl,
    }

    for row_idx, row in enumerate(rows, start=1):
        if not row or not any(str(c).strip() for c in row):
            continue

        clean_row = [str(c).strip() for c in row]

        # First non-empty row is header
        if header is None:
            header = [c.lower() for c in clean_row]
            for idx, h in enumerate(header):
                if h in ("employee code", "code", "emp code", "emp_code") or ("code" in h and "employee" in h):
                    col_code = idx
                elif "unpaid" in h or "lop" in h or "loss of pay" in h:
                    col_lop = idx
                elif "sick" in h or "sl" in h:
                    col_sl = idx
                elif "casual" in h or "cl" in h:
                    col_cl = idx
                elif "paid" in h or "pl" in h:
                    col_pl = idx
            continue

        total_rows += 1

        if col_code is None or col_code >= len(clean_row):
            errors.append(f"Row {row_idx}: Missing 'Employee Code' column in file.")
            continue

        code = clean_row[col_code]
        if not code:
            errors.append(f"Row {row_idx}: Empty employee code.")
            continue

        emp = employees_service.get_employee_by_code(storage, code)
        if not emp:
            errors.append(f"Row {row_idx} ({code}): Employee code not found in database.")
            continue

        updates: dict[str, float] = {}
        row_has_error = False

        # Validate PL, SL, CL numbers
        field_configs = [
            (col_pl, "custom_pl_accrual", "PL Quota"),
            (col_sl, "custom_sl_quota", "SL Quota"),
            (col_cl, "custom_cl_quota", "CL Quota"),
        ]

        for col_idx, db_field, label in field_configs:
            if col_idx is not None and col_idx < len(clean_row):
                col_val = clean_row[col_idx]
                if col_val != "":
                    try:
                        val = float(col_val)
                        if val < 0:
                            errors.append(f"Row {row_idx} ({code}): {label} cannot be negative ({val:g}).")
                            row_has_error = True
                            break
                        updates[db_field] = val
                    except ValueError:
                        errors.append(f"Row {row_idx} ({code}): Invalid numeric value for {label} ('{col_val}').")
                        row_has_error = True
                        break

        if row_has_error:
            continue

        # Check if new quotas differ from current effective employee values in DB
        has_change = False
        for field, new_val in updates.items():
            curr_val = emp.get(field)
            curr_float = float(curr_val) if curr_val is not None and str(curr_val).strip() != "" else defaults.get(field, 0.0)
            if abs(curr_float - new_val) > 0.001:
                has_change = True
                break

        if has_change and updates:
            employees_service.update_employee(storage, emp["id"], updates)
            updated += 1
        else:
            unchanged += 1

    if updated:
        storage.audit(
            actor=actor,
            action="import_leave_balances",
            entity="leave",
            entity_id=0,
            detail=f"Bulk imported leave quotas: {updated} updated, {unchanged} unchanged",
        )

    return {
        "total_rows": total_rows,
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
    }


def import_balances_csv(storage: Storage, csv_text: str, *, actor: str = "") -> dict[str, Any]:
    """Bulk import leave quotas from CSV text (backwards compatible wrapper)."""
    rows = parse_spreadsheet_rows(csv_text.encode("utf-8"), "import.csv")
    return import_balances_data(storage, rows, actor=actor)
