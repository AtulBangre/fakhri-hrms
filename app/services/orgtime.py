"""Local-time and calendar helpers.

Attendance and payroll are inherently *local* concepts: the working day starts at
09:30 in the company's timezone, not in UTC. Everything in the HRMS therefore
stores local dates (``YYYY-MM-DD``) and local clock times (``HH:MM:SS``), and the
timezone comes from ``HRMS_TZ`` (default Asia/Kolkata).
"""

from __future__ import annotations

import calendar
from functools import lru_cache
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
#: Before the joining date or after the last working day: neither paid nor absent.
STATUS_NOT_EMPLOYED = "not_employed"

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
    STATUS_NOT_EMPLOYED: "Not employed",
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


def parse_period(value: str | None) -> str | None:
    """Accept ``2026-09``, ``2026-9``, ``09/2026`` or ``9-2026`` → ``'2026-09'``; ``None`` if invalid.

    Browsers without ``<input type="month">`` (Firefox desktop, older Safari) send
    whatever the person typed, so the server has to be forgiving.
    """
    text = (value or "").strip().replace(" ", "")
    if not text:
        return None
    for sep in ("-", "/", "."):
        if sep in text:
            left, _, right = text.partition(sep)
            break
    else:
        return None
    if not (left.isdigit() and right.isdigit()):
        return None
    year, month = (int(left), int(right)) if len(left) == 4 else (int(right), int(left))
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        return None
    return f"{year}-{month:02d}"


def fiscal_year(period: str) -> str:
    """``'2026-03'`` → ``'2025-26'`` (the Indian financial year runs April → March)."""
    year, month = (int(part) for part in period.split("-")[:2])
    start = year if month >= 4 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def fiscal_year_months(fy: str) -> list[str]:
    """The twelve ``YYYY-MM`` periods of a financial year, April first."""
    start = int(fy.split("-")[0])
    return [f"{start}-{m:02d}" for m in range(4, 13)] + [f"{start + 1}-{m:02d}" for m in range(1, 4)]


