"""Employee Loan and Salary Advance Management Service.

Handles disbursing loans/advances, monthly EMI recovery through payroll,
tracking repayment schedules, and loan closure.
"""

from __future__ import annotations

from typing import Any
from ..db import Storage, utcnow_iso
from . import employees as employees_service


LOAN_TYPES = {
    "salary_advance": "Salary Advance",
    "personal_loan": "Personal Loan",
    "emergency_loan": "Emergency Loan",
}


def create_loan(
    storage: Storage,
    *,
    employee_id: int,
    principal_amount: float,
    monthly_emi: float,
    disbursed_on: str,
    loan_type: str = "salary_advance",
    reason: str | None = None,
    actor: str | None = None,
) -> int:
    """Disburse a new loan or advance to an active employee."""
    emp = employees_service.require_employee(storage, employee_id)
    if principal_amount <= 0:
        raise employees_service.ValidationError("Principal amount must be greater than zero.")
    if monthly_emi <= 0:
        raise employees_service.ValidationError("Monthly EMI must be greater than zero.")
    if monthly_emi > principal_amount:
        monthly_emi = principal_amount

    now = utcnow_iso()
    cursor = storage.execute(
        """
        INSERT INTO employee_loans (
            employee_id, loan_type, principal_amount, monthly_emi,
            total_repaid, remaining_balance, status, disbursed_on,
            reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?, 'active', ?, ?, ?, ?)
        """,
        (
            employee_id, loan_type, principal_amount, monthly_emi,
            principal_amount, disbursed_on, reason or None, now, now
        ),
    )
    loan_id = int(cursor.lastrowid)
    storage.audit(
        actor=actor,
        action="create_loan",
        entity="employee_loan",
        entity_id=loan_id,
        detail=f"emp={emp['code']} type={loan_type} amount={principal_amount:,.2f} emi={monthly_emi:,.2f}",
    )
    return loan_id


def get_loan(storage: Storage, loan_id: int) -> dict[str, Any] | None:
    """Fetch loan details along with borrower and repayment history."""
    row = storage.query_one(
        """
        SELECT l.*, e.name AS employee_name, e.code AS employee_code, e.department
        FROM employee_loans l
        JOIN employees e ON e.id = l.employee_id
        WHERE l.id = ?
        """,
        (loan_id,),
    )
    if not row:
        return None
    loan = dict(row)
    loan["type_label"] = LOAN_TYPES.get(loan["loan_type"], loan["loan_type"])
    repayments = storage.query(
        "SELECT * FROM loan_repayments WHERE loan_id = ? ORDER BY repaid_at DESC",
        (loan_id,),
    )
    loan["repayments"] = [dict(r) for r in repayments]
    loan["repaid_pct"] = (
        round((loan["total_repaid"] / loan["principal_amount"]) * 100, 1)
        if loan["principal_amount"] > 0
        else 100.0
    )
    return loan


def list_loans(
    storage: Storage,
    *,
    status: str | None = None,
    employee_id: int | None = None,
) -> list[dict[str, Any]]:
    """List loans with optional status/employee filters."""
    clauses = []
    params = []
    if status and status != "all":
        clauses.append("l.status = ?")
        params.append(status)
    if employee_id:
        clauses.append("l.employee_id = ?")
        params.append(employee_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = storage.query(
        f"""
        SELECT l.*, e.name AS employee_name, e.code AS employee_code, e.department
        FROM employee_loans l
        JOIN employees e ON e.id = l.employee_id
        {where}
        ORDER BY CASE l.status WHEN 'active' THEN 0 ELSE 1 END, l.created_at DESC
        """,
        params,
    )
    results = []
    for r in rows:
        d = dict(r)
        d["type_label"] = LOAN_TYPES.get(d["loan_type"], d["loan_type"])
        d["repaid_pct"] = (
            round((d["total_repaid"] / d["principal_amount"]) * 100, 1)
            if d["principal_amount"] > 0
            else 100.0
        )
        results.append(d)
    return results


def get_active_loan_for_employee(storage: Storage, employee_id: int) -> dict[str, Any] | None:
    """Fetch the current active loan with pending balance for an employee."""
    row = storage.query_one(
        """
        SELECT * FROM employee_loans
        WHERE employee_id = ? AND status = 'active' AND remaining_balance > 0
        ORDER BY id ASC LIMIT 1
        """,
        (employee_id,),
    )
    return dict(row) if row else None


def record_repayment(
    storage: Storage,
    *,
    loan_id: int,
    amount: float,
    payroll_run_id: int | None = None,
    payslip_id: int | None = None,
    period: str = "",
    note: str | None = None,
    actor: str | None = None,
) -> None:
    """Record an EMI repayment and adjust remaining balance."""
    loan = storage.query_one("SELECT * FROM employee_loans WHERE id = ?", (loan_id,))
    if not loan:
        raise ValueError(f"Loan #{loan_id} not found.")

    amount = round(float(amount), 2)
    if amount <= 0:
        return

    now = utcnow_iso()
    storage.execute(
        """
        INSERT INTO loan_repayments (loan_id, payroll_run_id, payslip_id, amount, period, repaid_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (loan_id, payroll_run_id, payslip_id, amount, period, now, note or None),
    )

    new_total_repaid = round(loan["total_repaid"] + amount, 2)
    new_remaining = max(0.0, round(loan["remaining_balance"] - amount, 2))
    new_status = "closed" if new_remaining <= 0.0 else loan["status"]

    storage.execute(
        """
        UPDATE employee_loans
        SET total_repaid = ?, remaining_balance = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_total_repaid, new_remaining, new_status, now, loan_id),
    )
    storage.audit(
        actor=actor,
        action="record_loan_repayment",
        entity="employee_loan",
        entity_id=loan_id,
        detail=f"amount={amount:,.2f} remaining={new_remaining:,.2f} status={new_status}",
    )


def close_loan(storage: Storage, loan_id: int, *, actor: str | None = None, reason: str | None = None) -> None:
    """Manually close or settle a loan."""
    now = utcnow_iso()
    storage.execute(
        "UPDATE employee_loans SET status = 'closed', remaining_balance = 0, updated_at = ? WHERE id = ?",
        (now, loan_id),
    )
    storage.audit(
        actor=actor,
        action="close_loan",
        entity="employee_loan",
        entity_id=loan_id,
        detail=f"manually closed: {reason or 'no reason'}",
    )


def loan_summary(storage: Storage) -> dict[str, Any]:
    """Summary metrics across all company loans."""
    row = storage.query_one(
        """
        SELECT
            COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_count,
            COALESCE(SUM(principal_amount), 0) AS total_disbursed,
            COALESCE(SUM(total_repaid), 0) AS total_repaid,
            COALESCE(SUM(CASE WHEN status = 'active' THEN remaining_balance ELSE 0 END), 0) AS outstanding
        FROM employee_loans
        """
    )
    if not row:
        return {"active_count": 0, "total_disbursed": 0.0, "total_repaid": 0.0, "outstanding": 0.0}
    return {
        "active_count": int(row["active_count"] or 0),
        "total_disbursed": float(row["total_disbursed"] or 0),
        "total_repaid": float(row["total_repaid"] or 0),
        "outstanding": float(row["outstanding"] or 0),
    }

