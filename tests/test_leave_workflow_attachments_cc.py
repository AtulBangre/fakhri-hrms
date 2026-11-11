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
