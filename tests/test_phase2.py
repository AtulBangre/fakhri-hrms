"""Phase 2 Integration & Unit Tests.

Covers:
1. Reporting Manager Hierarchy & Multi-level Approvals (Employee -> Manager -> HR).
2. Self-reporting manager validation.
3. Employee Loan & Advance Recovery Ledger (disbursal, automated payroll EMI deduction & repayment recording).
4. Shift assignment & overtime attributes.
"""

from __future__ import annotations

from datetime import date
import pytest

from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import loans as loans_service
from app.services import payroll as payroll_service
from app.services import orgtime


def test_reporting_manager_assignment_and_validation(storage, staff):
    emp1 = staff[0]
    emp2 = staff[1]

    # Assign emp2 as emp1's reporting manager
    employees_service.update_employee(storage, emp1["id"], {"manager_id": emp2["id"]})
    updated_emp1 = employees_service.get_employee(storage, emp1["id"])
    assert updated_emp1["manager_id"] == emp2["id"]

    # Verify self-reporting manager is blocked
    with pytest.raises(employees_service.ValidationError, match="cannot be their own reporting manager"):
        employees_service.update_employee(storage, emp1["id"], {"manager_id": emp1["id"]})


def test_multilevel_leave_approval_workflow(storage, settings, staff):
    emp = staff[0]
    manager = staff[1]

    # Set manager
    employees_service.update_employee(storage, emp["id"], {"manager_id": manager["id"]})

    # Employee requests leave
    res = leave_service.apply_leave(
        storage,
        settings,
        employee_id=emp["id"],
        leave_type="paid",
        from_date="2026-11-24",
        to_date="2026-11-25",
        reason="Family function",
        status="pending",
        allow_negative=True,
    )
    leave = res["leave"]
    assert leave["status"] == "pending_manager"
    assert leave["manager_status"] == "pending"

    # Manager's team inbox receives the request
    team_leaves = leave_service.list_manager_team_leave(storage, manager["id"], status="pending_manager")
    assert any(tl["id"] == leave["id"] for tl in team_leaves)

    # Another employee who is NOT manager cannot approve
    with pytest.raises(employees_service.ValidationError, match="not the designated reporting manager"):
        leave_service.manager_decide_leave(
            storage, leave["id"], manager_employee_id=staff[2]["id"], decision="approved"
        )

    # Manager approves with a recommendation note
    decided = leave_service.manager_decide_leave(
        storage,
        leave["id"],
        manager_employee_id=manager["id"],
        decision="approved",
        note="Approved, work delegated to team",
    )
    assert decided["status"] == "pending"
    assert decided["manager_status"] == "approved"
    assert decided["manager_note"] == "Approved, work delegated to team"

    # Now HR sees it in their queue as pending with manager approval
    hr_pending = leave_service.pending_requests(storage)
    req_for_hr = next(r for r in hr_pending if r["id"] == leave["id"])
    assert req_for_hr["manager_status"] == "approved"

    # HR final approval stamps attendance
    approved_res = leave_service.approve_leave(storage, leave_id=leave["id"], actor="disha_hr", allow_negative=True)
    assert approved_res["leave"]["status"] == "approved"


def test_employee_loan_ledger_and_payroll_emi_deduction(storage, settings, staff):
    emp = staff[0]
    period = "2026-10"

    # Disburse a personal loan of 20,000 with 5,000 monthly EMI
    loan_id = loans_service.create_loan(
        storage,
        employee_id=emp["id"],
        principal_amount=20000.0,
        monthly_emi=5000.0,
        disbursed_on="2026-10-01",
        loan_type="personal_loan",
        reason="Emergency home renovation",
        actor="hr_admin",
    )
    assert loan_id > 0

    loan = loans_service.get_loan(storage, loan_id)
    assert loan["principal_amount"] == 20000.0
    assert loan["remaining_balance"] == 20000.0
    assert loan["status"] == "active"

    # Generate payroll run for 2026-10
    run = payroll_service.generate_run(storage, settings, period)
    slips = payroll_service.slips_for_run(storage, run["id"])
    emp_slip = next(s for s in slips if s["employee_id"] == emp["id"])

    # EMI must be automatically calculated and included in advance deduction
    assert emp_slip["loan_id"] == loan_id
    assert emp_slip["loan_emi"] == 5000.0
    assert emp_slip["advance"] == 5000.0
    assert "Loan #" in (emp_slip["advance_note"] or "")

    # Finalize the run
    payroll_service.set_run_status(storage, period, "finalized", actor="hr_admin")

    # Verify repayment was automatically recorded and balance reduced
    loan_after = loans_service.get_loan(storage, loan_id)
    assert loan_after["total_repaid"] == 5000.0
    assert loan_after["remaining_balance"] == 15000.0
    assert len(loan_after["repayments"]) == 1
    assert loan_after["repayments"][0]["amount"] == 5000.0

    # Manual settlement / close loan
    loans_service.close_loan(storage, loan_id, actor="hr_admin", reason="Full cash settlement")
    closed_loan = loans_service.get_loan(storage, loan_id)
    assert closed_loan["status"] == "closed"
    assert closed_loan["remaining_balance"] == 0.0


def test_loan_summary_metrics(storage, staff):
    summary = loans_service.loan_summary(storage)
    assert "total_disbursed" in summary
    assert "total_repaid" in summary
    assert "outstanding" in summary
    assert "active_count" in summary


def test_manager_edit_selection_and_contact_email_display(client, storage, staff):
    # Log in as HR
    login_res = client.post("/login", data={"username": "hr", "password": "changeme123"}, follow_redirects=False)
    assert login_res.status_code == 303

    emp = staff[0]
    manager = staff[1]

    # Assign manager with an email
    employees_service.update_employee(
        storage, manager["id"], {"email": "manager.lead@company.com"}
    )
    employees_service.update_employee(
        storage, emp["id"], {"manager_id": manager["id"]}
    )

    # 1. Verify GET /employees/{emp_id}/edit has the manager option selected
    resp = client.get(f"/employees/{emp['id']}/edit")
    assert resp.status_code == 200
    html = resp.text
    # Should have option with manager.id and selected
    expected_option = f'<option value="{manager["id"]}" selected>'
    assert expected_option in html, f"Expected {expected_option} in edit form HTML"

    # 2. Verify GET /employees/{emp_id} shows manager's name, code and email under Contact
    detail_resp = client.get(f"/employees/{emp['id']}")
    assert detail_resp.status_code == 200
    detail_html = detail_resp.text
    assert manager["name"] in detail_html
    assert manager["code"] in detail_html
    assert "manager.lead@company.com" in detail_html
    assert "mailto:manager.lead@company.com" in detail_html

