"""Tests for leave attachments, formatted content, and CC email hierarchy."""

from __future__ import annotations

import io
import json
import pytest
from starlette.datastructures import UploadFile

from app import notify
from app.services import employees as employees_service
from app.services import leave as leave_service
from app.uploads import path_for


def test_leave_apply_with_attachments_and_cc_outbox(client, storage, settings, staff):
    emp = staff[0]
    manager = staff[1]

    # Assign manager and valid emails
    employees_service.update_employee(storage, emp["id"], {"manager_id": manager["id"], "email": "employee@example.com"})
    employees_service.update_employee(storage, manager["id"], {"email": "manager@example.com"})

    # Attachments metadata
    attachments = [
        {"original_name": "medical_slip.pdf", "stored_name": "fake_medical.pdf", "size": 1024, "mime": "application/pdf"}
    ]

    # Apply leave
    res = leave_service.apply_leave(
        storage,
        settings,
        employee_id=emp["id"],
        leave_type="sick",
        from_date="2026-11-20",
        to_date="2026-11-20",
        reason="Dear Team,\n\nI am unwell and unable to attend work.\n\nThanks.",
        status="pending",
        allow_negative=True,
        attachments=attachments,
    )
    leave = res["leave"]
    assert leave["status"] == "pending_manager"
    assert "fake_medical.pdf" in leave["attachments"]

    # Check list_leave parses attachment_files
    leaves = leave_service.list_leave(storage, employee_id=emp["id"], status="all")
    assert len(leaves) >= 1
    found = [l for l in leaves if l["id"] == leave["id"]][0]
    assert len(found["attachment_files"]) == 1
    assert found["attachment_files"][0]["original_name"] == "medical_slip.pdf"


def test_leave_notification_cc_hierarchy(storage, settings, staff):
    emp = staff[0]
    manager = staff[1]

    employees_service.update_employee(storage, emp["id"], {"manager_id": manager["id"], "email": "employee@example.com"})
    employees_service.update_employee(storage, manager["id"], {"email": "manager@example.com"})

    # Clear notifications
    storage.execute("DELETE FROM notifications")

    # 1. Notify HR on apply (with manager CC)
    hr_addrs = notify.hr_emails(storage, settings)
    notify.notify_hr(
        storage,
        settings,
        "leave_requested",
        category="leave_requested",
        name=emp["name"],
        code=emp["code"],
        department=emp.get("department") or "Engineering",
        manager_name=manager["name"],
        from_date="20 Nov 2026",
        to_date="20 Nov 2026",
        days=1.0,
        leave_type="Sick Leave",
        reason="Unwell",
        attachments=[{"original_name": "slip.pdf", "stored_name": "slip.pdf"}],
        cc=["manager@example.com"],
        reply_to="employee@example.com",
    )

    rows = storage.query("SELECT * FROM notifications ORDER BY id DESC")
    assert len(rows) == 1
    hr_msg = rows[0]
    assert hr_msg["recipient"] in hr_addrs
    meta = json.loads(hr_msg["meta"])
    assert meta["cc"] == ["manager@example.com"]
    assert meta["reply_to"] == "employee@example.com"
    assert "slip.pdf" in hr_msg["body"]

    # 2. Manager rejection notification
    storage.execute("DELETE FROM notifications")
    notify.queue_email(
        storage,
        "employee@example.com",
        "leave_decided",
        settings=settings,
        category="leave_rejected",
        name=emp["name"],
        from_date="20 Nov 2026",
        to_date="20 Nov 2026",
        days=1.0,
        decision="rejected",
        note="Critical release on that day",
        leave_type="Sick Leave",
        decided_by=f"Reporting Manager ({manager['name']})",
        cc=["manager@example.com", hr_addrs[0]],
    )

    rows = storage.query("SELECT * FROM notifications ORDER BY id DESC")
    assert len(rows) == 1
    rej_msg = rows[0]
    assert rej_msg["recipient"] == "employee@example.com"
    meta = json.loads(rej_msg["meta"])
    assert "manager@example.com" in meta["cc"]
    assert hr_addrs[0] in meta["cc"]


def test_leave_attachment_download(hr_client, storage, settings):
    # Create a dummy attachment file in uploads/leave/
    from app.uploads import upload_dir
    ldir = upload_dir(settings, "leave")
    test_path = ldir / "test_file_sample.txt"
    test_path.write_text("Leave attachment sample content", encoding="utf-8")

    resp = hr_client.get("/leave/attachment/test_file_sample.txt")
    assert resp.status_code == 200
    assert b"Leave attachment sample content" in resp.content


def test_leave_balances_xlsx_export_and_import(hr_client, storage, settings, staff):
    emp1 = staff[0]
    emp2 = staff[1]

    # 1. Export Excel
    resp = hr_client.get("/leave/balances/export.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    xlsx_bytes = resp.content
    assert xlsx_bytes.startswith(b"PK\x03\x04")

    # 2. Parse exported rows
    rows = leave_service.parse_spreadsheet_rows(xlsx_bytes, "leave_balances.xlsx")
    assert len(rows) >= 3  # Header + at least 2 staff
    assert "Employee Code" in rows[0]
    assert "Unpaid LOP" in rows[0]
    assert "PL Quota" in rows[0]

    # 3. Import with No Change
    res1 = leave_service.import_balances_data(storage, rows, actor="hr_test")
    assert res1["errors"] == []
    assert res1["updated"] == 0
    assert res1["unchanged"] == res1["total_rows"]

    # 4. Modify one employee's quota, keep other unchanged, and introduce an error row
    # Row format: Employee Code, Employee Name, Department, PL Quota, SL Quota, CL Quota, Unpaid LOP, Total Available
    modified_rows = [
        rows[0],
        # Update emp1 PL from 6 to 12
        [emp1["code"], emp1["name"], emp1.get("department") or "", "12.0", "6.0", "6.0", "0", "24.0"],
        # Keep emp2 unchanged
        rows[2],
        # Invalid employee code
        ["INVALID_CODE_999", "Fake Person", "Dept", "5.0", "5.0", "5.0", "0", "15.0"],
        # Invalid negative number
        [emp1["code"], emp1["name"], "Dept", "-10", "6.0", "6.0", "0", "0"],
    ]

    res2 = leave_service.import_balances_data(storage, modified_rows, actor="hr_test")
    assert res2["updated"] == 1
    assert res2["unchanged"] >= 1
    assert len(res2["errors"]) == 2  # Missing code and negative number
    assert any("INVALID_CODE_999" in e for e in res2["errors"])
    assert any("negative" in e for e in res2["errors"])

    # Verify emp1 got updated in DB
    refreshed_emp1 = employees_service.get_employee(storage, emp1["id"])
    assert float(refreshed_emp1["custom_pl_accrual"]) == 12.0


def test_employee_leave_history_api(hr_client, storage, settings, staff):
    emp = staff[0]
    leave_res = leave_service.apply_leave(
        storage,
        settings,
        employee_id=emp["id"],
        leave_type="paid",
        from_date="2026-11-28",
        to_date="2026-11-29",
        reason="Family trip",
        status="approved",
        allow_negative=True,
    )

    resp = hr_client.get(f"/leave/employee/{emp['id']}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["employee"]["id"] == emp["id"]
    assert "balance" in data
    assert len(data["leaves"]) >= 1
    assert data["leaves"][0]["reason"] == "Family trip"