def days_in_period(period: str) -> list[date]:
    start, end = month_bounds(period)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def each_day(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# ------------------------------------------------------------------ calendar
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def parse_weekdays(raw: str | None) -> set[int]:
    """``"5,6"`` → ``{5, 6}``. Unknown tokens are ignored; empty means no week off."""
    days: set[int] = set()
    for token in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(float(token))
        except ValueError:
            continue
        if 0 <= value <= 6:
            days.add(value)
    return days


def week_off_weekdays(storage: Storage) -> set[int]:
    """Weekdays that are always off (Monday=0 … Sunday=6)."""
    return parse_weekdays(storage.get_setting("week_off_days", "6"))


def saturday_off_weeks(storage: Storage) -> set[int]:
    """Which Saturdays of the month are off, e.g. ``{2, 4}`` for 2nd & 4th."""
    return {week for week in parse_weekdays(storage.get_setting("saturday_off_weeks", "")) if 1 <= week <= 5}


def is_week_off(storage: Storage, day: date) -> bool:
    if day.weekday() in week_off_weekdays(storage):
        return True
    # Alternate Saturdays: the Nth Saturday of the month (1st = days 1-7, ...)
    return day.weekday() == 5 and ((day.day - 1) // 7 + 1) in saturday_off_weeks(storage)


def week_off_label(storage: Storage) -> str:
    """Human summary such as ``"Sunday + 2nd & 4th Saturday"``."""
    parts = [WEEKDAY_NAMES[d] for d in sorted(week_off_weekdays(storage))]
    weeks = sorted(saturday_off_weeks(storage))
    if weeks and 5 not in week_off_weekdays(storage):
        ordinal = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
        parts.append(" & ".join(ordinal[w] for w in weeks) + " Saturday")
    return " + ".join(parts) or "No weekly off"


# --------------------------------------------------------------- day source
_SYSTEM_NOTES = ("leave #", "regularized:", "demo data", "face scan overrode a manual entry")


def source_label(record: dict | None) -> str | None:
    """How a day was marked, in words people understand (same wording everywhere).

    ``face`` → "Gate scanner · Main Gate", ``field`` → "Field (GPS) · Client site",
    ``manual`` → "Marked by HR" (or "Corrected on request" for an approved
    regularization), ``leave`` → "Leave".
    """
    if not record or not record.get("status"):
        return None
    source = record.get("source") or ""
    device = (record.get("device_label") or "").strip()
    note = (record.get("note") or "").strip().lower()
    if source == "face":
        return "Gate scanner" + (f" · {device}" if device else "")
    if source == "field":
        place = device.split("·", 1)[1].strip() if "·" in device else ""
        return "Field (GPS)" + (f" · {place}" if place else "")
    if source == "leave":
        return "Leave"
    if source == "manual":
        return "Corrected on request" if note.startswith("regularized:") else "Marked by HR"
    return None


def user_note(record: dict | None) -> str | None:
    """The note a person typed — system bookkeeping notes are hidden."""
    note = ((record or {}).get("note") or "").strip()
    if not note or note.lower().startswith(_SYSTEM_NOTES):
        return None
    return note


def holiday_name(storage: Storage, day: date) -> str | None:
    return storage.holidays().get(day.isoformat())


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


@lru_cache(maxsize=4096)
def parse_clock(value: str | None) -> time | None:
    """``'09:30'`` / ``'09:30:15'`` → ``time``. Cached: registers parse the same few
    hundred clock strings tens of thousands of times, and ``strptime`` is slow."""
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() and len(part) <= 2 for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


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


@lru_cache(maxsize=4096)
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


LATE_POLICIES = ("none", "lop_count_only", "tiered_fine_only", "hybrid_both")


def late_policy(storage: Storage) -> str:
    mode = storage.get_setting("late_policy_mode", "tiered_fine_only")
    return mode if mode in LATE_POLICIES else "tiered_fine_only"


def late_assessment(storage: Storage, moment: datetime) -> dict:
    """Late minutes, fine and automatic half day for a check-in at ``moment`` (Feature 4).

    * ``late_minutes`` counts from shift start + grace (as before).
    * Fines: slab 1 up to ``late_slab_1_minutes`` late, slab 2 for anything later.
    * Arriving ``auto_half_day_late_minutes`` or more after **shift start** turns the
      day into a half day (0.5 LOP) instead of a fine.
    * Policy ``none`` records lateness but has no consequence; ``lop_count_only``
      keeps the old "every N late marks" rule only (applied in payroll).
    """
    policy = late_policy(storage)
    arrival = moment.hour * 60 + moment.minute
    start = shift_start_minutes(storage)
    late = max(0, arrival - (start + storage.get_int("late_grace_minutes", 15)))
    cutoff = storage.get_int("auto_half_day_late_minutes", 60)
    half_day = policy != "none" and cutoff > 0 and arrival >= start + cutoff
    fine = 0.0
    if late and not half_day and policy in ("tiered_fine_only", "hybrid_both"):
        slab_1 = storage.get_int("late_slab_1_minutes", 15)
        fine = storage.get_float("late_slab_1_penalty", 50) if late <= slab_1 else storage.get_float("late_slab_2_penalty", 100)
    return {"late_minutes": late, "fine": round(max(0.0, fine), 2), "half_day": half_day, "policy": policy}


def late_marks_rule(storage: Storage) -> int:
    """The "every N late marks = half a day" rule, only in the policies that use it."""
    if late_policy(storage) not in ("lop_count_only", "hybrid_both"):
        return 0
    return storage.get_int("late_marks_per_half_lop", 0)


def human_date(day: date | str) -> str:
    parsed = parse_date(day)
    return parsed.strftime("%d %b %Y")


def human_day(day: date | str) -> str:
    parsed = parse_date(day)
    return parsed.strftime("%a, %d %b")
