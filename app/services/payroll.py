"""Payroll: monthly runs, loss-of-pay maths, payslips and CSV export.

The model is deliberately the familiar Indian payroll shape:

    per_day      = monthly_gross / basis_days        (calendar days, or a fixed 26)
    base_gross   = monthly_gross x employed_days / calendar_days   (joined / left mid-month)
    lop_days     = absent + unpaid leave + 0.5 x half days without paid leave + late penalty
    earned_gross = base_gross - lop_days x per_day
    net_pay      = earned_gross + bonus - (pf + esi + professional tax + tds + advance + other)

Attendance drives everything: any unmarked working day in the past counts as
absent (see ``attendance.effective_status``), which is why HR must close the
month's exceptions before finalising a run.

Lateness follows ``late_policy_mode``: tiered fines per late day (a payslip
deduction line), an automatic half day for very late arrivals, and/or the older
"every N late marks = half a day of LOP" rule (``lop_count_only`` / ``hybrid_both``).

Salary components come from ``salary_revisions`` — the row effective for the
period is used, so a mid-month raise is picked up the month it starts.

Income tax (TDS) comes from the employee's ``tax_profiles`` row for the financial
year (see ``services/tax.py``). Once HR/the CA confirms the annual figure, each
month deducts what is still outstanding divided by the months left, so a figure
confirmed in October is still fully recovered by March.
"""

from __future__ import annotations

from datetime import timedelta
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


#: Adjustments HR types onto a draft payslip; they survive a recalculation.
ADJUSTMENT_FIELDS = ("bonus", "advance", "other_deduction", "tds_override", "note")


def _require_started(settings: Settings, period: str) -> None:
    """A month that has not begun has no attendance to pay for (PAY-04)."""
    if period > orgtime.period_of(orgtime.today(settings)):
        raise employees_service.ValidationError(
            f"Cannot generate payroll for {orgtime.month_label(period)} — that month has not started. "
            "Payroll can only be run for the current or a past month."
        )


def _require_closable(storage: Storage, settings: Settings, period: str) -> None:
    """Finalising/paying the current month waits for its last day (configurable)."""
    today = orgtime.today(settings)
    if period > orgtime.period_of(today):
        _require_started(settings, period)
    if period < orgtime.period_of(today):
        return
    last_day = orgtime.month_bounds(period)[1].day
    from_day = storage.get_int("payroll_finalise_from_day", 0) or last_day
    if today.day < min(from_day, last_day):
        raise employees_service.ValidationError(
            f"{orgtime.month_label(period)} is still running — it can be finalised from day "
            f"{min(from_day, last_day)} of the month. Keep it as a draft until then."
        )


def period_state(storage: Storage, settings: Settings, period: str) -> dict[str, Any]:
    """What the payroll screen may offer for a month: future, still running, or closable."""
    today = orgtime.today(settings)
    current = orgtime.period_of(today)
    last_day = orgtime.month_bounds(period)[1]
    from_day = min(storage.get_int("payroll_finalise_from_day", 0) or last_day.day, last_day.day)
    running = period == current
    return {
        "future": period > current,
        "running": running,
        "days_left": (last_day - today).days if running else 0,
        "closable": period < current or (running and today.day >= from_day),
        "closable_from": last_day.replace(day=from_day) if running else None,
    }


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
        month_start, _ = orgtime.month_bounds(period)
        if revision["effective_from"] > month_start.isoformat():
            # A raise that starts mid-month: weight each structure by its own days (PAY-12).
            return _blend_revisions(storage, employee_id, revision, period)
        return revision
    employee = employees_service.require_employee(storage, employee_id)
    return {
        "monthly_gross": float(employee.get("monthly_gross") or 0),
        "basic": 0.0, "hra": 0.0, "other_allowances": 0.0,
        "pf": 0.0, "esi": 0.0, "professional_tax": 0.0,
        "effective_from": employee.get("joined_on"),
    }


