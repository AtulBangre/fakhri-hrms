"""Celebrations service: birthdays, wedding anniversaries, and work anniversaries."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..db import Storage
from . import orgtime

KIND_LABELS = {
    "birthday": "Birthday",
    "anniversary": "Wedding anniversary",
    "work": "Work anniversary",
}

KIND_ICONS = {
    "birthday": "🎂",
    "anniversary": "💍",
    "work": "🏢",
}


def _occurrence(value: str | None, year: int) -> date | None:
    """Calculate the date of this celebration in the specified year.
    Feb 29 falls on Feb 28 in non-leap years.
    """
    if not value:
        return None
    try:
        orig = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    try:
        return orig.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def _ordinal(n: int) -> str:
    """Return 1st, 2nd, 3rd, 4th, etc."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def list_celebrations(
    storage: Storage,
    *,
    today: date,
    from_date: date | None = None,
    to_date: date | None = None,
    kind: str = "birthday",  # "birthday", "all", "anniversary", "work"
    department: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """List celebrations matching the filters.
    
    If neither from_date nor to_date is provided (default mode),
    finds the next upcoming occurrence for every employee and sorts
    by nearest first (e.g. today, tomorrow, next week, ...).
    """
    query = (
        "SELECT id, code, name, department, designation, email, phone, photo_filename, "
        "date_of_birth, anniversary, joined_on "
        "FROM employees WHERE status = 'active'"
    )
    params: list[Any] = []
    if department:
        query += " AND department = ?"
        params.append(department)
    query += " ORDER BY name COLLATE NOCASE"

    people = storage.query(query, tuple(params))
    search_term = (search or "").strip().lower()

    # Determine which event types to check
    kinds_to_check: list[tuple[str, str]] = []
    if kind in ("birthday", "all"):
        kinds_to_check.append(("birthday", "date_of_birth"))
    if kind in ("anniversary", "all"):
        kinds_to_check.append(("anniversary", "anniversary"))
    if kind in ("work", "all"):
        kinds_to_check.append(("work", "joined_on"))

    results: list[dict[str, Any]] = []

    has_date_range = from_date is not None or to_date is not None
    effective_from = from_date or today
    effective_to = to_date or (effective_from + timedelta(days=365))

    for person in people:
        p_name = person.get("name") or ""
        p_code = person.get("code") or ""
        p_dept = person.get("department") or ""
        if search_term and (search_term not in p_name.lower() and search_term not in p_code.lower() and search_term not in p_dept.lower()):
            continue

        for evt_kind, col in kinds_to_check:
            raw_val = person.get(col)
            if not raw_val:
                continue

            try:
                orig_date = date.fromisoformat(str(raw_val)[:10])
            except ValueError:
                continue

            if has_date_range:
                # Check all years spanned by [effective_from, effective_to]
                min_yr = effective_from.year
                max_yr = effective_to.year
                for yr in range(min_yr, max_yr + 1):
                    occ = _occurrence(raw_val, yr)
                    if occ is None:
                        continue
                    if effective_from <= occ <= effective_to:
                        years = yr - orig_date.year
                        if evt_kind == "work" and years < 1:
                            continue
                        in_days = (occ - today).days
                        results.append(_build_item(person, evt_kind, occ, orig_date, years, in_days))
            else:
                # Default mode: find the NEXT upcoming occurrence on or after today
                this_yr_occ = _occurrence(raw_val, today.year)
                if this_yr_occ is not None and this_yr_occ >= today:
                    next_occ = this_yr_occ
                else:
                    next_occ = _occurrence(raw_val, today.year + 1)

                if next_occ is None:
                    continue

                years = next_occ.year - orig_date.year
                if evt_kind == "work" and years < 1:
                    continue
                in_days = (next_occ - today).days
                results.append(_build_item(person, evt_kind, next_occ, orig_date, years, in_days))

    # Sort nearest first by in_days, then by name
    return sorted(results, key=lambda x: (x["in_days"], x["date"], x["employee"]["name"].lower()))


