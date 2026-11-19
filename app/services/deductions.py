"""Deduction & penalty records: monthly employee deductions, late fines, asset/damage recoveries, and waivers."""

from __future__ import annotations

import logging
from typing import Any

from ..db import Storage, utcnow_iso
from . import employees as employees_service
from . import locks, orgtime

logger = logging.getLogger("hrms.deductions")

DEDUCTION_CATEGORIES = (
    "late_fine",
    "damage_recovery",
    "salary_advance",
    "disciplinary",
    "uniform",
    "other",
)

CATEGORY_LABELS = {
    "late_fine": "Late Arrival Fine",
    "damage_recovery": "Damage / Asset Recovery",
    "salary_advance": "Salary Advance",
    "disciplinary": "Disciplinary Penalty",
    "uniform": "Uniform / Asset Charges",
    "other": "Other Deduction",
}


def list_deductions(
    storage: Storage,
    period: str,
    *,
    employee_id: int | None = None,
    category: str | None = None,
    status: str | None = "active",
    department: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """List deductions for a period with joined employee info and flexible filtering."""
    clauses = ["d.period = ?"]
    params: list[Any] = [period]

    if employee_id:
        clauses.append("d.employee_id = ?")
        params.append(employee_id)

    if category and category != "all":
        clauses.append("d.category = ?")
        params.append(category)

    if status and status != "all":
        clauses.append("d.status = ?")
        params.append(status)

    if department and department != "all":
        clauses.append("e.department = ? COLLATE NOCASE")
        params.append(department.strip())

    if q and q.strip():
        clause, words = employees_service.search_clause(q, ("e.name", "e.code", "d.reason"))
        if clause:
            clauses.append(clause)
            params.extend(words)

    sql = f"""
        SELECT d.*, e.code AS employee_code, e.name AS employee_name, e.department AS employee_department
        FROM monthly_deductions d
        JOIN employees e ON e.id = d.employee_id
        WHERE {' AND '.join(clauses)}
        ORDER BY d.created_at DESC, d.id DESC
    """
    rows = storage.query(sql, tuple(params))
    for r in rows:
        r["category_label"] = CATEGORY_LABELS.get(r["category"], r["category"].replace("_", " ").title())
    return rows


def get_deduction(storage: Storage, deduction_id: int) -> dict[str, Any] | None:
    row = storage.query_one(
        """
        SELECT d.*, e.code AS employee_code, e.name AS employee_name, e.department AS employee_department
        FROM monthly_deductions d
        JOIN employees e ON e.id = d.employee_id
        WHERE d.id = ?
        """,
        (deduction_id,),
    )
    if row:
        row["category_label"] = CATEGORY_LABELS.get(row["category"], row["category"].replace("_", " ").title())
    return row


def add_deduction(
    storage: Storage,
    *,
    employee_id: int,
    period: str,
    amount: float,
    reason: str,
    category: str = "other",
    work_date: str | None = None,
    actor: str | None = None,
) -> int:
    """Add a new monthly deduction or penalty for an employee."""
    employee = employees_service.require_employee(storage, employee_id)
    locks.assert_period_open(storage, f"{period}-01")

    if amount <= 0:
        raise employees_service.ValidationError("Deduction amount must be greater than zero.")
    if not (reason or "").strip():
        raise employees_service.ValidationError("A reason for the deduction is required.")
    if category not in DEDUCTION_CATEGORIES:
        category = "other"

    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO monthly_deductions (employee_id, period, work_date, category, amount, reason, status, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (employee_id, period, work_date, category, round(amount, 2), reason.strip(), actor, now, now),
    )
    deduction_id = cursor.lastrowid
    storage.audit(
        actor=actor,
        action="add_deduction",
        entity="monthly_deductions",
        entity_id=deduction_id,
        detail=f"employee={employee['code']} period={period} category={category} amount={amount:.2f} reason={reason}",
    )
    return deduction_id


def update_deduction(
    storage: Storage,
    deduction_id: int,
    *,
    amount: float,
    reason: str,
    category: str = "other",
    actor: str | None = None,
) -> dict[str, Any]:
    """Edit amount, category or reason of an existing deduction."""
    row = get_deduction(storage, deduction_id)
    if not row:
        raise employees_service.ValidationError("Deduction entry not found.")
    locks.assert_period_open(storage, f"{row['period']}-01")

    if amount <= 0:
        raise employees_service.ValidationError("Deduction amount must be greater than zero.")
    if not (reason or "").strip():
        raise employees_service.ValidationError("A reason for the deduction is required.")
    if category not in DEDUCTION_CATEGORIES:
        category = "other"

    now = utcnow_iso()
    storage.execute(
        """
        UPDATE monthly_deductions
        SET amount = ?, reason = ?, category = ?, updated_at = ?
        WHERE id = ?
        """,
        (round(amount, 2), reason.strip(), category, now, deduction_id),
    )
    storage.audit(
        actor=actor,
        action="update_deduction",
        entity="monthly_deductions",
        entity_id=deduction_id,
        detail=f"old_amount={row['amount']} new_amount={amount:.2f} category={category} reason={reason}",
    )
    updated = get_deduction(storage, deduction_id)
    assert updated is not None
    return updated


def waive_deduction(
    storage: Storage,
    deduction_id: int,
    *,
    reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Waive a deduction so it does not deduct from payroll while preserving history."""
    row = get_deduction(storage, deduction_id)
    if not row:
        raise employees_service.ValidationError("Deduction entry not found.")
    locks.assert_period_open(storage, f"{row['period']}-01")

    now = utcnow_iso()
    waiver_note = (reason or "").strip() or "Waived by HR"
    updated_reason = f"{row['reason']} [WAIVED: {waiver_note}]"
    storage.execute(
        """
        UPDATE monthly_deductions
        SET status = 'waived', reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (updated_reason, now, deduction_id),
    )
    storage.audit(
        actor=actor,
        action="waive_deduction",
        entity="monthly_deductions",
        entity_id=deduction_id,
        detail=f"amount={row['amount']} reason={waiver_note}",
    )
    updated = get_deduction(storage, deduction_id)
    assert updated is not None
    return updated


def delete_deduction(storage: Storage, deduction_id: int, *, actor: str | None = None) -> None:
    """Delete a deduction entry entirely."""
    row = get_deduction(storage, deduction_id)
    if not row:
        raise employees_service.ValidationError("Deduction entry not found.")
    locks.assert_period_open(storage, f"{row['period']}-01")

    storage.execute("DELETE FROM monthly_deductions WHERE id = ?", (deduction_id,))
    storage.audit(
        actor=actor,
        action="delete_deduction",
        entity="monthly_deductions",
        entity_id=deduction_id,
        detail=f"employee={row['employee_code']} amount={row['amount']} reason={row['reason']}",
    )


def stats_for_period(storage: Storage, period: str) -> dict[str, Any]:
    """Summary metrics for the monthly deductions dashboard."""
    total_active = float(storage.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM monthly_deductions WHERE period = ? AND status = 'active'",
        (period,),
    ))
    late_fines = float(storage.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM monthly_deductions WHERE period = ? AND category = 'late_fine' AND status = 'active'",
        (period,),
    ))
    damages_penalties = float(storage.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM monthly_deductions WHERE period = ? AND category IN ('damage_recovery', 'disciplinary') AND status = 'active'",
        (period,),
    ))
    waived = float(storage.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM monthly_deductions WHERE period = ? AND status = 'waived'",
        (period,),
    ))
    impacted_employees = int(storage.scalar(
        "SELECT COUNT(DISTINCT employee_id) FROM monthly_deductions WHERE period = ? AND status = 'active'",
        (period,),
    ))

    # Also sum raw attendance late_fines for comparison
    start, end = orgtime.month_bounds(period)
    attendance_fines = float(storage.scalar(
        "SELECT COALESCE(SUM(late_fine), 0) FROM attendance WHERE work_date BETWEEN ? AND ? AND late_fine > 0",
        (start.isoformat(), end.isoformat()),
    ))

    return {
        "period": period,
        "total_active": round(total_active, 2),
        "late_fines": round(late_fines, 2),
        "damages_penalties": round(damages_penalties, 2),
        "waived": round(waived, 2),
        "impacted_employees": impacted_employees,
        "attendance_fines": round(attendance_fines, 2),
    }


