"""Bulk Employee Import with 18-month historical disbursed salary recording.

Handles CSV template generation with dynamic last 18-month column headers,
flexible date parsing, employee creation, portal login generation,
and historical payroll_run + payslip persistence.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from typing import Any

from .. import auth
from ..config import Settings
from ..db import Storage, utcnow_iso
from ..services import employees as employees_service
from ..services import orgtime
from ..services.payroll import basis_days_for


def get_past_months(count: int = 18, reference_date: date | None = None) -> list[str]:
    """Return past ``count`` months in YYYY-MM format going backwards from reference_date."""
    if reference_date is None:
        reference_date = date.today()
    periods = []
    y, m = reference_date.year, reference_date.month
    for _ in range(count):
        periods.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return periods


def parse_date_flexible(val: str) -> str:
    """Parse dates in YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY, etc."""
    raw = (val or "").strip()
    if not raw:
        raise ValueError("Date is required")

    # Match YYYY-MM-DD
    m1 = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m1:
        y, m, d = int(m1.group(1)), int(m1.group(2)), int(m1.group(3))
        return date(y, m, d).isoformat()

    # Match DD-MM-YYYY or DD/MM/YYYY
    m2 = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", raw)
    if m2:
        d, m, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return date(y, m, d).isoformat()

    # Fallback to standard ISO parser
    return date.fromisoformat(raw[:10]).isoformat()


def clean_number(val: Any) -> float | None:
    """Clean string number into float, returning None if empty or invalid."""
    if val is None:
        return None
    s = str(val).replace("₹", "").replace(",", "").strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if f >= 0 else None
    except ValueError:
        return None


def generate_sample_csv(storage: Storage, settings: Settings) -> str:
    """Generate sample CSV template with standard fields and last 18 months salary headers."""
    today = orgtime.today(settings)
    past_months = get_past_months(18, today)

    salary_headers = [f"salary_{p.replace('-', '_')}" for p in past_months]

    base_headers = [
        "name", "code", "joined_on", "monthly_gross", "department", "designation",
        "employment_type", "email", "phone", "pan", "aadhaar_last4", "uan", "esi_number",
        "bank_name", "bank_account_number", "bank_ifsc",
    ]

    fieldnames = base_headers + salary_headers
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()

    # Sample Row 1: Long-term employee with full 18 months of historical salaries
    sample_1: dict[str, Any] = {
        "name": "Rajesh Sharma",
        "code": "FSPL02030010",
        "joined_on": past_months[-1] + "-01",  # joined 18 months ago
        "monthly_gross": "55000",
        "department": "Engineering",
        "designation": "Senior Software Engineer",
        "employment_type": "full_time",
        "email": "rajesh.sharma@example.com",
        "phone": "9876543210",
        "pan": "ABCDE1234F",
        "aadhaar_last4": "1234",
        "uan": "100123456789",
        "esi_number": "31001234560001001",
        "bank_name": "HDFC Bank",
        "bank_account_number": "50100123456789",
        "bank_ifsc": "HDFC0001234",
    }
    for p, h in zip(past_months, salary_headers):
        sample_1[h] = "55000"
    writer.writerow(sample_1)

    # Sample Row 2: Joined 14 months ago (earlier 4 months intentionally left blank)
    joined_14_m = past_months[13] if len(past_months) > 13 else past_months[-1]
    sample_2: dict[str, Any] = {
        "name": "Pooja Patel",
        "code": "FSPL02030011",
        "joined_on": joined_14_m + "-15",
        "monthly_gross": "42000",
        "department": "Marketing",
        "designation": "Growth Specialist",
        "employment_type": "full_time",
        "email": "pooja.patel@example.com",
        "phone": "9812345678",
        "pan": "PQRST5678G",
        "aadhaar_last4": "5678",
        "uan": "",
        "esi_number": "",
        "bank_name": "ICICI Bank",
        "bank_account_number": "123405001234",
        "bank_ifsc": "ICIC0001234",
    }
    # Only fill recent 14 months; earlier months remain blank
    for i, h in enumerate(salary_headers):
        if i < 14:
            sample_2[h] = "42000"
        else:
            sample_2[h] = ""
    writer.writerow(sample_2)

    return out.getvalue()


def record_historical_payslip(
    storage: Storage,
    *,
    employee_id: int,
    period: str,
    salary_amount: float,
) -> None:
    """Store or update disbursed salary for period in payroll_runs & payslips."""
    if salary_amount <= 0:
        return

    now = utcnow_iso()
    b_days = basis_days_for(storage, period)

    # 1. Ensure a paid payroll_run exists for this period
    run = storage.query_one("SELECT id, status FROM payroll_runs WHERE period = ?", (period,))
    if run:
        run_id = int(run["id"])
    else:
        cursor = storage.execute(
            """
            INSERT INTO payroll_runs (period, status, basis_days, total_gross, total_deductions,
                                      total_net, generated_at, finalized_at, paid_at, note)
            VALUES (?, 'paid', ?, 0, 0, 0, ?, ?, ?, 'Historical payroll import')
            """,
            (period, b_days, now, now, now),
        )
        run_id = int(cursor.lastrowid)

    # 2. Check if a payslip already exists for this employee in this run
    per_day = round(salary_amount / b_days, 2)
    existing_slip = storage.query_one(
        "SELECT id FROM payslips WHERE run_id = ? AND employee_id = ?",
        (run_id, employee_id),
    )

    if existing_slip:
        storage.execute(
            """
            UPDATE payslips SET
                monthly_gross = ?, earned_gross = ?, net_pay = ?, per_day_rate = ?,
                note = 'Historical disbursed salary (updated)', updated_at = ?
            WHERE id = ?
            """,
            (salary_amount, salary_amount, salary_amount, per_day, now, existing_slip["id"]),
        )
    else:
        storage.execute(
            """
            INSERT INTO payslips (
                run_id, employee_id, period, basis_days, present_days, paid_leave_days,
                lop_days, monthly_gross, per_day_rate, earned_gross, pf, esi,
                professional_tax, tds, total_deductions, net_pay, created_at, updated_at,
                bonus, advance, other_deduction, tds_override, note, reimbursement, rounding, late_fine
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 0, 0, 0, 0, 0, ?, ?, ?, 0, 0, 0, 0,
                    'Historical disbursed salary', 0, 0, 0)
            """,
            (
                run_id, employee_id, period, b_days, b_days,
                salary_amount, per_day, salary_amount, salary_amount, now, now,
            ),
        )

    # 3. Keep run totals synchronized
    totals = storage.query_one(
        """
        SELECT COALESCE(SUM(earned_gross), 0) AS gross,
               COALESCE(SUM(total_deductions), 0) AS deductions,
               COALESCE(SUM(net_pay), 0) AS net
        FROM payslips WHERE run_id = ?
        """,
        (run_id,),
    )
    storage.execute(
        "UPDATE payroll_runs SET total_gross = ?, total_deductions = ?, total_net = ? WHERE id = ?",
        (totals["gross"], totals["deductions"], totals["net"], run_id),
    )


def import_employees_csv(
    storage: Storage,
    settings: Settings,
    csv_text: str,
    *,
    actor: str = "hr",
) -> dict[str, Any]:
    """Parse CSV, validate each row, create employees & save historical salaries."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("The uploaded CSV is empty or has no header row.")

    # Identify salary columns: salary_YYYY_MM, salary_YYYY-MM, YYYY-MM, YYYY_MM
    salary_col_map: dict[str, str] = {}
    for col in reader.fieldnames:
        clean_col = col.strip()
        m = re.search(r"(?:salary_)?(\d{4})[-_](\d{2})", clean_col, re.IGNORECASE)
        if m:
            period = f"{m.group(1)}-{m.group(2)}"
            salary_col_map[col] = period

    success_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []

    for row_idx, row in enumerate(reader, start=2):  # row 1 is header
        # Strip all keys and values
        clean_row = {str(k).strip(): str(v).strip() if v is not None else "" for k, v in row.items()}
        name = clean_row.get("name", "").strip()
        code = clean_row.get("code", "").strip().upper()

        # Skip completely blank lines
        if not any(clean_row.values()):
            continue

        try:
            if not name:
                raise ValueError("Employee name is required.")

            raw_joined = clean_row.get("joined_on", "").strip()
            if not raw_joined:
                raise ValueError("Date of joining (joined_on) is required.")

            joined_on = parse_date_flexible(raw_joined)

            raw_gross = clean_row.get("monthly_gross", "")
            monthly_gross = clean_number(raw_gross)
            if monthly_gross is None or monthly_gross <= 0:
                raise ValueError(f"Monthly gross salary must be a positive number (got '{raw_gross}').")

            # Validate or generate employee code
            if code and employees_service.get_employee_by_code(storage, code):
                raise ValueError(f"Employee code '{code}' is already used by another employee.")

            employee_data: dict[str, Any] = {
                "name": name,
                "code": code or None,
                "joined_on": joined_on,
                "monthly_gross": monthly_gross,
                "department": clean_row.get("department") or None,
                "designation": clean_row.get("designation") or None,
                "employment_type": clean_row.get("employment_type") or "full_time",
                "email": clean_row.get("email") or None,
                "phone": clean_row.get("phone") or None,
                "pan": clean_row.get("pan") or None,
                "aadhaar_last4": clean_row.get("aadhaar_last4") or None,
                "uan": clean_row.get("uan") or None,
                "esi_number": clean_row.get("esi_number") or None,
                "bank_name": clean_row.get("bank_name") or None,
                "account_number": clean_row.get("bank_account_number") or clean_row.get("account_number") or None,
                "ifsc": clean_row.get("bank_ifsc") or clean_row.get("ifsc") or None,
                "notes": f"Bulk imported via CSV on {date.today().isoformat()}",
                "_actor": actor,
            }

            # Optional breakup
            basic = clean_number(clean_row.get("basic"))
            if basic:
                employee_data["basic"] = basic
            hra = clean_number(clean_row.get("hra"))
            if hra:
                employee_data["hra"] = hra
            other_allowances = clean_number(clean_row.get("other_allowances"))
            if other_allowances:
                employee_data["other_allowances"] = other_allowances

            emp_id = employees_service.create_employee(storage, employee_data)
            created_emp = employees_service.get_employee(storage, emp_id)

            # Auto create portal login
            try:
                auth.ensure_employee_login(storage, created_emp)
            except Exception:
                pass  # non-fatal if login fails

            # Process historical salary columns
            salaries_saved = 0
            for col_name, period in salary_col_map.items():
                salary_val = clean_number(clean_row.get(col_name))
                if salary_val is not None and salary_val > 0:
                    record_historical_payslip(
                        storage,
                        employee_id=emp_id,
                        period=period,
                        salary_amount=salary_val,
                    )
                    salaries_saved += 1

            storage.audit(
                actor=actor,
                action="bulk_import_employee",
                entity="employee",
                entity_id=emp_id,
                detail=f"Imported {name} ({created_emp['code']}) with {salaries_saved} historical payslips",
            )

            success_rows.append({
                "row": row_idx,
                "id": emp_id,
                "name": name,
                "code": created_emp["code"],
                "salaries_saved": salaries_saved,
            })

        except Exception as exc:
            failed_rows.append({
                "row": row_idx,
                "name": name or "—",
                "code": code or "—",
                "error": str(exc),
                "raw_data": clean_row,
            })

    return {
        "total": len(success_rows) + len(failed_rows),
        "success_count": len(success_rows),
        "failed_count": len(failed_rows),
        "success_rows": success_rows,
        "failed_rows": failed_rows,
        "original_headers": reader.fieldnames,
    }
