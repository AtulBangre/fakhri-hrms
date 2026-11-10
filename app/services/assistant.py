"""Ask HR: answers questions from the app's own data, in English or Hinglish.

* **Read only.** Nothing here writes, and nothing leaves the server.
* **Role scoped.** An employee only ever gets their own data, and only finalised
  payslips. Everyone-else queries (who is late, payroll totals, looking up a person)
  exist in the HR handlers alone. A kiosk login gets nothing.
* **Honest.** A question it cannot match gets "I don't know that one", never a guess.

Matching works on whole words, so "please" is not mistaken for "pl".
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..auth import SessionUser
from ..config import Settings
from ..db import Storage
from . import announcements as announcement_service
from . import attendance as attendance_service
from . import employees as employees_service
from . import leave as leave_service
from . import orgtime, payroll

MAX_NAMES = 8

EMPLOYEE_SUGGESTIONS = ["My leave balance", "Today's attendance", "This month's attendance",
                        "Latest payslip", "Upcoming holidays", "Shift timing"]
HR_SUGGESTIONS = ["Today's attendance", "Who is late today?", "Pending approvals",
                  "Payroll status", "Employees missing face", "Upcoming holidays"]

GREETING = {"hi", "hii", "hello", "hey", "namaste", "namaskar", "salaam", "help", "madad", "start"}
NOTICE = {"notice", "notices", "noticeboard", "announcement", "announcements", "suchna"}
HOLIDAY = {"holiday", "holidays", "festival", "festivals", "tyohar", "tyohaar"}
LEAVE_WORDS = {"leave", "leaves", "chutti", "chhutti", "chuti", "chhuti", "chuttiyan", "chhuttiyan", "balance",
               "sick", "casual", "pl", "sl", "cl", "avkash"}
WHEN = {"kab", "next", "agli", "agla", "upcoming", "list", "calendar"}
CORRECTION = {"correction", "correct", "regularize", "regularise", "regularization", "regularisation",
              "missed", "galat", "wrong", "bhool", "bhul"}
PAY = {"payslip", "payslips", "slip", "salary", "pay", "ctc", "tankhwah", "tankha", "vetan", "pagar", "payroll",
       "bank", "ecr"}
POLICY = {"shift", "timing", "timings", "time", "grace", "policy", "rule", "rules", "niyam", "samay", "office"}
PUNCH = {"today", "todays", "aaj", "check", "checkin", "checkout", "punch", "scan", "nikal"}
ATTENDANCE = {"attendance", "hajri", "haziri", "present", "absent", "late", "hours", "month", "mahina", "mahine",
              "marked", "unmarked"}
PENDING = {"pending", "approval", "approvals", "approve", "waiting", "review", "baki", "baaki"}
PEOPLE = {"employee", "employees", "headcount", "staff", "people", "face", "faces", "enrol", "enrolled", "enrolment"}
STOP_NAMES = {"the", "and", "who", "how", "what", "today", "leave", "late", "absent", "present", "show", "ask"}

EMPLOYEE_GUIDES: list[tuple[set[str], str, str, str]] = [
    ({"tax", "tds", "form16", "itr"}, "Your tax projection, regime and Form 16 are on the Income tax page.",
     "Open income tax", "/me/tax"),
    ({"expense", "expenses", "claim", "claims", "reimbursement", "kharcha"},
     "Submit a claim on the Expenses page. HR approves it from there.", "Open expenses", "/me/expenses"),
    ({"asset", "assets", "laptop"}, "Everything issued to you is listed on the Assets page.",
     "Open assets", "/me/assets"),
    ({"appraisal", "appraisals", "review", "rating", "performance"},
     "Your self-review and ratings are on the Appraisal page.", "Open appraisal", "/me/appraisal"),
    ({"resign", "resignation", "exit", "notice", "istifa"},
     "You can submit a resignation and follow it on the Resignation page.", "Open resignation", "/me/exit"),
    ({"password"}, "You can change your password from your account page.", "Change password", "/me/password"),
    ({"profile", "phone", "address", "email", "details"},
     "Your details are on the Profile page. Changes go to HR for approval.", "Open profile", "/me/profile"),
]


def _reply(answer: str, suggestions: list[str], *actions: tuple[str, str]) -> dict[str, Any]:
    return {
        "answer": answer,
        "suggestions": suggestions,
        "actions": [{"label": label, "href": href} for label, href in actions],
    }


def _money(value: Any) -> str:
    return f"₹{float(value or 0):,.0f}"


def _number(value: Any) -> str:
    return f"{float(value or 0):g}"


def _names(rows: list[dict]) -> str:
    names = [row["name"] for row in rows[:MAX_NAMES]]
    extra = len(rows) - len(names)
    return ", ".join(names) + (f" and {extra} more" if extra > 0 else "")


# ------------------------------------------------------------------ for everyone
def _holidays(storage: Storage, settings: Settings, suggestions: list[str]) -> dict[str, Any]:
    today = orgtime.today(settings).isoformat()
    upcoming = [(day, name) for day, name in sorted(storage.holidays().items()) if day >= today][:5]
    if not upcoming:
        return _reply("No upcoming company holidays are recorded for this period.", suggestions)
    lines = "\n".join(f"• {name}: {orgtime.human_date(day)}" for day, name in upcoming)
    return _reply(f"Upcoming company holidays:\n{lines}", suggestions)


def _notices(storage: Storage, audience: str, suggestions: list[str]) -> dict[str, Any]:
    notes = announcement_service.visible_for(storage, audience)
    if not notes:
        return _reply("There are no active notices right now.", suggestions)
    lines = "\n".join(f"• {note['title']}" for note in notes)
    return _reply(f"Current notices:\n{lines}", suggestions)


def _policy(storage: Storage, settings: Settings, employee: dict | None, suggestions: list[str]) -> dict[str, Any]:
    shift = orgtime.shift_for(storage, employee)
    half_day = orgtime.format_minutes(shift["half_day_minutes"])
    label = f" ({shift['name']})" if shift["name"] else ""
    lines = [
        f"Office timing is {shift['start_time']} to {shift['end_time']}{label}.",
        f"Grace period: {shift['grace_minutes']} minutes. A later scan is marked late.",
        f"Check-out opens {orgtime.format_minutes(settings.min_checkout_minutes)} after check-in.",
        f"Working less than {half_day} counts as a half day.",
    ]
    marks = orgtime.late_marks_rule(storage)
    if marks:
        lines.append(f"Every {marks} late marks cost half a day of pay.")
    return _reply("\n".join(lines), suggestions)


# ----------------------------------------------------------------- employee only
def _my_today(storage: Storage, settings: Settings, employee: dict) -> dict[str, Any]:
    today = orgtime.today(settings)
    row = attendance_service.get_row(storage, employee["id"], today)
    kind = orgtime.day_kind(storage, today)
    actions = [("My attendance", "/me/attendance")]
    next_up = ["This month's attendance", "My leave balance", "Shift timing"]
    if row and row.get("check_in") and not row.get("check_out") and row["status"] != orgtime.STATUS_LEAVE:
        now = orgtime.hhmmss(orgtime.local_now(settings))
        opens = attendance_service.checkout_opens_at(settings, row["check_in"], now)
        text = f"You checked in at {orgtime.format_clock(row['check_in'])}."
        if row.get("late_minutes"):
            text += f" That is {row['late_minutes']} minutes late."
        text += f"\nYou can check out after {opens}." if opens else "\nScan at the gate when you leave."
    elif row and row.get("check_out"):
        text = (f"You checked in at {orgtime.format_clock(row['check_in'])} and out at "
                f"{orgtime.format_clock(row['check_out'])}. Worked {orgtime.format_minutes(row['worked_minutes'])}.")
    elif row:
        text = f"Today is marked as {orgtime.STATUS_LABELS.get(row['status'], row['status'])}."
    elif kind == orgtime.STATUS_WEEK_OFF:
        text = "Today is your weekly off, so no scan is needed."
    elif kind == orgtime.STATUS_HOLIDAY:
        text = "Today is a holiday, so no scan is needed."
    else:
        text = "You are not marked yet today. Scan at the gate when you arrive."
    return _reply(text, next_up, *actions)


def _my_month(storage: Storage, settings: Settings, employee: dict) -> dict[str, Any]:
    period = orgtime.period_of(orgtime.today(settings))
    s = attendance_service.employee_month_summary(storage, settings, employee["id"], period)
    lines = [
        f"{orgtime.month_label(period)} so far:",
        f"• Present: {_number(s['present'])}, half days: {_number(s['half_days'])}",
        f"• Absent: {_number(s['absent'])}, paid leave: {_number(s['paid_leave'])}, unpaid: {_number(s['unpaid_leave'])}",
        f"• Late marks: {s['late_marks']}" + (f" (fines {_money(s['late_fines'])})" if s["late_fines"] else ""),
        f"• Hours recorded: {orgtime.format_minutes(s['worked_minutes'])}",
    ]
    return _reply("\n".join(lines), ["Today's attendance", "How do I correct attendance?", "Latest payslip"],
                  ("Day by day", f"/me/attendance?period={period}"))


def _my_leave(storage: Storage, settings: Settings, employee: dict, tokens: set[str]) -> dict[str, Any]:
    bal = leave_service.balance(storage, settings, employee["id"])
    lines = [f"You have {_number(bal['available'])} paid leave days available ({_number(bal['total_available'])} total leaves available across all quotas)."]
    lines.append(f"Earned PL {_number(bal['accrued'])}, total leave taken {_number(bal['total_used'])}, "
                 f"waiting for approval {_number(bal['pending'])}.")
    if bal["used_unpaid"]:
        lines.append(f"Unpaid leave taken: {_number(bal['used_unpaid'])}.")
    asked = {"sick": "SL", "sl": "SL", "casual": "CL", "cl": "CL"}
    codes = {asked[word] for word in tokens & asked.keys()}
    lines += [f"{q['name']}: {_number(q['remaining'])} of {_number(q['allocated'])} left."
              for q in bal["quotas"] if q["code"] in codes]
    recent = leave_service.employee_leave_history(storage, employee["id"], limit=3)
    if recent:
        lines.append("Recent requests:")
        lines += [f"• {item['from_label']}: {item['type_label']}, {item['status']}" for item in recent]
    return _reply("\n".join(lines), ["Upcoming holidays", "This month's attendance", "Shift timing"],
                  ("Apply for leave", "/me/leave"))


def _my_payslip(storage: Storage, employee: dict) -> dict[str, Any]:
    slip = storage.query_one(
        "SELECT p.* FROM payslips p JOIN payroll_runs r ON r.id = p.run_id "
        "WHERE p.employee_id = ? AND r.status IN ('finalized','paid') ORDER BY p.period DESC LIMIT 1",
        (employee["id"],),
    )
    next_up = ["My leave balance", "This month's attendance", "Income tax"]
    if not slip:
        return _reply("You have no payslip yet. It appears here once HR finalises payroll.", next_up,
                      ("My payslips", "/me/payslips"))
    return _reply(
        f"Your latest payslip is for {orgtime.month_label(slip['period'])}.\n"
        f"Net pay {_money(slip['net_pay'])}, gross {_money(slip['monthly_gross'])}.",
        next_up,
        ("Open payslip", f"/me/payslips/{slip['id']}"),
        ("Download PDF", f"/me/payslips/{slip['id']}.pdf"),
        ("All payslips", "/me/payslips"),
    )


def _answer_employee(storage: Storage, settings: Settings, user: SessionUser, tokens: set[str]) -> dict[str, Any]:
    employee = employees_service.get_employee(storage, int(user.employee_id or 0))
    if employee is None:
        return _reply("Your employee record is not available. Please ask HR.", [])
    suggestions = EMPLOYEE_SUGGESTIONS
    first_name = (employee.get("name") or "there").split(" ")[0]

    if tokens & CORRECTION:
        return _reply("Open My attendance and choose Request correction. You can ask for any day in the last "
                      "60 days, until that month's payroll is finalised. HR then approves it.",
                      ["This month's attendance", "Today's attendance"], ("My attendance", "/me/attendance"))
    if tokens & {"field"} or {"gps", "location"} & tokens:
        mode = employees_service.self_punch_mode(storage, employee)
        if mode == "disabled":
            return _reply("Field attendance is not enabled for you. Please ask HR.", suggestions)
        return _reply("You can mark field attendance from the Field attendance page.", suggestions,
                      ("Open field attendance", "/me/field"))
    if tokens & NOTICE and not tokens & {"resign", "resignation", "period"}:
        return _notices(storage, "employees", suggestions)
    if tokens & HOLIDAY or (tokens & LEAVE_WORDS and tokens & WHEN):
        return _holidays(storage, settings, suggestions)
    if tokens & LEAVE_WORDS:
        return _my_leave(storage, settings, employee, tokens)
    if tokens & PAY:
        return _my_payslip(storage, employee)
    if tokens & POLICY:
        return _policy(storage, settings, employee, suggestions)
    if tokens & PUNCH:
        return _my_today(storage, settings, employee)
    if tokens & ATTENDANCE:
        return _my_month(storage, settings, employee)
    for words, text, label, href in EMPLOYEE_GUIDES:
        if tokens & words:
            return _reply(text, suggestions, (label, href))
    if tokens & GREETING:
        return _reply(f"Hello {first_name}! Ask me about your attendance, leave, payslips, holidays or office "
                      "timing.", suggestions)
    return _reply("I don't know that one yet. I can answer about your attendance, leave, payslips, holidays and "
                  "office timing. For anything else, please contact HR.", suggestions)


# ----------------------------------------------------------------------- HR only
def _hr_today(storage: Storage, settings: Settings, tokens: set[str]) -> dict[str, Any]:
    today = orgtime.today(settings)
    board = attendance_service.dashboard(storage, settings)
    counts, rows = board["counts"], board["rows"]
    text = (f"Today's stats ({orgtime.human_date(today)}): {counts['present']} present, {counts['late']} late, "
            f"{counts['half_day']} half day, {counts['leave']} on leave, {counts['absent']} absent, "
            f"{counts['not_marked']} not marked yet.")
    focus = [
        ({"late"}, "Late", [r for r in rows if r["is_late"]]),
        ({"absent"}, "Absent", [r for r in rows if r["effective_status"] == orgtime.STATUS_ABSENT]),
        (LEAVE_WORDS, "On leave", [r for r in rows if r["effective_status"] == orgtime.STATUS_LEAVE]),
        ({"present"}, "Present", [r for r in rows if r["effective_status"] == orgtime.STATUS_PRESENT]),
        ({"unmarked", "marked"}, "Not marked", [r for r in rows if r["effective_status"] == orgtime.STATUS_PENDING]),
    ]
    for words, label, people in focus:
        if tokens & words:
            text += f"\n{label}: {_names(people) if people else 'nobody'}."
    return _reply(text, ["Who is late today?", "Who is not marked today?", "Pending approvals"],
                  ("Open daily board", "/attendance"), ("Monthly register", "/attendance/register"))


def _hr_pending(storage: Storage) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM leave_requests WHERE status = 'pending') AS leave,
          (SELECT COUNT(*) FROM attendance_regularizations WHERE status = 'pending') AS regularizations,
          (SELECT COUNT(*) FROM profile_change_requests WHERE status = 'pending') AS profile,
          (SELECT COUNT(*) FROM expense_claims WHERE status = 'pending') AS expenses,
          (SELECT COUNT(*) FROM employee_exits WHERE status = 'requested') AS exits
        """
    ) or {}
    items = [
        ("leave", "Leave requests", "/leave"),
        ("regularizations", "Attendance corrections", "/attendance/regularizations"),
        ("profile", "Profile changes", "/profile-requests"),
        ("expenses", "Expense claims", "/expenses"),
        ("exits", "Resignations", "/exits"),
    ]
    waiting = [(label, int(row.get(key) or 0), href) for key, label, href in items if row.get(key)]
    if not waiting:
        return _reply("Nothing is waiting for approval.", ["Today's attendance", "Payroll status"])
    lines = "\n".join(f"• {label}: {count}" for label, count, _ in waiting)
    return _reply(f"Waiting for your approval:\n{lines}", ["Today's attendance", "Payroll status"],
                  *[(label, href) for label, _, href in waiting])