def employee_deductions_for_period(storage: Storage, employee_id: int, period: str) -> list[dict[str, Any]]:
    """Active deductions for a specific employee in a payroll period."""
    rows = storage.query(
        """
        SELECT * FROM monthly_deductions
        WHERE employee_id = ? AND period = ? AND status = 'active'
        ORDER BY id ASC
        """,
        (employee_id, period),
    )
    for r in rows:
        r["category_label"] = CATEGORY_LABELS.get(r["category"], r["category"].replace("_", " ").title())
    return rows


def sync_attendance_fines(storage: Storage, period: str, *, actor: str | None = None) -> int:
    """Consolidate non-zero attendance late_fines into monthly_deductions if not already present."""
    start, end = orgtime.month_bounds(period)
    rows = storage.query(
        """
        SELECT a.id, a.employee_id, a.work_date, a.late_fine, a.note, e.code, e.name
        FROM attendance a
        JOIN employees e ON e.id = a.employee_id
        WHERE a.work_date BETWEEN ? AND ? AND a.late_fine > 0
        """,
        (start.isoformat(), end.isoformat()),
    )
    synced_count = 0
    now = utcnow_iso()
    for r in rows:
        existing = storage.query_one(
            """
            SELECT id FROM monthly_deductions
            WHERE employee_id = ? AND work_date = ? AND category = 'late_fine'
            """,
            (r["employee_id"], r["work_date"]),
        )
        if not existing:
            reason = f"Late arrival on {r['work_date']}" + (f" ({r['note']})" if r.get("note") else "")
            storage.execute(
                """
                INSERT INTO monthly_deductions (employee_id, period, work_date, category, amount, reason, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'late_fine', ?, ?, 'active', ?, ?, ?)
                """,
                (r["employee_id"], period, r["work_date"], float(r["late_fine"]), reason, actor or "system_sync", now, now),
            )
            synced_count += 1
    return synced_count
