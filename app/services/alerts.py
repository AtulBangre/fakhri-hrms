"""Scheduled alerts: celebrations, attendance reminders and the HR daily summary.

``run_due`` is called from the notification worker about once a minute. Each job
runs once per day. A job claims its day with one atomic insert into ``app_settings``,
so a second worker or a restart cannot send anything twice.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from .. import notify
from ..config import Settings
from ..db import Storage
from . import attendance as attendance_service
from . import orgtime, wishes

logger = logging.getLogger("hrms.alerts")

REMINDER_DAYS = (3, 1, 0)      # HR hears about a celebration this many days before
DASHBOARD_DAYS = 3
REMINDER_WINDOW_HOURS = 3      # after a long outage, an old attendance nudge is noise
CHECKOUT_REMINDER_MINUTES = 30
SUMMARY_AFTER_START_MINUTES = 60
CLAIM_PREFIX = "alert_day:"
NOTIFICATION_RETENTION_DAYS = 30   # permanently delete notifications (push, email, whatsapp) older than 30 days
AUDIT_LOG_RETENTION_DAYS = 120     # permanently delete audit logs older than 120 days
INBOX_RETENTION_DAYS = 30          # backward-compatibility
OFFLINE_SCAN_ID_DAYS = 14         # long enough to outlast any scan a device may still resend

KIND_LABELS = {"birthday": "Birthday", "anniversary": "Anniversary", "work": "Work anniversary"}


# ---------------------------------------------------------------- celebrations
def _occurrence(value: str | None, year: int) -> date | None:
    """This year's date for a stored date. 29 Feb falls on 28 Feb in other years."""
    if not value:
        return None
    try:
        original = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    try:
        return original.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def upcoming(storage: Storage, today: date, days: int = DASHBOARD_DAYS) -> list[dict[str, Any]]:
    """Active employees' celebrations from ``today`` to ``days`` ahead, soonest first."""
    people = storage.query(
        "SELECT id, code, name, department, email, photo_filename, date_of_birth, anniversary, joined_on "
        "FROM employees WHERE status = 'active' ORDER BY name COLLATE NOCASE"
    )
    found = []
    for person in people:
        for kind, column in (("birthday", "date_of_birth"), ("anniversary", "anniversary"), ("work", "joined_on")):
            for year in (today.year, today.year + 1):  # 30 Dec looks ahead into January
                when = _occurrence(person.get(column), year)
                if when is None or not 0 <= (when - today).days <= days:
                    continue
                years = year - int(str(person[column])[:4])
                if kind == "work" and years < 1:
                    continue
                found.append({
                    "employee": person, "kind": kind, "label": KIND_LABELS[kind], "date": when,
                    "in_days": (when - today).days, "years": years,
                    "when_label": {0: "Today", 1: "Tomorrow"}.get((when - today).days,
                                                                 f"In {(when - today).days} days"),
                })
    return sorted(found, key=lambda item: (item["in_days"], item["employee"]["name"].lower()))


def _send_wishes(storage: Storage, settings: Settings, today: date) -> int:
    """Today's wishes: an email from HR and a push to each person celebrating."""
    used: set[str] = set()
    sent = 0
    for item in upcoming(storage, today, 0):
        person, kind = item["employee"], item["kind"]
        first = (person["name"] or "there").split(" ")[0]
        if kind == "birthday":
            template, title = "birthday", f"Happy Birthday, {first}!"
            ctx = {
                "opener": wishes.pick(wishes.BIRTHDAY_OPENERS, person["id"], today.year),
                "wish": wishes.pick(wishes.BIRTHDAY_WISHES, person["id"], today.year, used),
                "closer": wishes.pick(wishes.BIRTHDAY_CLOSERS, person["id"], today.year),
            }
        elif kind == "anniversary":
            template, title = "anniversary", f"Happy Anniversary, {first}!"
            ctx = {"wish": wishes.pick(wishes.ANNIVERSARY_WISHES, person["id"], today.year, used)}
        else:
            template, title = "work_anniversary", f"Happy {_ordinal(item['years'])} work anniversary, {first}!"
            ctx = {"years": item["years"],
                   "wish": wishes.pick(wishes.WORK_ANNIVERSARY_WISHES, person["id"], today.year, used)}
        if settings.notify_email_enabled and person.get("email"):
            notify.queue_email(storage, person["email"], template, settings=settings, category=template,
                               name=first, title=title, **ctx)
            sent += 1
        notify.push_employee(storage, settings, person["id"], title, ctx["wish"], "/me", category=template)
        if getattr(settings, "notify_whatsapp_celebrations", True) and person.get("phone"):
            if kind == "birthday":
                notify.queue_whatsapp(storage, person["phone"], "birthday_wish", settings=settings, name=first)
            else:
                years_label = f"{_ordinal(item.get('years', 1))} " if item.get("years") else ""
                notify.queue_whatsapp(storage, person["phone"], "anniversary_wish", settings=settings, name=first, years=years_label)
    return sent