def tds_for_period(storage: Storage, employee_id: int, period: str) -> float:
    """Income tax to deduct this month, from the FY tax profile (0 without one)."""
    fy = orgtime.fiscal_year(period)
    profile = storage.query_one(
        "SELECT confirmed_tds, monthly_tds FROM tax_profiles WHERE employee_id = ? AND fiscal_year = ?",
        (employee_id, fy),
    )
    if not profile:
        return 0.0
    if profile["confirmed_tds"] is None:
        return max(0.0, float(profile["monthly_tds"] or 0))

    months = orgtime.fiscal_year_months(fy)
    earlier = months[: months.index(period)]
    already = 0.0
    if earlier:
        marks = ",".join("?" for _ in earlier)
        already = float(storage.scalar(
            f"SELECT COALESCE(SUM(tds), 0) AS t FROM payslips WHERE employee_id = ? AND period IN ({marks})",
            (employee_id, *earlier),
        ))
    remaining_months = len(months) - len(earlier)
    return max(0.0, (float(profile["confirmed_tds"]) - already) / remaining_months)


#: Statutory limits used in "auto" mode (PAY-08).
PF_RATE, PF_WAGE_CEILING = 0.12, 15_000.0
ESI_RATE, ESI_GROSS_LIMIT = 0.0075, 21_000.0


def statutory_deductions(salary: dict[str, Any], monthly_gross: float, earned_gross: float) -> tuple[float, float, float]:
    """PF, ESI and professional tax for the month, following the *earned* wages.

    ``fixed`` mode pro-rates the amounts on the salary structure by the share of
    the month actually earned; ``auto`` mode computes 12% PF on earned basic (wage
    ceiling ₹15,000) and 0.75% ESI on earned gross while gross is within ₹21,000.
    Professional tax is a flat slab and is only charged when something was earned.
    """
    share = (earned_gross / monthly_gross) if monthly_gross else 0.0
    professional_tax = float(salary.get("professional_tax") or 0) if earned_gross > 0 else 0.0
    if salary.get("statutory_mode") == "auto":
        basic = float(salary.get("basic") or 0) or monthly_gross * 0.5
        pf = PF_RATE * min(basic * share, PF_WAGE_CEILING)
        esi = ESI_RATE * earned_gross if monthly_gross <= ESI_GROSS_LIMIT else 0.0
    else:
        pf = float(salary.get("pf") or 0) * share
        esi = float(salary.get("esi") or 0) * share
    # never deduct more than was earned
    pf = min(pf, earned_gross)
    esi = min(esi, max(0.0, earned_gross - pf))
    professional_tax = min(professional_tax, max(0.0, earned_gross - pf - esi))
    return round(pf, 2), round(esi, 2), round(professional_tax, 2)


def approved_claims(storage: Storage, employee_id: int, period: str) -> list[dict[str, Any]]:
    """Approved, not-yet-paid expense claims dated up to the end of the period (PAY-14)."""
    _, month_end = orgtime.month_bounds(period)
    run = get_run(storage, period)
    return storage.query(
        "SELECT id, amount FROM expense_claims WHERE employee_id = ? AND status = 'approved' "
        "AND expense_date <= ? AND (payroll_run_id IS NULL OR payroll_run_id = ?) ORDER BY id",
        (employee_id, month_end.isoformat(), run["id"] if run else -1),
    )


def _settle(earned: float, bonus: float, reimbursement: float, deductions: float) -> dict[str, float]:
    """Net pay rounded to the rupee, keeping the rounding so totals reconcile (PAY-09)."""
    raw = earned + bonus + reimbursement - deductions
    net = float(round(raw))
    return {"net_pay": net, "rounding": round(net - raw, 2)}