def _hr_payroll(storage: Storage, settings: Settings) -> dict[str, Any]:
    today = orgtime.today(settings)
    period = orgtime.period_of(today)
    run = payroll.get_run(storage, period)
    if run:
        text = (f"{run['label']} payroll is {run['status']}, with net pay of {_money(run.get('total_net'))}.")
    else:
        text = f"{orgtime.month_label(period)} payroll has not been generated yet."
    missing = attendance_service.unmarked_working_days(storage, settings, period)
    if missing:
        text += (f"\n{len(missing)} employee{'' if len(missing) == 1 else 's'} have unmarked working days this "
                 "month. Payroll counts those days as absent.")
    return _reply(text, ["Pending approvals", "Today's attendance"],
                  ("Open payroll", "/payroll"), ("Monthly register", "/attendance/register"))


def _hr_headcount(storage: Storage, tokens: set[str]) -> dict[str, Any]:
    for department in employees_service.departments(storage):
        if set(re.findall(r"[a-z0-9]+", department.lower())) <= tokens:
            count = storage.scalar("SELECT COUNT(*) FROM employees WHERE status = 'active' "
                                   "AND department = ? COLLATE NOCASE", (department,))
            return _reply(f"{department} has {count} active employee{'' if count == 1 else 's'}.",
                          ["Employees missing face", "Today's attendance"],
                          ("Open employees", f"/employees?department={quote(department)}"))
    stats = employees_service.stats(storage)
    text = (f"{stats['active']} active employees ({stats['inactive']} inactive). "
            f"{stats['face_enrolled']} have a face enrolled, {stats['missing_face']} do not.")
    missing = employees_service.employees_missing_face(storage)
    if missing:
        text += f"\nMissing face: {_names(missing)}."
    return _reply(text, ["Today's attendance", "Pending approvals"], ("Open employees", "/employees"))


