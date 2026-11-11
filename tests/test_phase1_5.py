"""Tests for Phase 1.5: Bank Payout NEFT, Granular Finalize/On-Hold, and Leave Balance Export/Import."""

from __future__ import annotations

import io
from datetime import date
import pytest
from fastapi.testclient import TestClient

from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import payroll as payroll_service
from tests.helpers import csrf_of


def test_bank_payout_csv_and_hold_exclusion(storage, settings, staff):
    emp1 = staff[0]
    emp2 = staff[1]

    employees_service.update_employee(
        storage,
        emp1["id"],
        {
            "bank_name": "HDFC Bank",
            "account_number": "123456789012",
            "ifsc": "HDFC0001234",
            "account_holder": "Aarav Sharma",
        },
    )
    employees_service.update_employee(
        storage,
        emp2["id"],
        {
            "bank_name": "ICICI Bank",
            "account_number": "987654321098",
            "ifsc": "ICIC0000001",
            "account_holder": "Isha Verma",
        },
    )

    period = "2026-06"
    payroll_service.generate_run(storage, settings, period)
    slips = payroll_service.list_slips(storage, period)
    assert len(slips) >= 2

    # Put emp2 on hold
    slip2 = next(s for s in slips if s["employee_id"] == emp2["id"])
    payroll_service.set_slip_status(
        storage, slip2["id"], "on_hold", actor="hr_test", reason="Attendance dispute"
    )

    refreshed2 = payroll_service.get_slip(storage, slip2["id"])
    assert refreshed2["status"] == "on_hold"
    assert refreshed2["hold_reason"] == "Attendance dispute"

    # Export Bank Payout CSV
    payout_csv = payroll_service.export_bank_payout_csv(storage, period)
    assert "Beneficiary Account Number,Beneficiary Name,IFSC Code,Amount,Narration,Employee Code,Bank Name" in payout_csv
    assert "123456789012" in payout_csv
    assert "HDFC0001234" in payout_csv
    assert emp1["code"] in payout_csv

    # emp2 is on hold, so must NOT appear in payout csv
    assert "987654321098" not in payout_csv

    # Register CSV should contain all employees with bank columns and status
    reg_csv = payroll_service.payroll_csv(storage, period)
    assert "Bank Name,Account Number,IFSC" in reg_csv
    assert "on_hold" in reg_csv
    assert "123456789012" in reg_csv
    assert "987654321098" in reg_csv


def test_bulk_slip_status_and_release(storage, settings, staff):
    period = "2026-06"
    payroll_service.generate_run(storage, settings, period)
    slips = payroll_service.list_slips(storage, period)
    slip_ids = [s["id"] for s in slips[:2]]

    # Bulk finalize 2 slips
    count = payroll_service.bulk_set_slip_status(
        storage, period, slip_ids, "finalized", actor="hr_test"
    )
    assert count == 2

    for sid in slip_ids:
        s = payroll_service.get_slip(storage, sid)
        assert s["status"] == "finalized"
        assert s["finalized_by"] == "hr_test"

    # Release back to draft
    count = payroll_service.bulk_set_slip_status(
        storage, period, [slip_ids[0]], "draft", actor="hr_test"
    )
    assert count == 1
    s0 = payroll_service.get_slip(storage, slip_ids[0])
    assert s0["status"] == "draft"


def test_leave_dual_date_formatting(storage, settings, staff):
    emp = staff[0]
    # Request a leave
    res = leave_service.apply_leave(
        storage,
        settings,
        employee_id=emp["id"],
        leave_type="paid",
        from_date="2026-11-20",
        to_date="2026-11-22",
        reason="Family vacation",
        status="pending",
        allow_negative=True,
    )
    leave_id = res["leave"]["id"]
    assert leave_id > 0

    leaves = leave_service.list_leave(storage, status="pending")
    req = next(l for l in leaves if l["id"] == leave_id)
    # Check that applied_date, applied_time, and applied_label are present
    assert req.get("applied_date")
    assert req.get("applied_label")
    assert req["from_label"] == "20 Nov 2026"
    assert req["to_label"] == "22 Nov 2026"


def test_leave_balances_export_and_bulk_import(storage, settings, staff):
    # Export CSV
    csv_text = leave_service.export_balances_csv(storage, settings)
    assert "Employee Code,Employee Name,Department" in csv_text
    assert "Accrued PL,PL Taken,PL Available" in csv_text
    assert staff[0]["code"] in csv_text

    # Template CSV
    template_csv = leave_service.get_balances_template_csv(storage)
    assert "Employee Code,Employee Name,Department,Custom Annual PL,Custom Annual SL,Custom Annual CL" in template_csv
    assert staff[0]["code"] in template_csv

    # Bulk Import CSV
    import_text = (
        "Employee Code,Employee Name,Department,Custom Annual PL,Custom Annual SL,Custom Annual CL\n"
        f"{staff[0]['code']},{staff[0]['name']},Tech,15,8,7\n"
        f"{staff[1]['code']},{staff[1]['name']},Design,18,10,8\n"
    )
    res = leave_service.import_balances_csv(storage, import_text, actor="hr_test")
    assert res["updated"] == 2
    assert len(res["errors"]) == 0

    emp0 = employees_service.get_employee(storage, staff[0]["id"])
    assert emp0["custom_pl_accrual"] == 15.0
    assert emp0["custom_sl_quota"] == 8.0
    assert emp0["custom_cl_quota"] == 7.0


def test_payroll_and_leave_web_routes(hr_client: TestClient, staff, settings, storage):
    period = "2026-06"
    payroll_service.generate_run(storage, settings, period)
    slips = payroll_service.list_slips(storage, period)
    slip1 = slips[0]

    # Test Bank Payout CSV route
    resp_payout = hr_client.get(f"/payroll/export-payout.csv?period={period}")
    assert resp_payout.status_code == 200
    assert "Beneficiary Account Number,Beneficiary Name" in resp_payout.text

    # Test single slip status (put on hold)
    token = csrf_of(hr_client, f"/payroll?period={period}")
    resp_hold = hr_client.post(
        f"/payroll/slip/{slip1['id']}/status",
        data={
            "_csrf": token,
            "status": "on_hold",
            "reason": "Tax verification",
        },
        follow_redirects=True,
    )
    assert resp_hold.status_code == 200
    s_refreshed = payroll_service.get_slip(storage, slip1["id"])
    assert s_refreshed["status"] == "on_hold"

    # Test leave balances export route
    resp_lexport = hr_client.get("/leave/balances/export.csv")
    assert resp_lexport.status_code == 200
    assert "Employee Code,Employee Name" in resp_lexport.text

    # Test leave balances template route
    resp_ltmpl = hr_client.get("/leave/balances/template.csv")
    assert resp_ltmpl.status_code == 200
    assert "Custom Annual PL" in resp_ltmpl.text