def _blend_revisions(storage: Storage, employee_id: int, latest: dict[str, Any], period: str) -> dict[str, Any]:
    month_start, month_end = orgtime.month_bounds(period)
    month_days = (month_end - month_start).days + 1
    revisions = storage.query(
        "SELECT * FROM salary_revisions WHERE employee_id = ? AND effective_from <= ? "
        "ORDER BY effective_from, id",
        (employee_id, month_end.isoformat()),
    )
    # one structure per day: the latest revision effective on or before it
    weights: dict[int, int] = {}
    by_id = {rev["id"]: rev for rev in revisions}
    for offset in range(month_days):
        day = (month_start + timedelta(days=offset)).isoformat()
        active = [rev for rev in revisions if rev["effective_from"] <= day]
        if active:
            weights[active[-1]["id"]] = weights.get(active[-1]["id"], 0) + 1
    covered = sum(weights.values())
    if covered == 0 or len(weights) == 1:
        return latest
    blended = dict(latest)
    for field in ("monthly_gross", "basic", "hra", "other_allowances", "pf", "esi"):
        blended[field] = sum(float(by_id[rid][field] or 0) * days for rid, days in weights.items()) / covered
    blended["split_note"] = ", ".join(
        f"{orgtime.human_date(by_id[rid]['effective_from'])}: {days} day(s) at {float(by_id[rid]['monthly_gross']):,.0f}"
        for rid, days in weights.items()
    )
    return blended