def _remind_hr(storage: Storage, settings: Settings, today: date) -> int:
    """Tell HR, by email and push, what is coming up in 3 days, tomorrow and today."""
    items = [item for item in upcoming(storage, today, max(REMINDER_DAYS)) if item["in_days"] in REMINDER_DAYS]
    for item in items:
        person = item["employee"]
        extra = f" ({_ordinal(item['years'])} year)" if item["kind"] == "work" else ""
        title = f"{item['label']}: {person['name']} — {item['when_label'].lower()}"
        message = (f"{person['name']} ({person['code']}"
                   + (f", {person['department']}" if person.get("department") else "")
                   + f"): {item['label'].lower()}{extra} on {orgtime.human_date(item['date'])}.")
        notify.notify_hr(storage, settings, "hr_alert", category="celebration_reminder", subject=title,
                         message=message, link="/dashboard")
    return len(items)


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


# ------------------------------------------------------------------ attendance
def _minutes(value: str | None) -> int | None:
    parsed = orgtime.parse_clock(value)
    return parsed.hour * 60 + parsed.minute if parsed else None


def _attendance_reminders(storage: Storage, settings: Settings, now: datetime) -> None:
    """Per shift: nudge people with no check-in, and people who forgot to check out."""
    today = now.date()
    if orgtime.day_kind(storage, today) != "working":
        return
    clock = now.hour * 60 + now.minute
    people = storage.query(
        "SELECT e.id, e.name, e.shift_id, e.phone FROM employees e JOIN users u ON u.employee_id = e.id "
        "WHERE e.status = 'active' AND u.active = 1 AND e.joined_on <= ?",
        (today.isoformat(),),
    )
    rows = {row["employee_id"]: row for row in storage.query(
        "SELECT employee_id, status, check_in, check_out FROM attendance WHERE work_date = ?", (today.isoformat(),)
    )}
    shifts = orgtime.shifts_by_id(storage)
    groups: dict[Any, list[dict]] = {}
    for person in people:
        groups.setdefault(person["shift_id"] or "default", []).append(person)

    wa_reminders_val = storage.get_setting("notify_whatsapp_reminders")
    wa_reminders_enabled = getattr(settings, "notify_whatsapp_reminders", True) if wa_reminders_val is None else (str(wa_reminders_val).strip() in ("1", "true", "True", "yes"))

    for shift_key, members in groups.items():
        shift = orgtime.shift_for(storage, members[0], shifts)
        start, end = _minutes(shift["start_time"]), _minutes(shift["end_time"])
        if start is None or end is None:
            continue
        due_in = start + int(shift["grace_minutes"] or 0)
        if 0 <= clock - due_in <= REMINDER_WINDOW_HOURS * 60 and _claim(storage, f"missed_in:{shift_key}", today):
            for person in members:
                if person["id"] not in rows:
                    notify.push_employee(storage, settings, person["id"], "You have not checked in yet",
                                         f"Your shift started at {shift['start_time']}. Scan at the gate when you arrive.",
                                         "/me", category="attendance_reminder")
                    if wa_reminders_enabled and person.get("phone"):
                        notify.queue_whatsapp(
                            storage, person["phone"], "attendance_reminder", settings=settings,
                            message=f"Reminder: Your shift started at {shift['start_time']}. Please mark your attendance.",
                        )
        due_out = end + CHECKOUT_REMINDER_MINUTES
        if 0 <= clock - due_out <= REMINDER_WINDOW_HOURS * 60 and _claim(storage, f"missed_out:{shift_key}", today):
            for person in members:
                row = rows.get(person["id"])
                if row and row.get("check_in") and not row.get("check_out"):
                    notify.push_employee(storage, settings, person["id"], "Don't forget to check out",
                                         f"Your shift ended at {shift['end_time']}. Scan at the gate before you leave.",
                                         "/me", category="attendance_reminder")
                    if wa_reminders_enabled and person.get("phone"):
                        notify.queue_whatsapp(
                            storage, person["phone"], "attendance_reminder", settings=settings,
                            message=f"Reminder: Your shift ended at {shift['end_time']}. Please mark check-out before leaving.",
                        )