def _find_people(storage: Storage, tokens: set[str]) -> list[dict]:
    people = storage.query("SELECT id, code, name FROM employees WHERE status = 'active' ORDER BY name COLLATE NOCASE")
    wanted = {t for t in tokens if len(t) >= 3 and t not in STOP_NAMES}
    # Keep only the closest names, so "Neha Patel" does not also list every Neha and Patel.
    scored = [(len(wanted & ({p["code"].lower()} | set(re.findall(r"[a-z0-9]+", p["name"].lower())))), p) for p in people]
    best = max((score for score, _ in scored), default=0)
    return [p for score, p in scored if score == best] if best else []


def _hr_person(storage: Storage, settings: Settings, person: dict) -> dict[str, Any]:
    today = orgtime.today(settings)
    row = next((r for r in attendance_service.daily_rows(storage, settings, today)
                if r["employee_id"] == person["id"]), None)
    bal = leave_service.balance(storage, settings, person["id"])
    text = f"{person['name']} ({person['code']})"
    if row:
        text += (f"\nToday: {row['status_label']}, in {row['check_in_label']}, out {row['check_out_label']}"
                 + (f", {row['late_minutes']} minutes late." if row["late_minutes"] else "."))
    text += f"\nLeave available: {_number(bal['total_available'])} days (PL: {_number(bal['pl_available'])}, SL: {_number(bal['sl_available'])}, CL: {_number(bal['cl_available'])})."
    return _reply(text, ["Today's attendance", "Pending approvals"], ("Open employee", f"/employees/{person['id']}"))


