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
import json
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


def export_balances_csv(storage: Storage, settings: Settings) -> str:
    """Export leave quotas for all active employees as a clean, editable CSV ready for re-import."""
    people = employees_service.list_employees(storage, status="active", limit=1000)
    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)
    header = "Employee Code,Employee Name,Department,PL Quota,SL Quota,CL Quota"
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
        ]
        lines.append(",".join(_csv_safe(v) for v in fields))
    return "\n".join(lines) + "\n"


def get_balances_template_csv(storage: Storage) -> str:
    """Downloadable sample template pre-filled with all active employees to update their quotas."""
    people = employees_service.list_employees(storage, status="active", limit=1000)
    default_pl = storage.get_float("pl_annual_quota", 6.0)
    default_sl = storage.get_float("sl_annual_quota", 6.0)
    default_cl = storage.get_float("cl_annual_quota", 6.0)
    header = "Employee Code,Employee Name,Department,PL Quota,SL Quota,CL Quota"
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
        ]
        lines.append(",".join(_csv_safe(v) for v in fields))
    return "\n".join(lines) + "\n"


def import_balances_csv(storage: Storage, csv_text: str, *, actor: str = "") -> dict[str, Any]:
    """Bulk import leave quotas from CSV text, returning detailed breakdown of updated, unchanged, and errors."""
    import csv
    import io

    reader = csv.reader(io.StringIO(csv_text.strip()))
    header = None
    updated = 0
    unchanged = 0
    total_rows = 0
    errors: list[str] = []

    for row_idx, row in enumerate(reader, start=1):
        if not row or not any(c.strip() for c in row):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            continue

        total_rows += 1
        row_dict = {header[i]: row[i].strip() for i in range(min(len(header), len(row)))}
        code = row_dict.get("employee code") or row_dict.get("code")
        if not code:
            errors.append(f"Row {row_idx}: Missing employee code.")
            continue

        emp = employees_service.get_employee_by_code(storage, code)
        if not emp:
            errors.append(f"Row {row_idx} ({code}): Employee code not found in database.")
            continue

        updates = {}
        row_has_error = False

        # Support flexible column headers
        mappings = [
            (("pl quota", "custom annual pl", "custom pl", "annual pl", "pl"), "custom_pl_accrual"),
            (("sl quota", "custom annual sl", "custom sl", "annual sl", "sl"), "custom_sl_quota"),
            (("cl quota", "custom annual cl", "custom cl", "annual cl", "cl"), "custom_cl_quota"),
        ]

        for aliases, field in mappings:
            col_val = None
            for alias in aliases:
                if alias in row_dict and row_dict[alias] != "":
                    col_val = row_dict[alias]
                    break
            if col_val is not None:
                try:
                    val = float(col_val)
                    if val < 0:
                        errors.append(f"Row {row_idx} ({code}): {aliases[0].upper()} cannot be negative ({val}).")
                        row_has_error = True
                        break
                    updates[field] = val
                except ValueError:
                    errors.append(f"Row {row_idx} ({code}): Invalid number for {aliases[0].upper()} ('{col_val}').")
                    row_has_error = True
                    break

        if row_has_error:
            continue

        # Check if new quotas differ from current employee values in DB
        has_change = False
        for field, new_val in updates.items():
            curr_val = emp.get(field)
            curr_float = float(curr_val) if curr_val is not None and str(curr_val).strip() != "" else None
            if curr_float is None or abs(curr_float - new_val) > 0.001:
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
