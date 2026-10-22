"""Local-time and calendar helpers.

Attendance and payroll are inherently *local* concepts: the working day starts at
09:30 in the company's timezone, not in UTC. Everything in the HRMS therefore
stores local dates (``YYYY-MM-DD``) and local clock times (``HH:MM:SS``), and the
timezone comes from ``HRMS_TZ`` (default Asia/Kolkata).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

from ..config import Settings
from ..db import Storage

STATUS_PRESENT = "present"
STATUS_ABSENT = "absent"
STATUS_HALF_DAY = "half_day"
STATUS_LEAVE = "leave"
STATUS_WEEK_OFF = "week_off"
STATUS_HOLIDAY = "holiday"
STATUS_PENDING = "pending"

ALL_STATUSES = (
    STATUS_PRESENT,
    STATUS_ABSENT,
    STATUS_HALF_DAY,
    STATUS_LEAVE,
    STATUS_WEEK_OFF,
    STATUS_HOLIDAY,
)

#: Leave types that are paid (do not reduce salary) and count as leave balance.
PAID_LEAVE_TYPES = ("paid", "sick", "casual")
ALL_LEAVE_TYPES = ("paid", "sick", "casual", "unpaid")

LEAVE_LABELS = {
    "paid": "Paid leave",
    "sick": "Sick leave",
    "casual": "Casual leave",
    "unpaid": "Unpaid leave (LOP)",
}

STATUS_LABELS = {
    STATUS_PRESENT: "Present",
    STATUS_ABSENT: "Absent",
    STATUS_HALF_DAY: "Half day",
    STATUS_LEAVE: "On leave",
    STATUS_WEEK_OFF: "Week off",
    STATUS_HOLIDAY: "Holiday",
    STATUS_PENDING: "Not marked",
}


# --------------------------------------------------------------------- clock
def local_now(settings: Settings) -> datetime:
    return datetime.now(settings.tz)


def today(settings: Settings) -> date:
    return local_now(settings).date()


def today_str(settings: Settings) -> str:
    return today(settings).isoformat()


def period_of(day: date | str) -> str:
    """``'YYYY-MM'`` for a date or ISO date string."""
    if isinstance(day, str):
        return day[:7]
    return day.strftime("%Y-%m")


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def month_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def month_days(period: str) -> int:
    return calendar.monthrange(*(int(p) for p in period.split("-")))[1]


def month_label(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    return f"{calendar.month_name[month]} {year}"


def days_in_period(period: str) -> list[date]:
    start, end = month_bounds(period)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def each_day(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# ------------------------------------------------------------------ calendar
def is_week_off(storage: Storage, day: date) -> bool:
    return day.weekday() == storage.get_int("week_off_days", 6)


def holiday_name(storage: Storage, day: date) -> str | None:
    row = storage.query_one("SELECT name FROM holidays WHERE day = ?", (day.isoformat(),))
    return row["name"] if row else None


def day_kind(storage: Storage, day: date) -> str:
    """``holiday``, ``week_off`` or ``working`` for a date."""
    if holiday_name(storage, day):
        return STATUS_HOLIDAY
    if is_week_off(storage, day):
        return STATUS_WEEK_OFF
    return "working"


def working_days(storage: Storage, start: date, end: date) -> float:
    """Count of working days in a range (week offs and holidays excluded)."""
    return float(sum(1 for day in each_day(start, end) if day_kind(storage, day) == "working"))


def effective_status(storage: Storage, employee_id: int, day: date, *, settings: Settings) -> str:
    """Status for one employee on one day, filling in calendar defaults.

    Precedence: a stored attendance row wins; otherwise holidays and week offs are
    implied; anything else in the past counts as absent, and today/future as
    "not marked".
    """
    row = storage.query_one(
        "SELECT status FROM attendance WHERE employee_id = ? AND work_date = ?",
        (employee_id, day.isoformat()),
    )
    if row:
        return row["status"]
    kind = day_kind(storage, day)
    if kind in (STATUS_HOLIDAY, STATUS_WEEK_OFF):
        return kind
    if day < today(settings):
        return STATUS_ABSENT
    return STATUS_PENDING


# ---------------------------------------------------------------------- time
def hhmmss(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


def parse_clock(value: str | None) -> time | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def minutes_between(start: str | None, end: str | None) -> int | None:
    start_t, end_t = parse_clock(start), parse_clock(end)
    if start_t is None or end_t is None:
        return None
    start_dt = datetime.combine(date(2000, 1, 1), start_t)
    end_dt = datetime.combine(date(2000, 1, 1), end_t)
    if end_dt < start_dt:  # overnight shift
        end_dt += timedelta(days=1)
    return int((end_dt - start_dt).total_seconds() // 60)


def seconds_between(start: str | None, end: str | None) -> int | None:
    """Second-precision gap between two clock strings (for the kiosk debounce)."""
    start_t, end_t = parse_clock(start), parse_clock(end)
    if start_t is None or end_t is None:
        return None
    start_dt = datetime.combine(date(2000, 1, 1), start_t)
    end_dt = datetime.combine(date(2000, 1, 1), end_t)
    if end_dt < start_dt:  # overnight shift
        end_dt += timedelta(days=1)
    return int((end_dt - start_dt).total_seconds())


def format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def format_clock(value: str | None) -> str:
    parsed = parse_clock(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%H:%M")


def shift_start_minutes(storage: Storage) -> int:
    parsed = parse_clock(storage.get_setting("shift_start", "09:30"))
    return parsed.hour * 60 + parsed.minute if parsed else 9 * 60 + 30


def late_minutes_for(storage: Storage, moment: datetime) -> int:
    """How many minutes late a check-in at ``moment`` is (0 when on time)."""
    grace = storage.get_int("late_grace_minutes", 15)
    now_minutes = moment.hour * 60 + moment.minute
    delta = now_minutes - (shift_start_minutes(storage) + grace)
    return max(0, delta)


def human_date(day: date | str) -> str:
    parsed = parse_date(day)
    return parsed.strftime("%d %b %Y")


def human_day(day: date | str) -> str:
    parsed = parse_date(day)
    return parsed.strftime("%a, %d %b")
