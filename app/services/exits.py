"""Employee exit: resignation → notice → clearance → full & final (F&F) settlement.

    employee resigns (ESS) or HR records an exit        status = requested / approved
    HR approves, fixes the last working day (LWD)        status = approved  (+ clearance checklist)
    HR ticks the checklist, reviews the F&F figure       (computed live, never stored until settled)
    HR settles                                           status = settled   (employee → inactive)

The F&F statement is the familiar Indian shape:

    earnings   = salary for the LWD month (if payroll has not paid it)
               + leave encashment (paid-leave balance x basic / 30)
               + gratuity (15/26 x basic x years, from 4 years 240 days of service)
               + approved-but-unpaid expense claims + HR's extra earnings
    deductions = PF / ESI / PT on that salary
               + notice shortfall (unserved notice days x gross / 30, unless waived)
               + cost of company assets not returned + HR's extra deductions

"basic" falls back to gross when a salary structure has no basic split. The
statement is frozen into ``fnf_snapshot`` at settlement, so later salary or
attendance edits cannot change a settled figure.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..config import Settings
from .. import notify
from ..db import Storage, utcnow_iso
from . import attendance as attendance_service
from . import employees as employees_service
from . import leave as leave_service
from . import orgtime, payroll

ValidationError = employees_service.ValidationError

EXIT_TYPES = {
    "resignation": "Resignation",
    "termination": "Termination",
    "retirement": "Retirement",
    "absconding": "Absconding",
    "other": "Other",
}
OPEN_STATUSES = ("requested", "approved")
DEFAULT_CHECKLIST = (
    "Company assets returned",
    "ID / access card returned",
    "Email and system access revoked",
    "Knowledge transfer completed",
    "Exit interview done",
    "No-dues from accounts",
)
GRATUITY_CAP = 2_000_000.0
#: Payment of Gratuity Act: 5 years, read by courts as 4 years and 240 days.
GRATUITY_MIN_DAYS = 4 * 365 + 240


def _date(value: str | None, label: str) -> date:
    try:
        return orgtime.parse_date(value or "")
    except ValueError as exc:
        raise ValidationError(f"Enter a valid {label}.") from exc


def notice_period(storage: Storage) -> int:
    return max(0, storage.get_int("notice_period_days", 30))


# ------------------------------------------------------------------ reading
def get(storage: Storage, exit_id: int) -> dict[str, Any]:
    row = storage.query_one(
        """
        SELECT x.*, e.name, e.code, e.department, e.designation, e.joined_on, e.status AS employee_status
        FROM employee_exits x JOIN employees e ON e.id = x.employee_id WHERE x.id = ?
        """,
        (exit_id,),
    )
    if row is None:
        raise ValidationError("Exit record not found.")
    row["type_label"] = EXIT_TYPES.get(row["exit_type"], row["exit_type"])
    return row


def list_exits(storage: Storage, *, status: str | None = None) -> list[dict[str, Any]]:
    where, params = "", []
    if status == "open":
        where = "WHERE x.status IN ('requested','approved')"
    elif status and status != "all":
        where, params = "WHERE x.status = ?", [status]
    rows = storage.query(
        f"""
        SELECT x.*, e.name, e.code, e.department FROM employee_exits x
        JOIN employees e ON e.id = x.employee_id {where}
        ORDER BY CASE x.status WHEN 'requested' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                 x.last_working_day DESC, x.id DESC
        """,
        params,
    )
    for row in rows:
        row["type_label"] = EXIT_TYPES.get(row["exit_type"], row["exit_type"])
    return rows


def open_exit_for(storage: Storage, employee_id: int) -> dict[str, Any] | None:
    row = storage.query_one(
        "SELECT id FROM employee_exits WHERE employee_id = ? AND status IN ('requested','approved') "
        "ORDER BY id DESC LIMIT 1",
        (employee_id,),
    )
    return get(storage, row["id"]) if row else None


def latest_for(storage: Storage, employee_id: int) -> dict[str, Any] | None:
    row = storage.query_one(
        "SELECT id FROM employee_exits WHERE employee_id = ? ORDER BY id DESC LIMIT 1", (employee_id,)
    )
    return get(storage, row["id"]) if row else None


def checklist(storage: Storage, exit_id: int) -> list[dict[str, Any]]:
    return storage.query("SELECT * FROM exit_checklist WHERE exit_id = ? ORDER BY id", (exit_id,))


def counts(storage: Storage) -> dict[str, int]:
    rows = storage.query("SELECT status, COUNT(*) AS n FROM employee_exits GROUP BY status")
    found = {row["status"]: int(row["n"]) for row in rows}
    return {key: found.get(key, 0) for key in ("requested", "approved", "settled", "withdrawn", "rejected")}


# ------------------------------------------------------------------ writing
def _insert(storage: Storage, *, employee_id: int, exit_type: str, initiated_by: str, reason: str | None,
            resignation_date: date, notice_days: int, last_working_day: date, status: str,
            actor: str | None) -> int:
    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO employee_exits (employee_id, exit_type, initiated_by, reason, resignation_date, notice_days,
                                    last_working_day, status, created_by, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, exit_type, initiated_by, reason, resignation_date.isoformat(), notice_days,
         last_working_day.isoformat(), status, actor, now, now),
    )
    return int(cursor.lastrowid)


def resign(storage: Storage, settings: Settings, *, employee_id: int, reason: str,
           proposed_last_day: str | None = None, actor: str | None = None) -> dict[str, Any]:
    """Employee-initiated resignation from the portal."""
    employees_service.require_employee(storage, employee_id)
    if open_exit_for(storage, employee_id):
        raise ValidationError("You already have a resignation in progress.")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("Please give a short reason for leaving.")
    today = orgtime.today(settings)
    notice = notice_period(storage)
    lwd = _date(proposed_last_day, "last working day") if proposed_last_day else today + timedelta(days=notice)
    if lwd < today:
        raise ValidationError("The last working day cannot be in the past.")
    exit_id = _insert(storage, employee_id=employee_id, exit_type="resignation", initiated_by="employee",
                      reason=reason[:1000], resignation_date=today, notice_days=notice,
                      last_working_day=lwd, status="requested", actor=actor)
    storage.audit(actor=actor, action="resignation_submitted", entity="employee", entity_id=employee_id,
                  detail=f"LWD {lwd.isoformat()}")
    employee = employees_service.get_employee(storage, employee_id) or {}
    notify.notify_hr(
        storage, settings, "hr_alert", category="resignation",
        subject=f"Resignation: {employee.get('name')}",
        message=f"{employee.get('name')} ({employee.get('code')}) has resigned. Proposed last working day: "
                f"{orgtime.human_date(lwd)}. Reason: {reason}",
        link=f"/exits/{exit_id}",
    )
    return get(storage, exit_id)


def record_exit(storage: Storage, settings: Settings, *, employee_id: int, exit_type: str,
                resignation_date: str, last_working_day: str, notice_days: int | None = None,
                notice_waived: bool = False, reason: str | None = None, actor: str | None = None) -> dict[str, Any]:
    """HR-initiated exit (termination, retirement, or a resignation received on paper)."""
    employees_service.require_employee(storage, employee_id)
    if exit_type not in EXIT_TYPES:
        raise ValidationError("Choose the type of exit.")
    if open_exit_for(storage, employee_id):
        raise ValidationError("This employee already has an exit in progress.")
    start = _date(resignation_date, "notice date")
    lwd = _date(last_working_day, "last working day")
    if lwd < start:
        raise ValidationError("The last working day cannot be before the notice date.")
    notice = notice_period(storage) if notice_days is None else max(0, int(notice_days))
    exit_id = _insert(storage, employee_id=employee_id, exit_type=exit_type, initiated_by="hr",
                      reason=(reason or "").strip() or None, resignation_date=start, notice_days=notice,
                      last_working_day=lwd, status="requested", actor=actor)
    approve(storage, exit_id, last_working_day=lwd.isoformat(), notice_days=notice,
            notice_waived=notice_waived, actor=actor)
    return get(storage, exit_id)


def approve(storage: Storage, exit_id: int, *, last_working_day: str, notice_days: int,
            notice_waived: bool = False, actor: str | None = None) -> dict[str, Any]:
    row = get(storage, exit_id)
    if row["status"] not in ("requested", "approved"):
        raise ValidationError(f"This exit is already {row['status']}.")
    lwd = _date(last_working_day, "last working day")
    if lwd < orgtime.parse_date(row["resignation_date"]):
        raise ValidationError("The last working day cannot be before the resignation date.")
    storage.execute(
        """
        UPDATE employee_exits SET status = 'approved', last_working_day = ?, notice_days = ?, notice_waived = ?,
                                  decided_by = COALESCE(decided_by, ?), decided_at = COALESCE(decided_at, ?),
                                  updated_at = ?
        WHERE id = ?
        """,
        (lwd.isoformat(), max(0, int(notice_days)), 1 if notice_waived else 0, actor, utcnow_iso(),
         utcnow_iso(), exit_id),
    )
    if not checklist(storage, exit_id):
        storage.executemany(
            "INSERT INTO exit_checklist (exit_id, item) VALUES (?, ?)",
            [(exit_id, item) for item in DEFAULT_CHECKLIST],
        )
    storage.audit(actor=actor, action="exit_approved", entity="employee", entity_id=row["employee_id"],
                  detail=f"LWD {lwd.isoformat()}")
    return get(storage, exit_id)


def reject(storage: Storage, exit_id: int, *, actor: str | None = None) -> None:
    row = get(storage, exit_id)
    if row["status"] != "requested":
        raise ValidationError("Only a pending resignation can be rejected.")
    storage.execute(
        "UPDATE employee_exits SET status = 'rejected', decided_by = ?, decided_at = ?, updated_at = ? WHERE id = ?",
        (actor, utcnow_iso(), utcnow_iso(), exit_id),
    )
    storage.audit(actor=actor, action="exit_rejected", entity="employee", entity_id=row["employee_id"])


def withdraw(storage: Storage, exit_id: int, *, actor: str | None = None,
             employee_id: int | None = None) -> None:
    row = get(storage, exit_id)
    if employee_id is not None and row["employee_id"] != employee_id:
        raise ValidationError("Exit record not found.")
    if row["status"] not in OPEN_STATUSES:
        raise ValidationError(f"This exit is already {row['status']}.")
    if employee_id is not None and row["status"] != "requested":
        raise ValidationError("HR has already accepted your resignation — please speak to HR to withdraw it.")
    storage.execute("UPDATE employee_exits SET status = 'withdrawn', updated_at = ? WHERE id = ?",
                    (utcnow_iso(), exit_id))
    storage.audit(actor=actor, action="exit_withdrawn", entity="employee", entity_id=row["employee_id"])


def toggle_item(storage: Storage, item_id: int, *, done: bool, actor: str | None = None) -> int:
    item = storage.query_one("SELECT * FROM exit_checklist WHERE id = ?", (item_id,))
    if item is None:
        raise ValidationError("Checklist item not found.")
    storage.execute(
        "UPDATE exit_checklist SET done = ?, done_by = ?, done_at = ? WHERE id = ?",
        (1 if done else 0, actor if done else None, utcnow_iso() if done else None, item_id),
    )
    return int(item["exit_id"])


def update_settlement(storage: Storage, exit_id: int, *, extra_earnings: float, extra_deductions: float,
                      recover_assets: bool, note: str | None, actor: str | None = None) -> dict[str, Any]:
    row = get(storage, exit_id)
    if row["status"] != "approved":
        raise ValidationError("Adjust the settlement after the exit is approved and before it is settled.")
    if extra_earnings < 0 or extra_deductions < 0:
        raise ValidationError("Adjustments cannot be negative.")
    storage.execute(
        "UPDATE employee_exits SET extra_earnings = ?, extra_deductions = ?, recover_assets = ?, fnf_note = ?, "
        "updated_at = ? WHERE id = ?",
        (round(extra_earnings, 2), round(extra_deductions, 2), 1 if recover_assets else 0,
         (note or "").strip() or None, utcnow_iso(), exit_id),
    )
    storage.audit(actor=actor, action="fnf_adjusted", entity="employee", entity_id=row["employee_id"],
                  detail=f"+{extra_earnings} -{extra_deductions}")
    return get(storage, exit_id)


# ------------------------------------------------------------- settlement
def compute_fnf(storage: Storage, settings: Settings, exit_row: dict[str, Any]) -> dict[str, Any]:
    """The full & final statement for an exit. Pure calculation — no writes."""
    if exit_row["status"] == "settled" and exit_row.get("fnf_snapshot"):
        return json.loads(exit_row["fnf_snapshot"])

    employee = employees_service.require_employee(storage, exit_row["employee_id"])
    lwd = orgtime.parse_date(exit_row["last_working_day"])
    period = orgtime.period_of(lwd)
    salary = payroll.salary_for_period(storage, employee["id"], period)
    gross = float(salary.get("monthly_gross") or 0)
    basic = float(salary.get("basic") or 0) or gross
    basis_days = payroll.basis_days_for(storage, period)
    per_day = gross / basis_days if basis_days else 0.0
    notes: list[str] = []
    if not float(salary.get("basic") or 0):
        notes.append("No basic split in the salary structure — gross is used for encashment and gratuity.")

    earnings: list[dict[str, Any]] = []
    deductions: list[dict[str, Any]] = []

    def earn(label: str, amount: float, note: str = "") -> None:
        earnings.append({"label": label, "amount": round(amount, 2), "note": note})

    def deduct(label: str, amount: float, note: str = "") -> None:
        deductions.append({"label": label, "amount": round(amount, 2), "note": note})

    # 1. salary for every month payroll has not covered yet, up to the LWD (EXIT-01)
    last_paid = storage.scalar(
        "SELECT MAX(period) AS p FROM payslips WHERE employee_id = ?", (employee["id"],), None
    )
    months: list[str] = []
    if last_paid and last_paid < period:
        year, month = (int(x) for x in last_paid.split("-"))
        while True:
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            months.append(f"{year}-{month:02d}")
            if months[-1] >= period:
                break
    elif not last_paid:
        months = [period]  # no payroll history in HRMS: only the leaving month is ours to settle
    else:
        notes.append(f"{orgtime.month_label(period)} salary is in the payroll run, so it is not repeated here.")

    payable_days = 0.0
    totals = {"pf": 0.0, "esi": 0.0, "professional_tax": 0.0}
    for month in months:
        slip = payroll.compute_slip(storage, settings, employee, month)
        if slip["earned_gross"] <= 0 and slip["employed_days"] <= 0:
            continue
        days = slip["present_days"] + slip["paid_leave_days"]
        payable_days += days
        earn(f"Salary {orgtime.month_label(month)}", slip["earned_gross"],
             f"{slip['employed_days']} day(s) employed · {slip['lop_days']:g} LOP")
        for key in totals:
            totals[key] += float(slip[key])
    if totals["pf"]:
        deduct("Provident fund (PF)", totals["pf"], "on earned wages")
    if totals["esi"]:
        deduct("ESI", totals["esi"], "on earned wages")
    if totals["professional_tax"]:
        deduct("Professional tax", totals["professional_tax"])
    if len(months) > 1:
        notes.append(f"Includes {len(months)} months not yet paid through payroll.")

    # 2. leave encashment
    leave_balance = max(0.0, float(leave_service.balance(storage, settings, employee["id"], as_of=lwd)["balance"]))
    if leave_balance:
        earn("Leave encashment", leave_balance * basic / 30, f"{leave_balance:g} day(s) × basic ÷ 30")

    # 3. gratuity
    service_days = 0
    years = 0
    if employee.get("joined_on"):
        service_days = max(0, (lwd - orgtime.parse_date(employee["joined_on"])).days + 1)
        whole, remainder = divmod(service_days, 365)
        years = whole + (1 if remainder > 182 else 0)
    if service_days >= GRATUITY_MIN_DAYS and years:
        gratuity = min(GRATUITY_CAP, 15 / 26 * basic * years)
        earn("Gratuity", gratuity, f"15/26 × basic × {years} year(s)")

    # 4. approved expense claims not paid yet
    claims = storage.query(
        "SELECT id, amount FROM expense_claims WHERE employee_id = ? AND status = 'approved'", (employee["id"],)
    )
    if claims:
        earn("Approved expense claims", sum(float(c["amount"]) for c in claims), f"{len(claims)} claim(s)")

    if float(exit_row.get("extra_earnings") or 0):
        earn("Other earnings (HR)", float(exit_row["extra_earnings"]), exit_row.get("fnf_note") or "")

    # 5. notice shortfall
    served = (lwd - orgtime.parse_date(exit_row["resignation_date"])).days
    shortfall = max(0, int(exit_row["notice_days"]) - served)
    if shortfall and not exit_row.get("notice_waived") and exit_row["exit_type"] != "termination":
        deduct("Notice period shortfall", shortfall * gross / 30, f"{shortfall} day(s) × gross ÷ 30")
    elif shortfall:
        notes.append(f"Notice shortfall of {shortfall} day(s) not recovered "
                     f"({'waived' if exit_row.get('notice_waived') else 'employer-initiated exit'}).")

    # 6. assets still with the employee
    open_assets = storage.query(
        "SELECT a.tag, a.name, a.cost FROM asset_assignments x JOIN assets a ON a.id = x.asset_id "
        "WHERE x.employee_id = ? AND x.returned_on IS NULL",
        (employee["id"],),
    )
    if open_assets and exit_row.get("recover_assets", 1):
        deduct("Unreturned assets", sum(float(a["cost"] or 0) for a in open_assets),
               ", ".join(a["tag"] for a in open_assets))

    if float(exit_row.get("extra_deductions") or 0):
        deduct("Other deductions (HR)", float(exit_row["extra_deductions"]), exit_row.get("fnf_note") or "")

    total_earnings = round(sum(line["amount"] for line in earnings), 2)
    total_deductions = round(sum(line["amount"] for line in deductions), 2)
    return {
        "employee": {k: employee.get(k) for k in ("id", "code", "name", "department", "designation",
                                                  "joined_on", "pan", "bank_name", "account_number", "ifsc")},
        "last_working_day": lwd.isoformat(),
        "resignation_date": exit_row["resignation_date"],
        "monthly_gross": round(gross, 2),
        "basic": round(basic, 2),
        "per_day": round(per_day, 2),
        "payable_days": payable_days,
        "leave_balance": leave_balance,
        "service_days": service_days,
        "service_years": round(service_days / 365.25, 2),
        "notice_days": int(exit_row["notice_days"]),
        "notice_served": max(0, served),
        "notice_shortfall": shortfall,
        "open_assets": open_assets,
        "claim_ids": [c["id"] for c in claims],
        "earnings": earnings,
        "deductions": deductions,
        "total_earnings": total_earnings,
        "total_deductions": total_deductions,
        "net": round(total_earnings - total_deductions, 2),
        "notes": notes,
        "computed_at": utcnow_iso(),
    }


def settle(storage: Storage, settings: Settings, exit_id: int, *, actor: str | None = None) -> dict[str, Any]:
    row = get(storage, exit_id)
    if row["status"] != "approved":
        raise ValidationError("Approve the exit before settling it.")
    pending = [item["item"] for item in checklist(storage, exit_id) if not item["done"]]
    if pending:
        raise ValidationError("Finish the clearance checklist first: " + ", ".join(pending) + ".")
    statement = compute_fnf(storage, settings, row)
    with storage.transaction():
        storage.execute_tx(
            "UPDATE employee_exits SET status = 'settled', fnf_snapshot = ?, fnf_net = ?, settled_by = ?, "
            "settled_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(statement), statement["net"], actor, utcnow_iso(), utcnow_iso(), exit_id),
        )
        if statement["claim_ids"]:
            marks = ",".join("?" for _ in statement["claim_ids"])
            storage.execute_tx(
                f"UPDATE expense_claims SET status = 'paid', note = COALESCE(note, 'paid in F&F') WHERE id IN ({marks})",
                statement["claim_ids"],
            )
        storage.execute_tx("UPDATE employees SET status = 'inactive', updated_at = ? WHERE id = ?",
                           (utcnow_iso(), row["employee_id"]))
        storage.execute_tx("UPDATE users SET active = 0 WHERE employee_id = ?", (row["employee_id"],))
    storage.audit(actor=actor, action="fnf_settled", entity="employee", entity_id=row["employee_id"],
                  detail=f"net {statement['net']:.2f}")
    return get(storage, exit_id)