def _hr_summary(storage: Storage, settings: Settings, now: datetime) -> None:
    today = now.date()
    if orgtime.day_kind(storage, today) != "working":
        return
    start = _minutes(storage.get_setting("shift_start", "09:30"))
    if start is None or now.hour * 60 + now.minute < start + SUMMARY_AFTER_START_MINUTES:
        return
    if not _claim(storage, "hr_summary", today):
        return
    counts = attendance_service.dashboard(storage, settings, today)["counts"]
    notify.push_hr(storage, settings, "Today's attendance",
                   f"{counts['present']} present, {counts['late']} late, {counts['half_day']} half day, "
                   f"{counts['leave']} on leave, {counts['not_marked']} not marked yet.",
                   "/attendance", category="attendance_summary")
    hr_phone = storage.get_setting("hr_whatsapp_phone", getattr(settings, "hr_whatsapp_phone", ""))
    wa_att_val = storage.get_setting("notify_whatsapp_attendance")
    wa_attendance_enabled = getattr(settings, "notify_whatsapp_attendance", True) if wa_att_val is None else (str(wa_att_val).strip() in ("1", "true", "True", "yes"))
    if hr_phone and wa_attendance_enabled:
        notify.queue_whatsapp(
            storage, hr_phone, "hr_attendance_summary", settings=settings,
            date=orgtime.human_date(today),
            present=counts['present'], late=counts['late'],
            half_day=counts['half_day'], leave=counts['leave'],
            not_marked=counts['not_marked'],
        )


# --------------------------------------------------------------------- running
def _claim(storage: Storage, job: str, today: date) -> bool:
    """True for exactly one caller per job and day, across every worker."""
    cursor = storage.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, '1')",
        (f"{CLAIM_PREFIX}{today.isoformat()}:{job}",),
    )
    return (cursor.rowcount or 0) == 1


claim = _claim


def _housekeeping(storage: Storage, today: date) -> dict[str, int]:
    """Keep transient logs, claims, and expired tokens from growing forever.
    Permanently deletes:
    - All notifications (push, email, whatsapp) older than 30 days
    - Audit logs older than 120 days
    - Expired or already-used password tokens and OTP codes
    - Login rate-limit attempts older than 24 hours
    """
    import time
    from datetime import timezone

    purged: dict[str, int] = {}
    keep_from = (today - timedelta(days=2)).isoformat()
    for row in storage.query("SELECT key FROM app_settings WHERE key LIKE ?", (f"{CLAIM_PREFIX}%",)):
        if row["key"][len(CLAIM_PREFIX):][:10] < keep_from:
            storage.execute("DELETE FROM app_settings WHERE key = ?", (row["key"],))

    keep_scans_from = (today - timedelta(days=OFFLINE_SCAN_ID_DAYS)).isoformat()
    for row in storage.query("SELECT key FROM app_settings WHERE key LIKE 'offline_scan:%'"):
        if row["key"].split(":")[1] < keep_scans_from:
            storage.execute("DELETE FROM app_settings WHERE key = ?", (row["key"],))

    # 1. Notifications: permanently delete all channels older than 30 days
    notif_cutoff = (today - timedelta(days=NOTIFICATION_RETENTION_DAYS)).isoformat()
    cur = storage.execute("DELETE FROM notifications WHERE created_at < ?", (notif_cutoff,))
    purged["notifications"] = cur.rowcount

    # 2. Audit log: permanently delete records older than 120 days
    audit_cutoff = (today - timedelta(days=AUDIT_LOG_RETENTION_DAYS)).isoformat()
    cur = storage.execute("DELETE FROM audit_log WHERE at < ?", (audit_cutoff,))
    purged["audit_log"] = cur.rowcount

    # 3. Password tokens: permanently delete expired or already-used tokens
    now_epoch = time.time()
    cur = storage.execute("DELETE FROM password_tokens WHERE expires_at < ? OR used_at IS NOT NULL", (now_epoch,))
    purged["password_tokens"] = cur.rowcount

    # 4. OTP codes: permanently delete expired or already-used codes
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = storage.execute("DELETE FROM otp_codes WHERE expires_at < ? OR used_at IS NOT NULL", (now_iso,))
    purged["otp_codes"] = cur.rowcount

    # 5. Login attempts: delete attempts older than 1 day
    cur = storage.execute("DELETE FROM login_attempts WHERE at < ?", (now_epoch - 86400,))
    purged["login_attempts"] = cur.rowcount

    logger.info("Housekeeping completed: %s", purged)
    return purged


def run_due(storage: Storage, settings: Settings, now: datetime | None = None) -> None:
    """Run whatever is due. One failing job must not stop the others."""
    now = now or orgtime.local_now(settings)
    today = now.date()
    jobs = []
    if settings.notify_celebrations and now.hour >= settings.celebration_hour:
        jobs.append(("wishes", lambda: _claim(storage, "wishes", today) and _send_wishes(storage, settings, today)))
        jobs.append(("hr_reminders", lambda: _claim(storage, "hr_reminders", today) and _remind_hr(storage, settings, today)))
    if settings.notify_push:
        jobs.append(("attendance", lambda: _attendance_reminders(storage, settings, now)))
        jobs.append(("hr_summary", lambda: _hr_summary(storage, settings, now)))
    jobs.append(("housekeeping", lambda: _claim(storage, "housekeeping", today) and _housekeeping(storage, today)))
    for name, job in jobs:
        try:
            job()
        except Exception:
            logger.exception("alert job '%s' failed", name)