def compute_slip(storage: Storage, settings: Settings, employee: dict[str, Any], period: str) -> dict[str, Any]:
    """Pure calculation for one employee-month (no writes)."""
    basis_days = basis_days_for(storage, period)
    summary = attendance_service.employee_month_summary(storage, settings, employee["id"], period)
    salary = salary_for_period(storage, employee["id"], period)
    monthly_gross = float(salary.get("monthly_gross") or 0)
    per_day = monthly_gross / basis_days if basis_days else 0.0

    figures = attendance_service.pay_figures(
        summary, int(summary["late_marks"]), orgtime.late_marks_rule(storage)
    )
    present_days = figures["present_days"]
    paid_leave_days = figures["paid_leave_days"]
    late_lop_days = figures["late_lop_days"]
    lop_days = figures["lop_days"]
    # Joined or left mid-month: pay only the employed share of the month (PAY-03).
    month_days = orgtime.month_days(period)
    employed_share = min(1.0, summary["employed_days"] / month_days) if month_days else 0.0
    base_gross = monthly_gross * employed_share

    earned_gross = max(0.0, base_gross - lop_days * per_day)

    pf, esi, professional_tax = statutory_deductions(salary, monthly_gross, earned_gross)
    # Tax can only come out of pay that was actually earned this month.
    tds = min(tds_for_period(storage, employee["id"], period),
              max(0.0, earned_gross - pf - esi - professional_tax))
    # late-arrival fines (Feature 4), never more than the month can carry
    late_fine = min(float(summary.get("late_fines") or 0), max(0.0, earned_gross - pf - esi - professional_tax - tds))
    total_deductions = pf + esi + professional_tax + tds + late_fine
    claims = approved_claims(storage, employee["id"], period)

    return {
        "period": period,
        "basis_days": basis_days,
        "present_days": round(present_days, 2),
        "half_days": round(float(summary["half_days"]), 2),
        "paid_leave_days": round(paid_leave_days, 2),
        "unpaid_leave_days": round(float(summary["unpaid_leave"]), 2),
        "absent_days": round(float(summary["absent"]), 2),
        "late_marks": int(summary["late_marks"]),
        "employed_days": int(summary["employed_days"]),
        "not_employed_days": month_days - int(summary["employed_days"]),
        "late_lop_days": round(late_lop_days, 2),
        "lop_days": round(lop_days, 2),
        "monthly_gross": round(monthly_gross, 2),
        "per_day_rate": round(per_day, 2),
        "earned_gross": round(earned_gross, 2),
        "pf": round(pf, 2),
        "esi": round(esi, 2),
        "professional_tax": round(professional_tax, 2),
        "tds": round(tds, 2),
        "late_fine": round(late_fine, 2),
        "total_deductions": round(total_deductions, 2),
        "reimbursement": round(sum(float(c["amount"]) for c in claims), 2),
        "claim_ids": [c["id"] for c in claims],
        **_settle(earned_gross, 0.0, sum(float(c["amount"]) for c in claims), total_deductions),
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
    reset_adjustments: bool = False,
) -> dict[str, Any]:
    """Create or refresh the draft run for a month, recomputing every payslip.

    Recalculating keeps HR's adjustments (bonus, advance, other deduction, TDS
    override, note) unless ``reset_adjustments`` is set.
    """
    try:
        orgtime.month_bounds(period)  # validates the period string
    except (ValueError, TypeError) as exc:
        raise employees_service.ValidationError("Period must look like 2026-10.") from exc
    _require_started(settings, period)

    clause = "" if include_inactive else "WHERE status = 'active'"
    staff = storage.query(f"SELECT * FROM employees {clause} ORDER BY name COLLATE NOCASE")
    # Someone settled through F&F before this month started is never paid again (EXIT-02).
    month_start = orgtime.month_bounds(period)[0].isoformat()
    settled = {
        row["employee_id"] for row in storage.query(
            "SELECT employee_id FROM employee_exits WHERE status = 'settled' AND last_working_day < ?",
            (month_start,),
        )
    }
    staff = [person for person in staff if person["id"] not in settled]

    with storage.transaction():
        run = get_run(storage, period)
        kept: dict[int, dict[str, Any]] = {}
        if run:
            _require_draft(run)
            if not reset_adjustments:
                kept = {
                    row["employee_id"]: row
                    for row in storage.query(
                        f"SELECT employee_id, {', '.join(ADJUSTMENT_FIELDS)} FROM payslips WHERE run_id = ?",
                        (run["id"],),
                    )
                }
            storage.execute_tx("DELETE FROM payslips WHERE run_id = ?", (run["id"],))
            storage.execute_tx(
                "UPDATE expense_claims SET payroll_run_id = NULL WHERE payroll_run_id = ? AND status = 'approved'",
                (run["id"],),
            )
            storage.execute_tx(
                "UPDATE payroll_runs SET basis_days = ?, generated_at = ?, note = NULL WHERE id = ?",
                (basis_days_for(storage, period), utcnow_iso(), run["id"]),
            )
        else:
            cursor = storage.execute_tx(
                "INSERT INTO payroll_runs (period, status, basis_days, generated_at) VALUES (?, 'draft', ?, ?)",
                (period, basis_days_for(storage, period), utcnow_iso()),
            )
            run = {"id": int(cursor.lastrowid), "period": period, "status": "draft",
                   "basis_days": basis_days_for(storage, period)}

        now = utcnow_iso()
        for employee in staff:
            calc = compute_slip(storage, settings, employee, period)
            _apply_adjustments(calc, kept.get(employee["id"]))
            storage.execute_tx(
                """
                INSERT INTO payslips (run_id, employee_id, period, basis_days, present_days, paid_leave_days,
                                      lop_days, monthly_gross, per_day_rate, earned_gross, pf, esi,
                                      professional_tax, tds, total_deductions, net_pay, created_at, updated_at,
                                      bonus, advance, other_deduction, tds_override, note, reimbursement, rounding,
                                      late_fine)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run["id"], employee["id"], period, calc["basis_days"], calc["present_days"],
                    calc["paid_leave_days"], calc["lop_days"], calc["monthly_gross"], calc["per_day_rate"],
                    calc["earned_gross"], calc["pf"], calc["esi"], calc["professional_tax"],
                    calc["tds"], calc["total_deductions"], calc["net_pay"], now, now,
                    calc["bonus"], calc["advance"], calc["other_deduction"], calc["tds_override"], calc["note"],
                    calc["reimbursement"], calc["rounding"], calc["late_fine"],
                ),
            )
            if calc["claim_ids"]:
                marks = ",".join("?" for _ in calc["claim_ids"])
                storage.execute_tx(
                    f"UPDATE expense_claims SET payroll_run_id = ? WHERE id IN ({marks})",
                    (run["id"], *calc["claim_ids"]),
                )

        row = storage.query_one(
            """
            SELECT COALESCE(SUM(earned_gross),0) AS gross, COALESCE(SUM(total_deductions),0) AS deductions,
                   COALESCE(SUM(net_pay),0) AS net, COUNT(*) AS n
            FROM payslips WHERE run_id = ?
            """,
            (run["id"],),
        ) or {}
        storage.execute_tx(
            "UPDATE payroll_runs SET total_gross = ?, total_deductions = ?, total_net = ? WHERE id = ?",
            (round(row.get("gross", 0), 2), round(row.get("deductions", 0), 2), round(row.get("net", 0), 2), run["id"]),
        )

    kept_count = sum(1 for row in kept.values() if any(row.get(f) for f in ADJUSTMENT_FIELDS))
    storage.audit(actor=actor, action="generate_payroll", entity="payroll_run", entity_id=run["id"],
                  detail=f"{period}: {len(staff)} payslip(s), {kept_count} kept adjustments")
    refreshed = get_run(storage, period)
    assert refreshed is not None
    refreshed["kept_adjustments"] = kept_count
    return refreshed


def _apply_adjustments(calc: dict[str, Any], kept: dict[str, Any] | None) -> None:
    """Fold HR's saved adjustments into a freshly computed slip."""
    kept = kept or {}
    bonus = float(kept.get("bonus") or 0)
    advance = float(kept.get("advance") or 0)
    other = float(kept.get("other_deduction") or 0)
    override = kept.get("tds_override")
    tds = float(override) if override is not None else calc["tds"]
    note = kept.get("note")
    statutory = calc["pf"] + calc["esi"] + calc["professional_tax"] + tds + calc.get("late_fine", 0.0)
    available = calc["earned_gross"] + bonus + calc["reimbursement"] - statutory
    if advance + other > max(0.0, available):
        # attendance changed since HR typed these: recover only what this month can carry
        excess = advance + other - max(0.0, available)
        cut_other = min(other, excess)
        other -= cut_other
        advance = max(0.0, advance - (excess - cut_other))
        note = ((note + " · ") if note else "") + "recovery reduced to fit this month's pay"
    total = statutory + advance + other
    calc.update(
        bonus=bonus, advance=round(advance, 2), other_deduction=round(other, 2), tds=round(tds, 2),
        tds_override=override, note=note, total_deductions=round(total, 2),
        **_settle(calc["earned_gross"], bonus, calc["reimbursement"], total),
    )


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
    tds: float | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply HR adjustments (bonus, advance, other deduction, TDS) to a payslip.

    ``tds=None`` keeps the computed income tax as it is.
    """
    slip = storage.query_one("SELECT * FROM payslips WHERE id = ?", (slip_id,))
    if slip is None:
        raise employees_service.ValidationError(f"Payslip #{slip_id} not found.")
    run = storage.query_one("SELECT * FROM payroll_runs WHERE id = ?", (slip["run_id"],))
    assert run is not None
    _require_draft(run)

    tds_override = slip.get("tds_override") if tds is None else round(tds, 2)
    tds = float(slip.get("tds") or 0) if tds is None else tds
    for label, value in (("bonus", bonus), ("advance", advance), ("other_deduction", other_deduction),
                         ("TDS", tds)):
        if value < 0:
            raise employees_service.ValidationError(f"{label.replace('_', ' ').title()} cannot be negative.")

    total_deductions = (slip["pf"] + slip["esi"] + slip["professional_tax"] + tds + advance + other_deduction
                        + float(slip.get("late_fine") or 0))
    reimbursement = float(slip.get("reimbursement") or 0)
    shortfall = total_deductions - (slip["earned_gross"] + bonus + reimbursement)
    if shortfall > 0.005:
        raise employees_service.ValidationError(
            f"Deductions exceed earnings by ₹{shortfall:,.2f} — reduce the advance recovery or other deduction."
        )
    settled = _settle(slip["earned_gross"], bonus, reimbursement, total_deductions)
    storage.execute(
        """
        UPDATE payslips SET bonus = ?, advance = ?, other_deduction = ?, tds = ?, total_deductions = ?,
                            net_pay = ?, rounding = ?, note = ?, tds_override = ?, updated_at = ?
        WHERE id = ?
        """,
        (bonus, advance, other_deduction, round(tds, 2), round(total_deductions, 2), settled["net_pay"],
         settled["rounding"], note, tds_override, utcnow_iso(), slip_id),
    )
    _recompute_totals(storage, slip["run_id"])
    storage.audit(actor=actor, action="update_payslip", entity="payslip", entity_id=slip_id,
                  detail=f"bonus={bonus} advance={advance} deduction={other_deduction} tds={tds}")
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
    if status in ("finalized", "paid"):
        _require_closable(storage, storage.settings, period)

    column = "finalized_at" if status == "finalized" else "paid_at"
    storage.execute(
        f"UPDATE payroll_runs SET status = ?, {column} = ? WHERE id = ?", (status, utcnow_iso(), run["id"])
    )
    if status in ("finalized", "paid"):
        storage.execute(
            "UPDATE expense_claims SET status = 'paid', note = COALESCE(note, ?) "
            "WHERE payroll_run_id = ? AND status = 'approved'",
            (f"reimbursed in {orgtime.month_label(period)} payroll", run["id"]),
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
    storage.execute(
        "UPDATE expense_claims SET status = 'approved' WHERE payroll_run_id = ? AND status = 'paid'", (run["id"],)
    )
    storage.audit(actor=actor, action="payroll_reopen", entity="payroll_run", entity_id=run["id"], detail=period)
    refreshed = get_run(storage, period)
    assert refreshed is not None
    return refreshed


def delete_run(storage: Storage, period: str, *, actor: str | None = None) -> None:
    run = get_run(storage, period)
    if run is None:
        return
    if run["status"] != "draft":
        raise employees_service.ValidationError(
            "Only a draft run can be deleted — a paid run is a permanent record."
            if run["status"] == "paid" else "Reopen the run first; only a draft run can be deleted."
        )
    storage.execute("DELETE FROM payslips WHERE run_id = ?", (run["id"],))
    storage.execute("UPDATE expense_claims SET payroll_run_id = NULL WHERE payroll_run_id = ?", (run["id"],))
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


def slip_totals(slips: list[dict[str, Any]]) -> dict[str, Any]:
    """Column totals for a list of payslips (no extra queries)."""
    return {
        "employees": len(slips),
        "gross": round(sum(s["earned_gross"] for s in slips), 2),
        "bonus": round(sum(s["bonus"] for s in slips), 2),
        "deductions": round(sum(s["total_deductions"] for s in slips), 2),
        "tds": round(sum(float(s.get("tds") or 0) for s in slips), 2),
        "net": round(sum(s["net_pay"] for s in slips), 2),
        "lop_days": round(sum(s["lop_days"] for s in slips), 2),
        "present_days": round(sum(s["present_days"] for s in slips), 2),
        "paid_leave_days": round(sum(s["paid_leave_days"] for s in slips), 2),
    }


def period_summary(storage: Storage, period: str) -> dict[str, Any]:
    run = get_run(storage, period)
    if not run:
        return {"run": None, "slips": [], "totals": {}, "attendance": {}}
    slips = slips_for_run(storage, run["id"])
    totals = slip_totals(slips)
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
        "TDS,Late Fines,Total Deductions,Reimbursement,Rounding,Net Pay,Note"
    )
    lines = [header]
    for slip in slips:
        fields = [
            slip["code"], slip["name"], slip.get("department") or "", slip.get("designation") or "",
            f"{slip['monthly_gross']:.2f}", slip["basis_days"], f"{slip['present_days']:g}",
            f"{slip['paid_leave_days']:g}", f"{slip['lop_days']:g}", f"{slip['earned_gross']:.2f}",
            f"{slip['bonus']:.2f}", f"{slip['advance']:.2f}", f"{slip['other_deduction']:.2f}",
            f"{slip['pf']:.2f}", f"{slip['esi']:.2f}", f"{slip['professional_tax']:.2f}",
            f"{float(slip.get('tds') or 0):.2f}", f"{float(slip.get('late_fine') or 0):.2f}",
            f"{slip['total_deductions']:.2f}",
            f"{float(slip.get('reimbursement') or 0):.2f}", f"{float(slip.get('rounding') or 0):.2f}", f"{slip['net_pay']:.2f}", (slip.get("note") or "").replace(",", ";"),
        ]
        lines.append(",".join(_csv_safe(value) for value in fields))
    return "\n".join(lines) + "\n"


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text
