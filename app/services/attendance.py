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

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..config import Settings

logger = logging.getLogger("hrms.attendance")
from ..db import Storage, utcnow_iso
from . import employees as employees_service
from . import locks, orgtime


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
    late_fine: float = 0.0
    late_half_day: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "kind": self.kind,
            "message": self.message,
            "similarity": self.similarity,
            "late_minutes": self.late_minutes,
            "late_fine": self.late_fine,
            "late_half_day": self.late_half_day,
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
        SELECT e.id AS employee_id, e.code, e.name, e.department, e.designation, e.face_id, e.photo_filename,
               a.id AS attendance_id, a.status, a.check_in, a.check_out, a.late_minutes,
               a.worked_minutes, a.leave_type, a.source, a.device_label, a.note, a.late_fine,
               a.punch_in_photo, a.punch_out_photo
        FROM employees e
        LEFT JOIN attendance a ON a.employee_id = e.id AND a.work_date = ?
        WHERE e.status = 'active'
        ORDER BY e.name COLLATE NOCASE
        """,
        (day.isoformat(),),
    )
    kind = orgtime.day_kind(storage, day)
    joined = {r["id"]: r["joined_on"] for r in storage.query("SELECT id, joined_on FROM employees WHERE status = 'active'")}
    for row in rows:
        if (joined.get(row["employee_id"]) or "") > day.isoformat():
            effective = orgtime.STATUS_NOT_EMPLOYED
        elif row["status"]:
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
        row["source_label"] = orgtime.source_label(row)
        row["user_note"] = orgtime.user_note(row)
    return rows


def month_rows(storage: Storage, period: str, *, department: str | None = None) -> list[dict]:
    """Raw attendance rows for a whole month (optionally one department)."""
    start, end = orgtime.month_bounds(period)
    params: list = [start.isoformat(), end.isoformat()]
    clause = ""
    if department:
        clause = "AND e.department = ? COLLATE NOCASE"
        params.append(department.strip())
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


# ------------------------------------------------------------ day classifier
#: Tally keys produced by :func:`classify_day`. Payroll, the register and the ESS
#: views all count with these, so they can never disagree about a day.
DAY_CLASSES = (
    "present", "half_paid", "half_unpaid", "leave_paid", "leave_unpaid", "absent",
    "week_off", "holiday", "pending", "future", "not_employed",
)


def employment_window(storage: Storage, employee: dict, start: date, end: date) -> tuple[date, date] | None:
    """The part of ``start..end`` the person was employed: joining date to last working day.

    The last working day comes from an approved or settled exit. ``None`` when the
    whole range falls outside their employment.
    """
    lo, hi = start, end
    if employee.get("joined_on"):
        lo = max(lo, orgtime.parse_date(employee["joined_on"]))
    exit_row = storage.query_one(
        "SELECT last_working_day FROM employee_exits WHERE employee_id = ? AND status IN ('approved','settled') "
        "ORDER BY id DESC LIMIT 1",
        (employee["id"],),
    )
    if exit_row:
        hi = min(hi, orgtime.parse_date(exit_row["last_working_day"]))
    return (lo, hi) if lo <= hi else None


def classify_day(record: dict | None, kind: str, day: date, today: date,
                 window: tuple[date, date] | None) -> str:
    """One of :data:`DAY_CLASSES` for an employee-day.

    * outside the employment window → ``not_employed`` (never paid, never absent)
    * a half day is paid when it carries a paid leave type, otherwise it costs 0.5 LOP
    * an unmarked working day is ``absent`` only once it has passed — today is ``pending``
    """
    if window is None or day < window[0] or day > window[1]:
        return "not_employed"
    if record:
        status, leave_type = record["status"], record.get("leave_type")
        if status == orgtime.STATUS_PRESENT:
            return "present"
        if status == orgtime.STATUS_HALF_DAY:
            return "half_paid" if leave_type in orgtime.PAID_LEAVE_TYPES else "half_unpaid"
        if status == orgtime.STATUS_LEAVE:
            return "leave_paid" if leave_type in orgtime.PAID_LEAVE_TYPES else "leave_unpaid"
        if status in (orgtime.STATUS_ABSENT, orgtime.STATUS_WEEK_OFF, orgtime.STATUS_HOLIDAY):
            return status
    if kind in (orgtime.STATUS_WEEK_OFF, orgtime.STATUS_HOLIDAY):
        return kind
    if day < today:
        return "absent"
    return "pending" if day == today else "future"


def pay_figures(tally: dict, late_marks: int, marks_per_half_lop: int) -> dict:
    """Payable and loss-of-pay days from a tally — the one formula payroll and the register share."""
    late_lop = (late_marks // marks_per_half_lop) * 0.5 if marks_per_half_lop > 0 else 0.0
    present_days = tally["present"] + 0.5 * (tally["half_paid"] + tally["half_unpaid"])
    paid_leave = tally["leave_paid"] + 0.5 * tally["half_paid"]
    lop = tally["absent"] + tally["leave_unpaid"] + 0.5 * tally["half_unpaid"] + late_lop
    return {"present_days": present_days, "paid_leave_days": paid_leave,
            "late_lop_days": late_lop, "lop_days": lop}


def employee_month_summary(storage: Storage, settings: Settings, employee_id: int, period: str) -> dict:
    """Present / half / leave / absent / late tallies for one employee-month."""
    start, end = orgtime.month_bounds(period)
    summary = employee_range_summary(storage, employee_id, start, end, today=orgtime.today(settings))
    summary["period"] = period
    return summary


def employee_range_summary(storage: Storage, employee_id: int, start: date, limit: date,
                           *, today: date | None = None) -> dict:
    """Tallies for any date range (used by payroll, the ESS views and the F&F settlement).

    Days after ``today`` are not counted yet; ``employed_days`` is the calendar
    length of the employment window inside the range (used to pro-rate salary).
    """
    employee = employees_service.require_employee(storage, employee_id)
    today = today or limit
    window = employment_window(storage, employee, start, limit)
    tally = {key: 0 for key in DAY_CLASSES}
    summary = {"period": orgtime.period_of(start), "late_marks": 0, "late_minutes": 0, "late_fines": 0.0,
               "worked_minutes": 0,
               "days": 0, "employed_days": (window[1] - window[0]).days + 1 if window else 0}
    counted_until = min(limit, today)
    rows = {
        row["work_date"]: row
        for row in storage.query(
            "SELECT * FROM attendance WHERE employee_id = ? AND work_date BETWEEN ? AND ?",
            (employee_id, start.isoformat(), counted_until.isoformat()),
        )
    } if counted_until >= start else {}
    for day in (orgtime.each_day(start, counted_until) if counted_until >= start else []):
        summary["days"] += 1
        row = rows.get(day.isoformat())
        cls = classify_day(row, orgtime.day_kind(storage, day), day, today, window)
        tally[cls] += 1
        if row and cls != "not_employed":
            if row.get("late_minutes"):
                summary["late_marks"] += 1
                summary["late_minutes"] += int(row["late_minutes"])
            summary["late_fines"] += float(row.get("late_fine") or 0)
            if row.get("worked_minutes"):
                summary["worked_minutes"] += int(row["worked_minutes"])
    summary["late_fines"] = round(summary["late_fines"], 2)
    summary.update(tally)
    # backwards-compatible names used by templates
    summary["half_days"] = tally["half_paid"] + tally["half_unpaid"]
    summary["paid_leave"] = tally["leave_paid"]
    summary["unpaid_leave"] = tally["leave_unpaid"]
    return summary


def employee_month_days(storage: Storage, settings: Settings, employee: dict, period: str) -> list[dict]:
    """Assemble all calendar days in a month for an employee with punch status and photos."""
    start, end = orgtime.month_bounds(period)
    rows = {
        row["work_date"]: row
        for row in storage.query(
            "SELECT * FROM attendance WHERE employee_id = ? AND work_date BETWEEN ? AND ?",
            (employee["id"], start.isoformat(), end.isoformat()),
        )
    }
    today = orgtime.today(settings)
    window = employment_window(storage, employee, start, end)
    days = []
    for day in orgtime.days_in_period(period):
        iso = day.isoformat()
        record = rows.get(iso)
        cls = classify_day(record, orgtime.day_kind(storage, day), day, today, window)
        status = (record["status"] if record and cls != "not_employed"
                  else {"not_employed": orgtime.STATUS_NOT_EMPLOYED, "absent": orgtime.STATUS_ABSENT,
                        "week_off": orgtime.STATUS_WEEK_OFF, "holiday": orgtime.STATUS_HOLIDAY}
                  .get(cls, orgtime.STATUS_PENDING))
        days.append(
            {
                "day": day,
                "iso": iso,
                "weekday": day.strftime("%a"),
                "status": status,
                "status_label": orgtime.STATUS_LABELS.get(status, status),
                "check_in": orgtime.format_clock(record["check_in"]) if record else "—",
                "check_out": orgtime.format_clock(record["check_out"]) if record else "—",
                "worked": orgtime.format_minutes(record["worked_minutes"]) if record else "—",
                "raw_in": record["check_in"][:5] if record and record.get("check_in") else "",
                "raw_out": record["check_out"][:5] if record and record.get("check_out") else "",
                "raw_status": record["status"] if record else "",
                "raw_leave_type": record.get("leave_type") or "" if record else "",
                "punch_in_photo": record.get("punch_in_photo") if record else None,
                "punch_out_photo": record.get("punch_out_photo") if record else None,
                "late_minutes": record["late_minutes"] if record else 0,
                "late_fine": record.get("late_fine") if record else 0,
                "leave_type": orgtime.LEAVE_LABELS.get(record["leave_type"], "") if record else "",
                "note": orgtime.user_note(record) if record else None,
                "source": orgtime.source_label(record) if record and cls != "not_employed" else None,
                "day_kind": orgtime.day_kind(storage, day),
                "is_off": orgtime.day_kind(storage, day) in ("week_off", "holiday") or status in (orgtime.STATUS_WEEK_OFF, orgtime.STATUS_HOLIDAY),
                "is_today": day == today,
                "is_future": day > today,
                "is_fixable": day <= today,
            }
        )
    return days


#: The gate screen polls its status every 20 s; three missed polls means offline.
KIOSK_ONLINE_SECONDS = 75


def record_kiosk_heartbeat(storage: Storage, device: str) -> None:
    """Remember when a gate device last checked in (shown live on the HR dashboard)."""
    try:
        beats = json.loads(storage.get_setting("kiosk_heartbeat", "{}") or "{}")
    except ValueError:
        beats = {}
    beats[device[:60]] = utcnow_iso()
    storage.set_setting("kiosk_heartbeat", json.dumps(beats))


def kiosk_devices(storage: Storage) -> list[dict]:
    """Every gate device seen so far, with an online flag and seconds since last poll."""
    try:
        beats = json.loads(storage.get_setting("kiosk_heartbeat", "{}") or "{}")
    except ValueError:
        return []
    now = datetime.now(timezone.utc)
    devices = []
    for device, seen in sorted(beats.items()):
        try:
            age = (now - datetime.fromisoformat(seen.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            continue
        devices.append({"device": device, "seen_at": seen, "age_seconds": int(age),
                        "online": age <= KIOSK_ONLINE_SECONDS})
    return devices


def unmarked_working_days(storage: Storage, settings: Settings, period: str) -> list[dict]:
    """Active employees with past working days that have no attendance at all.

    Payroll charges these as absent (LOP), so HR should see them before running a
    month. Days before an employee joined are left out — that LOP is deliberate
    pro-rating, not a forgotten mark.
    """
    start, end = orgtime.month_bounds(period)
    limit = min(end, orgtime.today(settings) - timedelta(days=1))
    if limit < start:
        return []
    working = [day for day in orgtime.each_day(start, limit) if orgtime.day_kind(storage, day) == "working"]
    if not working:
        return []
    marked: dict[int, set[str]] = {}
    for row in storage.query(
        "SELECT employee_id, work_date FROM attendance WHERE work_date BETWEEN ? AND ?",
        (start.isoformat(), limit.isoformat()),
    ):
        marked.setdefault(row["employee_id"], set()).add(row["work_date"])

    result = []
    for employee in storage.query(
        "SELECT id, code, name, joined_on FROM employees WHERE status = 'active' ORDER BY name COLLATE NOCASE"
    ):
        joined = employee.get("joined_on") or ""
        days = [
            day for day in working
            if day.isoformat() >= joined[:10] and day.isoformat() not in marked.get(employee["id"], set())
        ]
        if days:
            result.append({**employee, "days": days})
    return result


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
               a.employee_id, e.name, e.code, e.department
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
    photo_bytes: bytes | None = None,
) -> MarkOutcome:
    """Apply the check-in / check-out state machine for one scan."""
    moment = moment or orgtime.local_now(settings)
    day = moment.date()
    now_clock = orgtime.hhmmss(moment)
    existing = get_row(storage, employee["id"], day)

    if locks.locked_status(storage, orgtime.period_of(day)):
        return MarkOutcome(
            ok=False, action="blocked", kind="warning",
            message="This month's payroll is already finalised — ask HR to record attendance.",
            employee=employee,
        )

    left = storage.query_one(
        "SELECT last_working_day FROM employee_exits WHERE employee_id = ? AND status IN ('approved','settled') "
        "AND last_working_day < ? ORDER BY id DESC LIMIT 1",
        (employee["id"], day.isoformat()),
    )
    if left:
        return MarkOutcome(
            ok=False, action="blocked", kind="warning",
            message=f"{employee['name']}'s last working day was {orgtime.human_date(left['last_working_day'])}.",
            employee=employee,
        )

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

        # Save check-out punch photo if provided
        out_photo = None
        if photo_bytes:
            try:
                from ..uploads import save_upload
                stored = save_upload(settings, "punches", photo_bytes, original_name=f"out_{employee['id']}_{day.isoformat()}.jpg")
                out_photo = stored.stored_name
            except Exception as exc:
                logger.warning("could not save punch out photo: %s", exc)

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
                                      punch_out_photo = COALESCE(?, punch_out_photo),
                                      device_label = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_clock, worked, new_status, source, out_photo, device_label, utcnow_iso(), existing["id"]),
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

        # Save check-in punch photo if provided
        in_photo = None
        if photo_bytes:
            try:
                from ..uploads import save_upload
                stored = save_upload(settings, "punches", photo_bytes, original_name=f"in_{employee['id']}_{day.isoformat()}.jpg")
                in_photo = stored.stored_name
            except Exception as exc:
                logger.warning("could not save punch in photo: %s", exc)

        # Half-day leave: the person works the other half. Record the scan, keep the
        # paid half day — never turn it into a full "present" (LV-05).
        if status == orgtime.STATUS_HALF_DAY and existing.get("leave_type"):
            storage.execute(
                "UPDATE attendance SET check_in = ?, punch_in_photo = COALESCE(?, punch_in_photo), device_label = ?, updated_at = ? WHERE id = ?",
                (now_clock, in_photo, device_label, utcnow_iso(), existing["id"]),
            )
            updated = get_row(storage, employee["id"], day)
            return MarkOutcome(
                ok=True, action="check_in", kind="success",
                message=f"{employee['name']} checked in at {orgtime.format_clock(now_clock)} (half-day leave)",
                employee=employee, attendance=updated, similarity=similarity,
                status=orgtime.STATUS_HALF_DAY, check_in=now_clock, late_minutes=0,
            )

        # Row exists without a check-in (e.g. HR marked absent, then the person showed up).
        assessed = orgtime.late_assessment(storage, moment)
        status_in = orgtime.STATUS_HALF_DAY if assessed["half_day"] else orgtime.STATUS_PRESENT
        note = "face scan overrode a manual entry"
        storage.execute(
            """
            UPDATE attendance SET check_in = ?, status = ?, late_minutes = ?, late_fine = ?, source = ?,
                                  punch_in_photo = COALESCE(?, punch_in_photo),
                                  device_label = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_clock, status_in, assessed["late_minutes"], assessed["fine"], source, in_photo, device_label, note,
             utcnow_iso(), existing["id"]),
        )
        updated = get_row(storage, employee["id"], day)
        return MarkOutcome(
            ok=True, action="check_in", kind="success",
            message=f"{employee['name']} checked in at {orgtime.format_clock(now_clock)}",
            employee=employee, attendance=updated, similarity=similarity,
            status=status_in, check_in=now_clock, late_minutes=assessed["late_minutes"],
            late_fine=assessed["fine"], late_half_day=assessed["half_day"],
        )

    # First scan of the day.
    in_photo = None
    if photo_bytes:
        try:
            from ..uploads import save_upload
            stored = save_upload(settings, "punches", photo_bytes, original_name=f"in_{employee['id']}_{day.isoformat()}.jpg")
            in_photo = stored.stored_name
        except Exception as exc:
            logger.warning("could not save punch in photo: %s", exc)

    kind = orgtime.day_kind(storage, day)
    note = None
    assessed = {"late_minutes": 0, "fine": 0.0, "half_day": False}
    if kind == orgtime.STATUS_WEEK_OFF:
        note = "worked on a weekly off"
    elif kind == orgtime.STATUS_HOLIDAY:
        note = "worked on a holiday"
    else:
        assessed = orgtime.late_assessment(storage, moment)  # no late rules on an off day
    late = assessed["late_minutes"]
    status_in = orgtime.STATUS_HALF_DAY if assessed["half_day"] else orgtime.STATUS_PRESENT
    if assessed["half_day"]:
        note = "late arrival — half day"

    now = utcnow_iso()
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, check_in, late_minutes, late_fine, source,
                                punch_in_photo, device_label, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (employee["id"], day.isoformat(), status_in, now_clock, late, assessed["fine"], source,
         in_photo, device_label, note, now, now),
    )
    created = get_row(storage, employee["id"], day)
    return MarkOutcome(
        ok=True, action="check_in", kind="success",
        message=f"{employee['name']} checked in at {orgtime.format_clock(now_clock)}"
                + (f" — {late} min late" if late else "")
                + (" (half day)" if assessed["half_day"] else ""),
        employee=employee, attendance=created, similarity=similarity,
        status=status_in, check_in=now_clock, late_minutes=late,
        late_fine=assessed["fine"], late_half_day=assessed["half_day"],
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
    release_leave: bool = False,
) -> dict:
    """HR override / correction for any employee-day.

    A day that belongs to an approved leave is only overwritten when the caller
    confirms with ``release_leave`` — the day then goes back to the balance (F3).
    """
    employee = employees_service.require_employee(storage, employee_id)
    day = orgtime.parse_date(work_date)
    locks.assert_period_open(storage, day)
    if status not in orgtime.ALL_STATUSES:
        raise employees_service.ValidationError(f"Unknown status '{status}'.")
    if leave_type not in orgtime.ALL_LEAVE_TYPES:
        leave_type = None
    if status == orgtime.STATUS_LEAVE and leave_type is None:
        leave_type = "paid"
    # A half day keeps no leave type unless HR picks one: the other half is then
    # unpaid (0.5 LOP), never silently paid leave (PAY-01).
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
    late, fine = 0, 0.0
    if check_in and status == orgtime.STATUS_PRESENT and orgtime.day_kind(storage, day) == "working":
        assessed = orgtime.late_assessment(storage, datetime.combine(day, orgtime.parse_clock(check_in)))
        late, fine = assessed["late_minutes"], assessed["fine"]
        if assessed["half_day"]:
            status = orgtime.STATUS_HALF_DAY  # late-arrival policy: a half day, no fine

    now = utcnow_iso()
    existing = get_row(storage, employee_id, day)
    if existing and existing.get("leave_id"):
        if not release_leave:
            raise LeaveDayConflict(
                f"{orgtime.human_date(day)} is on approved leave (#{existing['leave_id']}). Confirm that the leave "
                "day should be given back to the balance, then save again."
            )
        _release_leave_day(storage, existing, actor=actor)
    if existing:
        storage.execute(
            """
            UPDATE attendance SET status = ?, check_in = ?, check_out = ?, late_minutes = ?, late_fine = ?,
                                  worked_minutes = ?, leave_type = ?, source = 'manual', note = ?,
                                  leave_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (status, check_in, check_out, late, fine, worked, leave_type, note, now, existing["id"]),
        )
    else:
        storage.execute(
            """
            INSERT INTO attendance (employee_id, work_date, status, check_in, check_out, late_minutes, late_fine,
                                    worked_minutes, leave_type, source, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?, 'manual', ?,?,?)
            """,
            (employee_id, day.isoformat(), status, check_in, check_out, late, fine, worked, leave_type,
             note, now, now),
        )
    storage.audit(actor=actor, action="mark_attendance", entity="employee", entity_id=employee_id,
                  detail=f"{day.isoformat()} → {status}")
    row = get_row(storage, employee_id, day)
    assert row is not None
    return row


class LeaveDayConflict(employees_service.ValidationError):
    """Raised when a correction would overwrite an approved leave day without confirmation."""


def _release_leave_day(storage: Storage, row: dict, *, actor: str | None = None) -> None:
    """A leave day is being overwritten: give that day back to the balance (LV-07)."""
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (row["leave_id"],))
    if leave is None or leave["status"] != "approved":
        return
    charged = 0.5 if row["status"] == orgtime.STATUS_HALF_DAY else 1.0
    remaining = round(float(leave["days"]) - charged, 2)
    if remaining <= 0:
        storage.execute("UPDATE leave_requests SET status = 'cancelled', decided_at = ? WHERE id = ?",
                        (utcnow_iso(), leave["id"]))
    else:
        storage.execute("UPDATE leave_requests SET days = ? WHERE id = ?", (remaining, leave["id"]))
    storage.audit(actor=actor, action="leave_day_released", entity="employee", entity_id=row["employee_id"],
                  detail=f"leave #{leave['id']} {row['work_date']} → {remaining:g} day(s) left")


def clear_day(storage: Storage, *, employee_id: int, work_date: str, actor: str | None = None) -> None:
    locks.assert_period_open(storage, work_date)
    existing = get_row(storage, employee_id, work_date)
    if existing and existing.get("leave_id"):
        _release_leave_day(storage, existing, actor=actor)
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
        status_filter += " AND e.department = ? COLLATE NOCASE"
        params: list = [department.strip()]
    else:
        params = []
    staff = storage.query(
        f"SELECT e.id, e.code, e.name, e.department, e.designation, e.joined_on FROM employees e {status_filter} "
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
    marks_per_half_lop = orgtime.late_marks_rule(storage)
    for person in staff:
        window = employment_window(storage, person, start, end)
        tally = {key: 0 for key in DAY_CLASSES}
        totals = {"late": 0, "worked_minutes": 0}
        cells = []
        for day, day_date in zip(days, orgtime.each_day(start, end)):
            record = rows_by_key.get((person["id"], day["iso"]))
            kind = (orgtime.STATUS_HOLIDAY if day["is_holiday"]
                    else orgtime.STATUS_WEEK_OFF if day["is_week_off"] else "working")
            cls = classify_day(record, kind, day_date, today_date, window)
            tally[cls] += 1
            cell = {"status": _CLASS_STATUS[cls], "cls": cls, "code": _CLASS_CODE[cls], "css": _CLASS_CSS[cls]}
            if record and cls != "not_employed":
                cell.update(
                    check_in=orgtime.format_clock(record["check_in"]),
                    check_out=orgtime.format_clock(record["check_out"]),
                    late_minutes=record["late_minutes"], worked_minutes=record["worked_minutes"],
                    leave_type=record["leave_type"], note=record["note"], title=_cell_title(record),
                )
                if record["late_minutes"]:
                    totals["late"] += 1
                if record["worked_minutes"]:
                    totals["worked_minutes"] += int(record["worked_minutes"])
            else:
                cell["title"] = {
                    "week_off": "Weekly off", "holiday": day["holiday_name"] or "Holiday",
                    "pending": "Not marked yet", "future": "Not marked yet",
                    "absent": "Absent (no scan, no leave)", "not_employed": "Not employed (before joining / after leaving)",
                }[cls]
            cells.append(cell)

        figures = pay_figures(tally, totals["late"], marks_per_half_lop)
        totals.update(
            present=tally["present"], half_day=tally["half_paid"] + tally["half_unpaid"],
            leave=tally["leave_paid"] + tally["leave_unpaid"], unpaid_leave=tally["leave_unpaid"],
            paid_leave=tally["leave_paid"], absent=tally["absent"], not_employed=tally["not_employed"],
            payable_days=figures["present_days"] + figures["paid_leave_days"], lop_days=figures["lop_days"],
        )
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


_CLASS_STATUS = {
    "present": orgtime.STATUS_PRESENT, "half_paid": orgtime.STATUS_HALF_DAY, "half_unpaid": orgtime.STATUS_HALF_DAY,
    "leave_paid": orgtime.STATUS_LEAVE, "leave_unpaid": orgtime.STATUS_LEAVE, "absent": orgtime.STATUS_ABSENT,
    "week_off": orgtime.STATUS_WEEK_OFF, "holiday": orgtime.STATUS_HOLIDAY, "pending": orgtime.STATUS_PENDING,
    "future": orgtime.STATUS_PENDING, "not_employed": orgtime.STATUS_NOT_EMPLOYED,
}
_CLASS_CODE = {
    "present": "P", "half_paid": "½", "half_unpaid": "½", "leave_paid": "L", "leave_unpaid": "L",
    "absent": "A", "week_off": "W", "holiday": "H", "pending": "·", "future": "·", "not_employed": "–",
}
_CLASS_CSS = {
    "present": "P", "half_paid": "half", "half_unpaid": "half", "leave_paid": "leave", "leave_unpaid": "leave",
    "absent": "A", "week_off": "W", "holiday": "H", "pending": "pending", "future": "pending",
    "not_employed": "ne",
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
        bits.append(f"late {record['late_minutes']}m" + (f" · fine ₹{record['late_fine']:g}" if record.get("late_fine") else ""))
    if record.get("worked_minutes"):
        bits.append(orgtime.format_minutes(record["worked_minutes"]))
    if record.get("leave_type"):
        bits.append(orgtime.LEAVE_LABELS.get(record["leave_type"], record["leave_type"]))
    label = orgtime.source_label(record)
    if label:
        bits.append(label)
    if orgtime.user_note(record):
        bits.append(orgtime.user_note(record))
    return " · ".join(bits)


def register_csv(register: dict) -> str:
    """Flat CSV of the monthly register (one row per employee per day)."""
    lines = ["\ufeffEmployee Code,Employee Name,Department,Date,Status,Check In,Check Out,Late (min),Worked,Leave Type,Note"]
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
