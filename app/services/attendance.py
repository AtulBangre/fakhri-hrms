"""Attendance: face-driven marking, manual corrections, registers and stats.

The rules implemented here (and asserted in the test suite):

* One row per employee per day (``UNIQUE(employee_id, work_date)``).
* The **first** scan of the day is a check-in. A scan after the configured grace
  period records ``late_minutes``.
* The **next** scan after the debounce window closes the day (check-out) and
  computes ``worked_minutes``. Working less than the half-day threshold turns the
  day into a half day.
* Further scans the same day are refused as duplicates, so a queue at the gate
  cannot rewrite somebody's hours.
* A day covered by approved leave, a holiday or a weekly off is never overwritten
  by a scan — except that a scan *on a week off* is recorded (that is comp-off or
  overtime evidence, and HR needs to see it).
* Payroll never has to guess: an unmarked day in the past is treated as **absent**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Settings
from ..db import Storage, utcnow_iso
from . import employees as employees_service
from . import orgtime


@dataclass(slots=True)
class MarkOutcome:
    """Result of a kiosk scan, shaped for the screen at the gate."""

    ok: bool
    action: str
    message: str
    kind: str = "info"  # success | warning | error
    employee: dict | None = None
    attendance: dict | None = None
    similarity: float | None = None
    late_minutes: int = 0
    worked_minutes: int | None = None
    status: str | None = None
    check_in: str | None = None
    check_out: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "kind": self.kind,
            "message": self.message,
            "similarity": self.similarity,
            "late_minutes": self.late_minutes,
            "worked_minutes": self.worked_minutes,
            "status": self.status,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "employee": (
                {
                    "id": self.employee["id"],
                    "code": self.employee["code"],
                    "name": self.employee["name"],
                    "department": self.employee.get("department"),
                    "designation": self.employee.get("designation"),
                }
                if self.employee
                else None
            ),
            "attendance": (
                {
                    "work_date": self.attendance.get("work_date"),
                    "check_in": self.attendance.get("check_in"),
                    "check_out": self.attendance.get("check_out"),
                    "status": self.attendance.get("status"),
                }
                if self.attendance
                else None
            ),
        }


# ------------------------------------------------------------------- queries
def get_row(storage: Storage, employee_id: int, day: date | str) -> dict | None:
    value = day.isoformat() if isinstance(day, date) else day[:10]
    return storage.query_one(
        "SELECT * FROM attendance WHERE employee_id = ? AND work_date = ?", (employee_id, value)
    )


def daily_rows(storage: Storage, settings: Settings, day: date | None = None) -> list[dict]:
    """Every active employee with their attendance for one day."""
    day = day or orgtime.today(settings)
    rows = storage.query(
        """
        SELECT e.id AS employee_id, e.code, e.name, e.department, e.designation, e.face_id,
               a.id AS attendance_id, a.status, a.check_in, a.check_out, a.late_minutes,
               a.worked_minutes, a.leave_type, a.source, a.device_label, a.note
        FROM employees e
        LEFT JOIN attendance a ON a.employee_id = e.id AND a.work_date = ?
        WHERE e.status = 'active'
        ORDER BY e.name COLLATE NOCASE
        """,
        (day.isoformat(),),
    )
    kind = orgtime.day_kind(storage, day)
    for row in rows:
        if row["status"]:
            effective = row["status"]
        elif kind in (orgtime.STATUS_HOLIDAY, orgtime.STATUS_WEEK_OFF):
            effective = kind
        elif day < orgtime.today(settings):
            effective = orgtime.STATUS_ABSENT
        else:
            effective = orgtime.STATUS_PENDING
        row["effective_status"] = effective
        row["status_label"] = orgtime.STATUS_LABELS.get(effective, effective)
        row["check_in_label"] = orgtime.format_clock(row["check_in"])
        row["check_out_label"] = orgtime.format_clock(row["check_out"])
        row["worked_label"] = orgtime.format_minutes(row["worked_minutes"])
        row["is_late"] = bool(row["late_minutes"])
    return rows


def month_rows(storage: Storage, period: str, *, department: str | None = None) -> list[dict]:
    """Raw attendance rows for a whole month (optionally one department)."""
    params: list = [f"{period}-01", f"{period}-31"]
    clause = ""
    if department:
        clause = "AND e.department = ?"
        params.append(department)
    return storage.query(
        f"""
        SELECT a.*, e.code, e.name, e.department
        FROM attendance a
        JOIN employees e ON e.id = a.employee_id
        WHERE a.work_date BETWEEN ? AND ? {clause}
        ORDER BY a.work_date
        """,
        params,
    )


def employee_month_summary(storage: Storage, settings: Settings, employee_id: int, period: str) -> dict:
    """Present / half / leave / absent / late tallies for one employee-month."""
    start, end = orgtime.month_bounds(period)
    limit = min(end, orgtime.today(settings))
    summary = {
        "period": period,
        "present": 0.0,
        "half_days": 0.0,
        "paid_leave": 0.0,
        "unpaid_leave": 0.0,
        "absent": 0.0,
        "week_off": 0,
        "holiday": 0,
        "late_marks": 0,
        "late_minutes": 0,
        "worked_minutes": 0,
        "days": 0,
    }
    if limit < start:
        return summary

    rows = {
        row["work_date"]: row
        for row in storage.query(
            "SELECT * FROM attendance WHERE employee_id = ? AND work_date BETWEEN ? AND ?",
            (employee_id, start.isoformat(), limit.isoformat()),
        )
    }
    for day in orgtime.each_day(start, limit):
        summary["days"] += 1
        row = rows.get(day.isoformat())
        if row:
            status = row["status"]
            if status == orgtime.STATUS_PRESENT:
                summary["present"] += 1
            elif status == orgtime.STATUS_HALF_DAY:
                summary["half_days"] += 1
            elif status == orgtime.STATUS_LEAVE:
                if row.get("leave_type") in orgtime.PAID_LEAVE_TYPES:
                    summary["paid_leave"] += 1
                else:
                    summary["unpaid_leave"] += 1
            elif status == orgtime.STATUS_ABSENT:
                summary["absent"] += 1
            elif status == orgtime.STATUS_WEEK_OFF:
                summary["week_off"] += 1
            elif status == orgtime.STATUS_HOLIDAY:
                summary["holiday"] += 1
            if row.get("late_minutes"):
                summary["late_marks"] += 1
                summary["late_minutes"] += int(row["late_minutes"])
            if row.get("worked_minutes"):
                summary["worked_minutes"] += int(row["worked_minutes"])
        else:
            kind = orgtime.day_kind(storage, day)
            if kind == orgtime.STATUS_WEEK_OFF:
                summary["week_off"] += 1
            elif kind == orgtime.STATUS_HOLIDAY:
                summary["holiday"] += 1
            else:
                summary["absent"] += 1
    return summary


def recent_scans(storage: Storage, *, limit: int = 12, day: date | None = None) -> list[dict]:
    params: list = []
    clause = ""
    if day:
        clause = "AND a.work_date = ?"
        params.append(day.isoformat())
    params.append(limit)
    rows = storage.query(
        f"""
        SELECT a.work_date, a.check_in, a.check_out, a.status, a.source, a.device_label,
               e.name, e.code, e.department
        FROM attendance a JOIN employees e ON e.id = a.employee_id
        WHERE a.check_in IS NOT NULL {clause}
        ORDER BY a.updated_at DESC LIMIT ?
        """,
        params,
    )
    for row in rows:
        row["check_in_label"] = orgtime.format_clock(row["check_in"])
        row["check_out_label"] = orgtime.format_clock(row["check_out"])
    return rows


# ------------------------------------------------------------------- marking
def mark_by_face(
    storage: Storage,
    settings: Settings,
    employee: dict,
    *,
    device_label: str | None = None,
    moment: datetime | None = None,
    similarity: float | None = None,
    source: str = "face",
) -> MarkOutcome:
    """Apply the check-in / check-out state machine for one scan."""
    moment = moment or orgtime.local_now(settings)
    day = moment.date()
    now_clock = orgtime.hhmmss(moment)
    existing = get_row(storage, employee["id"], day)

    if employee.get("status") != "active":
        return MarkOutcome(
            ok=False, action="inactive", kind="warning",
            message=f"{employee['name']} is marked inactive in HRMS.",
            employee=employee,
        )

    if existing:
        status = existing["status"]

        if status in (orgtime.STATUS_LEAVE, orgtime.STATUS_HOLIDAY):
            label = "on approved leave" if status == orgtime.STATUS_LEAVE else "a holiday"
            return MarkOutcome(
                ok=False, action="blocked", kind="warning",
                message=f"Today is {label} for {employee['name']}.",
                employee=employee, attendance=existing, status=status,
                check_in=existing.get("check_in"), check_out=existing.get("check_out"),
            )

        if existing.get("check_in") and not existing.get("check_out"):
            since_seconds = orgtime.seconds_between(existing["check_in"], now_clock) or 0
            if since_seconds < settings.kiosk_debounce_seconds:
                return MarkOutcome(
                    ok=False, action="duplicate", kind="warning",
                    message=f"{employee['name']} checked in a moment ago.",
                    employee=employee, attendance=existing, status=status,
                    check_in=existing["check_in"],
                )

            worked = orgtime.minutes_between(existing["check_in"], now_clock)
            half_day_threshold = storage.get_int("half_day_min_minutes", 240)
            new_status = status
            if status == orgtime.STATUS_PRESENT and worked is not None and worked < half_day_threshold:
                new_status = orgtime.STATUS_HALF_DAY
            storage.execute(
                """
                UPDATE attendance SET check_out = ?, worked_minutes = ?, status = ?, source = ?,
                                      device_label = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_clock, worked, new_status, source, device_label, utcnow_iso(), existing["id"]),
            )
            updated = get_row(storage, employee["id"], day)
            return MarkOutcome(
                ok=True, action="check_out", kind="success",
                message=f"{employee['name']} checked out at {orgtime.format_clock(now_clock)}"
                        + (" (half day)" if new_status == orgtime.STATUS_HALF_DAY else ""),
                employee=employee, attendance=updated, status=new_status,
                check_in=existing["check_in"], check_out=now_clock, worked_minutes=worked,
            )

        if existing.get("check_out"):
            since_seconds = orgtime.seconds_between(existing["check_out"], now_clock) or 0
            if since_seconds < settings.kiosk_debounce_seconds:
                return MarkOutcome(
                    ok=False, action="duplicate", kind="warning",
                    message=f"{employee['name']} already scanned a moment ago.",
                    employee=employee, attendance=existing, status=status,
                    check_in=existing.get("check_in"), check_out=existing.get("check_out"),
                )
            return MarkOutcome(
                ok=False, action="already_done", kind="info",
                message=f"{employee['name']}'s attendance is already complete for today.",
                employee=employee, attendance=existing, status=status,
                check_in=existing.get("check_in"), check_out=existing.get("check_out"),
                worked_minutes=existing.get("worked_minutes"),
            )

        # Row exists without a check-in (e.g. HR marked absent, then the person showed up).
        late = orgtime.late_minutes_for(storage, moment)
        note = "face scan overrode a manual entry"
        storage.execute(
            """
            UPDATE attendance SET check_in = ?, status = ?, late_minutes = ?, source = ?,
                                  device_label = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_clock, orgtime.STATUS_PRESENT, late, source, device_label, note, utcnow_iso(), existing["id"]),
        )
        updated = get_row(storage, employee["id"], day)
        return MarkOutcome(
            ok=True, action="check_in", kind="success",
            message=f"{employee['name']} checked in at {orgtime.format_clock(now_clock)}",
            employee=employee, attendance=updated, similarity=similarity,
            status=orgtime.STATUS_PRESENT, check_in=now_clock, late_minutes=late,
        )

    # First scan of the day.
    late = orgtime.late_minutes_for(storage, moment)
    kind = orgtime.day_kind(storage, day)
    note = None
    if kind == orgtime.STATUS_WEEK_OFF:
        note = "worked on a weekly off"
    elif kind == orgtime.STATUS_HOLIDAY:
        note = "worked on a holiday"

    now = utcnow_iso()
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, check_in, late_minutes, source,
                                device_label, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (employee["id"], day.isoformat(), orgtime.STATUS_PRESENT, now_clock, late, source,
         device_label, note, now, now),
    )
    created = get_row(storage, employee["id"], day)
    return MarkOutcome(
        ok=True, action="check_in", kind="success",
        message=f"{employee['name']} checked in at {orgtime.format_clock(now_clock)}"
                + (f" — {late} min late" if late else ""),
        employee=employee, attendance=created, similarity=similarity,
        status=orgtime.STATUS_PRESENT, check_in=now_clock, late_minutes=late,
    )


def mark_manual(
    storage: Storage,
    *,
    employee_id: int,
    work_date: str,
    status: str,
    check_in: str | None = None,
    check_out: str | None = None,
    leave_type: str | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> dict:
    """HR override / correction for any employee-day."""
    employee = employees_service.require_employee(storage, employee_id)
    day = orgtime.parse_date(work_date)
    if status not in orgtime.ALL_STATUSES:
        raise employees_service.ValidationError(f"Unknown status '{status}'.")
    if status in (orgtime.STATUS_LEAVE, orgtime.STATUS_HALF_DAY) and leave_type not in orgtime.ALL_LEAVE_TYPES:
        leave_type = leave_type or "paid"
    if status not in (orgtime.STATUS_LEAVE, orgtime.STATUS_HALF_DAY):
        leave_type = None

    check_in = (check_in or "").strip() or None
    check_out = (check_out or "").strip() or None
    if check_in:
        parsed = orgtime.parse_clock(check_in)
        if parsed is None:
            raise employees_service.ValidationError(f"'{check_in}' is not a valid check-in time.")
        check_in = parsed.strftime("%H:%M:%S")
    if check_out:
        parsed = orgtime.parse_clock(check_out)
        if parsed is None:
            raise employees_service.ValidationError(f"'{check_out}' is not a valid check-out time.")
        check_out = parsed.strftime("%H:%M:%S")

    worked = orgtime.minutes_between(check_in, check_out) if (check_in and check_out) else None
    late = 0
    if check_in and status == orgtime.STATUS_PRESENT:
        moment = datetime.combine(day, orgtime.parse_clock(check_in))
        late = orgtime.late_minutes_for(storage, moment)

    now = utcnow_iso()
    existing = get_row(storage, employee_id, day)
    if existing:
        storage.execute(
            """
            UPDATE attendance SET status = ?, check_in = ?, check_out = ?, late_minutes = ?,
                                  worked_minutes = ?, leave_type = ?, source = 'manual', note = ?,
                                  updated_at = ?
            WHERE id = ?
            """,
            (status, check_in, check_out, late, worked, leave_type, note, now, existing["id"]),
        )
    else:
        storage.execute(
            """
            INSERT INTO attendance (employee_id, work_date, status, check_in, check_out, late_minutes,
                                    worked_minutes, leave_type, source, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?, 'manual', ?,?,?)
            """,
            (employee_id, day.isoformat(), status, check_in, check_out, late, worked, leave_type,
             note, now, now),
        )
    storage.audit(actor=actor, action="mark_attendance", entity="employee", entity_id=employee_id,
                  detail=f"{day.isoformat()} → {status}")
    row = get_row(storage, employee_id, day)
    assert row is not None
    return row


def clear_day(storage: Storage, *, employee_id: int, work_date: str, actor: str | None = None) -> None:
    storage.execute(
        "DELETE FROM attendance WHERE employee_id = ? AND work_date = ?", (employee_id, work_date[:10])
    )
    storage.audit(actor=actor, action="clear_attendance", entity="employee", entity_id=employee_id,
                  detail=work_date[:10])


# ---------------------------------------------------------------- dashboards
def dashboard(storage: Storage, settings: Settings, day: date | None = None) -> dict:
    day = day or orgtime.today(settings)
    rows = daily_rows(storage, settings, day)
    kind = orgtime.day_kind(storage, day)

    counts = {
        "total": len(rows),
        "present": 0,
        "late": 0,
        "half_day": 0,
        "leave": 0,
        "absent": 0,
        "not_marked": 0,
        "week_off": 0,
        "holiday": 0,
    }
    working = 0
    for row in rows:
        status = row["effective_status"]
        counts["late"] += 1 if row["is_late"] else 0
        if status == orgtime.STATUS_PRESENT:
            counts["present"] += 1
        elif status == orgtime.STATUS_HALF_DAY:
            counts["half_day"] += 1
        elif status == orgtime.STATUS_LEAVE:
            counts["leave"] += 1
        elif status == orgtime.STATUS_ABSENT:
            counts["absent"] += 1
        elif status == orgtime.STATUS_PENDING:
            counts["not_marked"] += 1
        elif status == orgtime.STATUS_WEEK_OFF:
            counts["week_off"] += 1
        elif status == orgtime.STATUS_HOLIDAY:
            counts["holiday"] += 1
        if status not in (orgtime.STATUS_WEEK_OFF, orgtime.STATUS_HOLIDAY):
            working += 1

    attended = counts["present"] + counts["half_day"] * 0.5 + counts["leave"]
    counts["attendance_percent"] = round(attended / working * 100, 1) if working else 0.0

    trend = []
    for offset in range(6, -1, -1):
        past = day.fromordinal(day.toordinal() - offset)
        past_kind = orgtime.day_kind(storage, past)
        value = int(
            storage.scalar(
                "SELECT COUNT(*) AS n FROM attendance WHERE work_date = ? AND status IN ('present','half_day')",
                (past.isoformat(),),
            )
        )
        trend.append(
            {
                "day": past.isoformat(),
                "label": past.strftime("%a"),
                "value": value,
                "is_off": past_kind in (orgtime.STATUS_WEEK_OFF, orgtime.STATUS_HOLIDAY),
            }
        )
    peak = max((item["value"] for item in trend), default=0) or 1
    for item in trend:
        item["percent"] = round(item["value"] / peak * 100)

    return {
        "day": day,
        "day_label": orgtime.human_day(day),
        "kind": kind,
        "counts": counts,
        "rows": rows,
        "trend": trend,
        "recent": recent_scans(storage, limit=8, day=day),
        "headcount": employees_service.stats(storage),
    }


def monthly_register(
    storage: Storage,
    settings: Settings,
    period: str,
    *,
    department: str | None = None,
    include_inactive: bool = False,
) -> dict:
    """Employees × days matrix used by the attendance register screen and CSV."""
    start, end = orgtime.month_bounds(period)
    status_filter = "WHERE e.status = 'active'" if not include_inactive else "WHERE 1=1"
    if department:
        status_filter += " AND e.department = ?"
        params: list = [department]
    else:
        params = []
    staff = storage.query(
        f"SELECT e.id, e.code, e.name, e.department, e.designation FROM employees e {status_filter} "
        "ORDER BY e.name COLLATE NOCASE",
        params,
    )

    rows_by_key = {
        (row["employee_id"], row["work_date"]): row
        for row in storage.query(
            "SELECT * FROM attendance WHERE work_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        )
    }

    days = []
    today_date = orgtime.today(settings)
    for day in orgtime.each_day(start, end):
        kind = orgtime.day_kind(storage, day)
        days.append(
            {
                "iso": day.isoformat(),
                "day": day.day,
                "weekday": day.strftime("%a"),
                "weekday_short": day.strftime("%a")[0],
                "is_week_off": kind == orgtime.STATUS_WEEK_OFF,
                "is_holiday": kind == orgtime.STATUS_HOLIDAY,
                "holiday_name": orgtime.holiday_name(storage, day),
                "is_future": day > today_date,
                "is_today": day == today_date,
            }
        )

    register_rows = []
    for person in staff:
        cells = []
        totals = {
            "present": 0.0, "half_day": 0, "leave": 0, "unpaid_leave": 0, "absent": 0,
            "late": 0, "worked_minutes": 0,
        }
        for day in days:
            iso = day["iso"]
            record = rows_by_key.get((person["id"], iso))
            if record:
                status = record["status"]
                cell = {
                    "status": status,
                    "code": _cell_code(status),
                    "check_in": orgtime.format_clock(record["check_in"]),
                    "check_out": orgtime.format_clock(record["check_out"]),
                    "late_minutes": record["late_minutes"],
                    "worked_minutes": record["worked_minutes"],
                    "leave_type": record["leave_type"],
                    "note": record["note"],
                    "title": _cell_title(record),
                }
                if status == orgtime.STATUS_PRESENT:
                    totals["present"] += 1
                elif status == orgtime.STATUS_HALF_DAY:
                    totals["half_day"] += 1
                elif status == orgtime.STATUS_LEAVE:
                    totals["leave"] += 1
                    if record["leave_type"] not in orgtime.PAID_LEAVE_TYPES:
                        totals["unpaid_leave"] += 1
                elif status == orgtime.STATUS_ABSENT:
                    totals["absent"] += 1
                if record["late_minutes"]:
                    totals["late"] += 1
                if record["worked_minutes"]:
                    totals["worked_minutes"] += int(record["worked_minutes"])
            elif day["is_week_off"]:
                cell = {"status": orgtime.STATUS_WEEK_OFF, "code": "W", "title": "Weekly off"}
            elif day["is_holiday"]:
                cell = {"status": orgtime.STATUS_HOLIDAY, "code": "H", "title": day["holiday_name"] or "Holiday"}
            elif day["is_future"]:
                cell = {"status": orgtime.STATUS_PENDING, "code": "·", "title": "Not marked yet"}
            else:
                cell = {"status": orgtime.STATUS_ABSENT, "code": "A", "title": "Absent (no scan, no leave)"}
                totals["absent"] += 1
            cells.append(cell)

        totals["paid_leave"] = totals["leave"] - totals["unpaid_leave"]
        totals["payable_days"] = totals["present"] + totals["half_day"] * 0.5 + totals["paid_leave"]
        totals["lop_days"] = totals["absent"] + totals["unpaid_leave"]
        register_rows.append({"employee": person, "cells": cells, "totals": totals})

    day_totals = []
    for index in range(len(days)):
        present = sum(1 for row in register_rows if row["cells"][index]["status"] == orgtime.STATUS_PRESENT)
        half = sum(1 for row in register_rows if row["cells"][index]["status"] == orgtime.STATUS_HALF_DAY)
        on_leave = sum(1 for row in register_rows if row["cells"][index]["status"] == orgtime.STATUS_LEAVE)
        day_totals.append({"present": present, "half_day": half, "leave": on_leave})

    return {
        "period": period,
        "label": orgtime.month_label(period),
        "days": days,
        "day_totals": day_totals,
        "rows": register_rows,
        "department": department,
        "departments": employees_service.departments(storage),
    }


def _cell_code(status: str) -> str:
    return {
        orgtime.STATUS_PRESENT: "P",
        orgtime.STATUS_ABSENT: "A",
        orgtime.STATUS_HALF_DAY: "½",
        orgtime.STATUS_LEAVE: "L",
        orgtime.STATUS_WEEK_OFF: "W",
        orgtime.STATUS_HOLIDAY: "H",
        orgtime.STATUS_PENDING: "·",
    }.get(status, "?")


def _cell_title(record: dict) -> str:
    bits = [orgtime.STATUS_LABELS.get(record["status"], record["status"])]
    if record.get("check_in"):
        bits.append(f"in {orgtime.format_clock(record['check_in'])}")
    if record.get("check_out"):
        bits.append(f"out {orgtime.format_clock(record['check_out'])}")
    if record.get("late_minutes"):
        bits.append(f"late {record['late_minutes']}m")
    if record.get("worked_minutes"):
        bits.append(orgtime.format_minutes(record["worked_minutes"]))
    if record.get("leave_type"):
        bits.append(orgtime.LEAVE_LABELS.get(record["leave_type"], record["leave_type"]))
    if record.get("note"):
        bits.append(record["note"])
    if record.get("source") == "manual":
        bits.append("manual entry")
    return " · ".join(bits)


def register_csv(register: dict) -> str:
    """Flat CSV of the monthly register (one row per employee per day)."""
    lines = ["Employee Code,Employee Name,Department,Date,Status,Check In,Check Out,Late (min),Worked,Leave Type,Note"]
    for row in register["rows"]:
        employee = row["employee"]
        for day, cell in zip(register["days"], row["cells"]):
            fields = [
                employee["code"],
                employee["name"],
                employee.get("department") or "",
                day["iso"],
                cell.get("status", ""),
                cell.get("check_in") or "",
                cell.get("check_out") or "",
                str(cell.get("late_minutes") or 0),
                str(cell.get("worked_minutes") or ""),
                cell.get("leave_type") or "",
                (cell.get("title") or "").replace(",", ";"),
            ]
            lines.append(",".join(_csv_safe(value) for value in fields))
    return "\n".join(lines) + "\n"


def summary_csv(register: dict) -> str:
    """One summary row per employee (what HR actually pastes into a sheet)."""
    lines = ["Employee Code,Employee Name,Department,Present,Half Days,Leave Marks,Absent,Late Marks,Payable Days"]
    for row in register["rows"]:
        employee, totals = row["employee"], row["totals"]
        lines.append(
            ",".join(
                _csv_safe(value)
                for value in (
                    employee["code"],
                    employee["name"],
                    employee.get("department") or "",
                    f"{totals['present']:g}",
                    f"{totals['half_day']:g}",
                    f"{totals['leave']:g}",
                    f"{totals['absent']:g}",
                    f"{totals['late']:g}",
                    f"{totals['payable_days']:g}",
                )
            )
        )
    return "\n".join(lines) + "\n"


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text