def _answer_hr(storage: Storage, settings: Settings, user: SessionUser, tokens: set[str]) -> dict[str, Any]:
    suggestions = HR_SUGGESTIONS
    people = _find_people(storage, tokens)
    if len(people) == 1:
        return _hr_person(storage, settings, people[0])
    if people:
        return _reply(f"Which person do you mean? {_names(people)}.", suggestions,
                      *[(p["name"], f"/employees/{p['id']}") for p in people[:4]])
    if tokens & NOTICE:
        return _notices(storage, "hr", suggestions)
    if tokens & HOLIDAY or (tokens & LEAVE_WORDS and tokens & WHEN):
        return _holidays(storage, settings, suggestions)
    if tokens & PENDING or tokens & CORRECTION:
        return _hr_pending(storage)
    if tokens & PAY:
        return _hr_payroll(storage, settings)
    if tokens & POLICY:
        return _policy(storage, settings, None, suggestions)
    if tokens & PEOPLE:
        return _hr_headcount(storage, tokens)
    if tokens & (PUNCH | ATTENDANCE | LEAVE_WORDS):
        return _hr_today(storage, settings, tokens)
    if tokens & GREETING:
        name = (user.display_name or "there").split(" ")[0]
        return _reply(f"Hello {name}! Ask me about today's attendance, approvals, payroll, holidays, office timing, "
                      "or type an employee's name.", suggestions)
    return _reply("I don't know that one yet. I can answer about today's attendance, approvals, payroll, holidays, "
                  "office timing and individual employees.", suggestions)


def answer(storage: Storage, settings: Settings, user: SessionUser, query: str) -> dict[str, Any]:
    """The reply for one question: ``answer``, ``suggestions`` and link ``actions``."""
    tokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()[:500]))
    if user.is_employee and user.employee_id:
        return _answer_employee(storage, settings, user, tokens)
    if user.is_hr:
        return _answer_hr(storage, settings, user, tokens)
    return _reply("Ask HR is not available on this login.", [])
