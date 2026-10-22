"""Payroll: monthly runs, loss-of-pay maths, payslips and CSV export.

The model is deliberately the familiar Indian payroll shape:

    per_day      = monthly_gross / basis_days        (calendar days, or a fixed 26)
    earned_gross = monthly_gross - lop_days x per_day
    net_pay      = earned_gross + bonus - (pf + esi + professional tax + advance + other)

Attendance drives everything: any unmarked working day in the past counts as
absent (see ``attendance.effective_status``), which is why HR must close the
month's exceptions before finalising a run.

Late marks can optionally cost money: with ``late_marks_per_half_lop = 3``, every
third late mark adds half a day of loss of pay. Set it to 0 to switch the rule off.

Salary components come from ``salary_revisions`` — the row effective for the
period is used, so a mid-month raise is picked up the month it starts.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from . import attendance as attendance_service
from . import employees as employees_service
from . import orgtime

RUN_STATUSES = ("draft", "finalized", "paid")


# --------------------------------------------------------------------- runs
def list_runs(storage: Storage) -> list[dict[str, Any]]:
    runs = storage.query("SELECT * FROM payroll_runs ORDER BY period DESC")
    for run in runs:
        run["label"] = orgtime.month_label(run["period"])
        run["slip_count"] = int(
            storage.scalar("SELECT COUNT(*) AS n FROM payslips WHERE run_id = ?", (run["id"],))
        )
    return runs


def get_run(storage: Storage, period: str) -> dict[str, Any] | None:
    run = storage.query_one("SELECT * FROM payroll_runs WHERE period = ?", (period,))
    if run:
        run["label"] = orgtime.month_label(run["period"])
    return run


def basis_days_for(storage: Storage, period: str) -> int:
    basis = storage.get_setting("payroll_basis_days", "calendar")
    if basis.strip() == "26":
        return 26
    return orgtime.month_days(period)


def _require_draft(run: dict[str, Any]) -> None:
    if run["status"] != "draft":
        raise employees_service.ValidationError(
            f"The {orgtime.month_label(run['period'])} run is {run['status']} and can no longer be recalculated."
        )


def salary_for_period(storage: Storage, employee_id: int, period: str) -> dict[str, Any]:
    """The salary revision in force for the month, with a safe fallback."""
    _, month_end = orgtime.month_bounds(period)
    revision = storage.query_one(
        """
        SELECT * FROM salary_revisions
        WHERE employee_id = ? AND effective_from <= ?
        ORDER BY effective_from DESC, id DESC LIMIT 1
        """,
        (employee_id, month_end.isoformat()),
    )
    if revision:
        return revision
    employee = employees_service.require_employee(storage, employee_id)
    return {
        "monthly_gross": float(employee.get("monthly_gross") or 0),
        "basic": 0.0, "hra": 0.0, "other_allowances": 0.0,
        "pf": 0.0, "esi": 0.0, "professional_tax": 0.0,
        "effective_from": employee.get("joined_on"),
    }


def compute_slip(storage: Storage, settings: Settings, employee: dict[str, Any], period: str) -> dict[str, Any]:
    """Pure calculation for one employee-month (no writes)."""
    basis_days = basis_days_for(storage, period)
    summary = attendance_service.employee_month_summary(storage, settings, employee["id"], period)
    salary = salary_for_period(storage, employee["id"], period)
    monthly_gross = float(salary.get("monthly_gross") or 0)
    per_day = monthly_gross / basis_days if basis_days else 0.0

    present_days = float(summary["present"]) + 0.5 * float(summary["half_days"])
    paid_leave_days = float(summary["paid_leave"])
    lop_days = float(summary["absent"]) + float(summary["unpaid_leave"])

    marks_per_lop = storage.get_int("late_marks_per_half_lop", 0)
    late_lop_days = 0.0
    if marks_per_lop > 0 and summary["late_marks"] >= marks_per_lop:
        late_lop_days = (summary["late_marks"] // marks_per_lop) * 0.5
        lop_days += late_lop_days

    earned_gross = max(0.0, monthly_gross - lop_days * per_day)

    pf = float(salary.get("pf") or 0)
    esi = float(salary.get("esi") or 0)
    professional_tax = float(salary.get("professional_tax") or 0)
    total_deductions = pf + esi + professional_tax

    return {
        "period": period,
        "basis_days": basis_days,
        "present_days": round(present_days, 2),
        "half_days": round(float(summary["half_days"]), 2),
        "paid_leave_days": round(paid_leave_days, 2),
        "unpaid_leave_days": round(float(summary["unpaid_leave"]), 2),
        "absent_days": round(float(summary["absent"]), 2),
        "late_marks": int(summary["late_marks"]),
        "late_lop_days": round(late_lop_days, 2),
        "lop_days": round(lop_days, 2),
        "monthly_gross": round(monthly_gross, 2),
        "per_day_rate": round(per_day, 2),
        "earned_gross": round(earned_gross, 2),
        "pf": round(pf, 2),
        "esi": round(esi, 2),
        "professional_tax": round(professional_tax, 2),
        "total_deductions": round(total_deductions, 2),
        "net_pay": round(earned_gross - total_deductions, 2),
        "worked_minutes": int(summary["worked_minutes"]),
        "salary_effective_from": salary.get("effective_from"),
    }


def generate_run(
    storage: Storage,
    settings: Settings,
    period: str,
    *,
    actor: str | None = None,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Create or refresh the draft run for a month, recomputing every payslip."""
    try:
        orgtime.month_bounds(period)  # validates the period string
    except (ValueError, TypeError) as exc:
        raise employees_service.ValidationError("Period must look like 2026-10.") from exc

    run = get_run(storage, period)
    if run:
        _require_draft(run)
        storage.execute("DELETE FROM payslips WHERE run_id = ?", (run["id"],))
        storage.execute(
            "UPDATE payroll_runs SET basis_days = ?, generated_at = ?, note = NULL WHERE id = ?",
            (basis_days_for(storage, period), utcnow_iso(), run["id"]),
        )
    else:
        cursor = storage.execute(
            "INSERT INTO payroll_runs (period, status, basis_days, generated_at) VALUES (?, 'draft', ?, ?)",
            (period, basis_days_for(storage, period), utcnow_iso()),
        )
        run = {"id": int(cursor.lastrowid), "period": period, "status": "draft",
               "basis_days": basis_days_for(storage, period)}

    clause = "" if include_inactive else "WHERE status = 'active'"
    staff = storage.query(f"SELECT * FROM employees {clause} ORDER BY name COLLATE NOCASE")

    now = utcnow_iso()
    for employee in staff:
        calc = compute_slip(storage, settings, employee, period)
        storage.execute(
            """
            INSERT INTO payslips (run_id, employee_id, period, basis_days, present_days, paid_leave_days,
                                  lop_days, monthly_gross, per_day_rate, earned_gross, pf, esi,
                                  professional_tax, total_deductions, net_pay, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run["id"], employee["id"], period, calc["basis_days"], calc["present_days"],
                calc["paid_leave_days"], calc["lop_days"], calc["monthly_gross"], calc["per_day_rate"],
                calc["earned_gross"], calc["pf"], calc["esi"], calc["professional_tax"],
                calc["total_deductions"], calc["net_pay"], now, now,
            ),
        )

    _recompute_totals(storage, run["id"])
    storage.audit(actor=actor, action="generate_payroll", entity="payroll_run", entity_id=run["id"],
                  detail=f"{period}: {len(staff)} payslip(s)")
    refreshed = get_run(storage, period)
    assert refreshed is not None
    return refreshed


def _recompute_totals(storage: Storage, run_id: int) -> None:
    row = storage.query_one(
        """
        SELECT COALESCE(SUM(earned_gross),0) AS gross, COALESCE(SUM(total_deductions),0) AS deductions,
               COALESCE(SUM(net_pay),0) AS net, COUNT(*) AS n
        FROM payslips WHERE run_id = ?
        """,
        (run_id,),
    ) or {}
    storage.execute(
        "UPDATE payroll_runs SET total_gross = ?, total_deductions = ?, total_net = ? WHERE id = ?",
        (round(row.get("gross", 0), 2), round(row.get("deductions", 0), 2), round(row.get("net", 0), 2), run_id),
    )


def update_slip(
    storage: Storage,
    slip_id: int,
    *,
    bonus: float = 0.0,
    advance: float = 0.0,
    other_deduction: float = 0.0,
    note: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply HR adjustments (bonus, salary advance, other deduction) to a payslip."""
    slip = storage.query_one("SELECT * FROM payslips WHERE id = ?", (slip_id,))
    if slip is None:
        raise employees_service.ValidationError(f"Payslip #{slip_id} not found.")
    run = storage.query_one("SELECT * FROM payroll_runs WHERE id = ?", (slip["run_id"],))
    assert run is not None
    _require_draft(run)

    for label, value in (("bonus", bonus), ("advance", advance), ("other_deduction", other_deduction)):
        if value < 0:
            raise employees_service.ValidationError(f"{label.replace('_', ' ').title()} cannot be negative.")

    total_deductions = slip["pf"] + slip["esi"] + slip["professional_tax"] + advance + other_deduction
    net_pay = slip["earned_gross"] + bonus - total_deductions
    storage.execute(
        """
        UPDATE payslips SET bonus = ?, advance = ?, other_deduction = ?, total_deductions = ?,
                            net_pay = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (bonus, advance, other_deduction, round(total_deductions, 2), round(net_pay, 2), note,
         utcnow_iso(), slip_id),
    )
    _recompute_totals(storage, slip["run_id"])
    storage.audit(actor=actor, action="update_payslip", entity="payslip", entity_id=slip_id,
                  detail=f"bonus={bonus} advance={advance} deduction={other_deduction}")
    updated = storage.query_one("SELECT * FROM payslips WHERE id = ?", (slip_id,))
    assert updated is not None
    return updated


def set_run_status(storage: Storage, period: str, status: str, *, actor: str | None = None) -> dict[str, Any]:
    if status not in RUN_STATUSES:
        raise employees_service.ValidationError(f"Unknown payroll status '{status}'.")
    run = get_run(storage, period)
    if run is None:
        raise employees_service.ValidationError(f"No payroll run found for {period}.")
    if run["status"] == "paid":
        raise employees_service.ValidationError("A paid run cannot be changed.")

    column = "finalized_at" if status == "finalized" else "paid_at"
    storage.execute(
        f"UPDATE payroll_runs SET status = ?, {column} = ? WHERE id = ?", (status, utcnow_iso(), run["id"])
    )
    storage.audit(actor=actor, action=f"payroll_{status}", entity="payroll_run", entity_id=run["id"],
                  detail=period)
    refreshed = get_run(storage, period)
    assert refreshed is not None
    return refreshed


def reopen_run(storage: Storage, period: str, *, actor: str | None = None) -> dict[str, Any]:
    """Push a finalised run back to draft so a mistake can be corrected."""
    run = get_run(storage, period)
    if run is None:
        raise employees_service.ValidationError(f"No payroll run found for {period}.")
    if run["status"] == "paid":
        raise employees_service.ValidationError("A paid run cannot be reopened.")
    storage.execute(
        "UPDATE payroll_runs SET status = 'draft', finalized_at = NULL WHERE id = ?", (run["id"],)
    )
    storage.audit(actor=actor, action="payroll_reopen", entity="payroll_run", entity_id=run["id"], detail=period)
    refreshed = get_run(storage, period)
    assert refreshed is not None
    return refreshed


def delete_run(storage: Storage, period: str, *, actor: str | None = None) -> None:
    run = get_run(storage, period)
    if run is None:
        return
    storage.execute("DELETE FROM payslips WHERE run_id = ?", (run["id"],))
    storage.execute("DELETE FROM payroll_runs WHERE id = ?", (run["id"],))
    storage.audit(actor=actor, action="delete_payroll", entity="payroll_run", entity_id=run["id"], detail=period)


# ------------------------------------------------------------------- read
def slips_for_run(storage: Storage, run_id: int) -> list[dict[str, Any]]:
    rows = storage.query(
        """
        SELECT p.*, e.code, e.name, e.department, e.designation, e.joined_on
        FROM payslips p JOIN employees e ON e.id = p.employee_id
        WHERE p.run_id = ? ORDER BY e.name COLLATE NOCASE
        """,
        (run_id,),
    )
    return rows


def list_slips(storage: Storage, period: str) -> list[dict[str, Any]]:
    run = get_run(storage, period)
    return slips_for_run(storage, run["id"]) if run else []


def get_slip(storage: Storage, slip_id: int) -> dict[str, Any] | None:
    return storage.query_one(
        """
        SELECT p.*, e.code, e.name, e.department, e.designation, e.joined_on, e.email, e.phone, e.id AS employee_id
        FROM payslips p JOIN employees e ON e.id = p.employee_id WHERE p.id = ?
        """,
        (slip_id,),
    )


def period_summary(storage: Storage, period: str) -> dict[str, Any]:
    run = get_run(storage, period)
    if not run:
        return {"run": None, "slips": [], "totals": {}, "attendance": {}}
    slips = slips_for_run(storage, run["id"])
    totals = {
        "employees": len(slips),
        "gross": round(sum(s["earned_gross"] for s in slips), 2),
        "bonus": round(sum(s["bonus"] for s in slips), 2),
        "deductions": round(sum(s["total_deductions"] for s in slips), 2),
        "net": round(sum(s["net_pay"] for s in slips), 2),
        "lop_days": round(sum(s["lop_days"] for s in slips), 2),
        "present_days": round(sum(s["present_days"] for s in slips), 2),
        "paid_leave_days": round(sum(s["paid_leave_days"] for s in slips), 2),
    }
    return {
        "run": run,
        "slips": slips,
        "totals": totals,
        "attendance": attendance_service.monthly_register(
            storage, storage.settings, period
        ),
    }


def payroll_csv(storage: Storage, period: str) -> str:
    """Bank-transfer style CSV: one row per employee, ready for the accountant."""
    slips = list_slips(storage, period)
    header = (
        "Employee Code,Employee Name,Department,Designation,Monthly Gross,Basis Days,Present Days,"
        "Paid Leave Days,LOP Days,Earned Gross,Bonus,Advance,Other Deduction,PF,ESI,Professional Tax,"
        "Total Deductions,Net Pay,Note"
    )
    lines = [header]
    for slip in slips:
        fields = [
            slip["code"], slip["name"], slip.get("department") or "", slip.get("designation") or "",
            f"{slip['monthly_gross']:.2f}", slip["basis_days"], f"{slip['present_days']:g}",
            f"{slip['paid_leave_days']:g}", f"{slip['lop_days']:g}", f"{slip['earned_gross']:.2f}",
            f"{slip['bonus']:.2f}", f"{slip['advance']:.2f}", f"{slip['other_deduction']:.2f}",
            f"{slip['pf']:.2f}", f"{slip['esi']:.2f}", f"{slip['professional_tax']:.2f}",
            f"{slip['total_deductions']:.2f}", f"{slip['net_pay']:.2f}", (slip.get("note") or "").replace(",", ";"),
        ]
        lines.append(",".join(_csv_safe(value) for value in fields))
    return "\n".join(lines) + "\n"


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text