def _build_item(
    person: dict[str, Any],
    kind: str,
    occ: date,
    orig_date: date,
    years: int,
    in_days: int,
) -> dict[str, Any]:
    """Construct standard celebration record dictionary."""
    if in_days == 0:
        when_label = "Today! 🎉"
    elif in_days == 1:
        when_label = "Tomorrow"
    elif in_days < 0:
        when_label = f"{abs(in_days)} days ago"
    else:
        when_label = f"In {in_days} days"

    if kind == "birthday":
        milestone = f"Turning {years}"
        orig_label = f"Born {orig_date.strftime('%d %b %Y')}"
    elif kind == "anniversary":
        milestone = f"{_ordinal(years)} Wedding Anniversary"
        orig_label = f"Married {orig_date.strftime('%d %b %Y')}"
    else:
        milestone = f"Completing {years} year{'s' if years != 1 else ''}"
        orig_label = f"Joined {orig_date.strftime('%d %b %Y')}"

    # WhatsApp pre-filled greeting message
    first_name = (person.get("name") or "there").split()[0]
    if kind == "birthday":
        greeting = f"Wishing you a very Happy Birthday, {first_name}! 🎂 Wishing you a wonderful year ahead."
    elif kind == "anniversary":
        greeting = f"Happy Wedding Anniversary, {first_name}! 💍 Wishing you both a lifetime of happiness."
    else:
        greeting = f"Congratulations on completing {years} year{'s' if years != 1 else ''} with Fakhri IT Services, {first_name}! 🏢"

    phone = str(person.get("phone") or "").strip().replace(" ", "").replace("-", "")
    if phone and not phone.startswith("+"):
        if len(phone) == 10:
            phone = "91" + phone
        elif phone.startswith("0") and len(phone) == 11:
            phone = "91" + phone[1:]

    return {
        "employee": person,
        "kind": kind,
        "icon": KIND_ICONS.get(kind, "🎉"),
        "label": KIND_LABELS.get(kind, "Celebration"),
        "date": occ,
        "original_date": orig_date,
        "years": years,
        "in_days": in_days,
        "when_label": when_label,
        "milestone": milestone,
        "original_label": orig_label,
        "greeting": greeting,
        "phone_clean": phone,
    }


def celebration_stats(storage: Storage, today: date) -> dict[str, Any]:
    """Summary counts for the celebration dashboard."""
    active_employees = storage.query(
        "SELECT id, date_of_birth, anniversary, joined_on FROM employees WHERE status = 'active'"
    )
    total_active = len(active_employees)
    with_dob = sum(1 for e in active_employees if e.get("date_of_birth"))
    missing_dob = total_active - with_dob

    # Lookups for today, next 7 days, and current month
    today_count = 0
    next_7_count = 0
    this_month_count = 0

    month_start = today.replace(day=1)
    # last day of month
    if today.month == 12:
        next_month_start = date(today.year + 1, 1, 1)
    else:
        next_month_start = date(today.year, today.month + 1, 1)
    month_end = next_month_start - timedelta(days=1)

    for person in active_employees:
        for col, kind in (("date_of_birth", "birthday"), ("anniversary", "anniversary"), ("joined_on", "work")):
            raw = person.get(col)
            if not raw:
                continue
            occ = _occurrence(raw, today.year)
            if occ is None:
                continue
            years = today.year - int(str(raw)[:4])
            if kind == "work" and years < 1:
                continue

            diff = (occ - today).days
            if diff == 0:
                today_count += 1
            if 0 <= diff <= 7:
                next_7_count += 1
            if month_start <= occ <= month_end:
                this_month_count += 1

    return {
        "total_active": total_active,
        "with_dob": with_dob,
        "missing_dob": missing_dob,
        "today_count": today_count,
        "next_7_count": next_7_count,
        "this_month_count": this_month_count,
    }


def missing_dob_employees(storage: Storage) -> list[dict[str, Any]]:
    """Active employees who have no date of birth on record."""
    return storage.query(
        "SELECT id, code, name, department, designation, email, phone, joined_on "
        "FROM employees WHERE status = 'active' AND (date_of_birth IS NULL OR TRIM(date_of_birth) = '') "
        "ORDER BY name COLLATE NOCASE"
    )

