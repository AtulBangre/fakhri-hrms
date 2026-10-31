"""Tests for newly enhanced HRMS features:
1. Dynamic SL & CL settings in /settings
2. Employee leave application confirmation email to both HR and Employee
3. Shift Master: Multiple shifts create, make default, and delete
4. Individual employee custom leave quotas override
"""

from __future__ import annotations

from datetime import date
import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.config import SETTING_DEFS
from app.db import Storage
from app.services import attendance as attendance_service, employees as employees_service, leave as leave_service, orgtime
from tests.conftest import HR_PASSWORD, HR_USERNAME
from tests.helpers import csrf_of


def test_leave_quotas_in_settings_defs(client: TestClient, hr_client: TestClient, storage: Storage):
    """Verify sl_annual_quota and cl_annual_quota exist in SETTING_DEFS and can be updated."""
    keys = [defn[0] for defn in SETTING_DEFS]
    assert "sl_annual_quota" in keys
    assert "cl_annual_quota" in keys

    # Verify updating via /settings/value
    token = csrf_of(hr_client, "/settings")
    resp = hr_client.post("/settings/value", data={"_csrf": token, "key": "sl_annual_quota", "value": "10.0"}, follow_redirects=True)
    assert resp.status_code == 200
    assert storage.get_float("sl_annual_quota", 7.0) == 10.0

    resp2 = hr_client.post("/settings/value", data={"_csrf": token, "key": "cl_annual_quota", "value": "12.0"}, follow_redirects=True)
    assert resp2.status_code == 200
    assert storage.get_float("cl_annual_quota", 8.0) == 12.0


def test_shift_management_crud(client: TestClient, hr_client: TestClient, storage: Storage):
    """Verify creating, making default, and deleting shifts via /settings/shifts."""
    token = csrf_of(hr_client, "/settings")

    # 1. Create a new shift
    resp = hr_client.post("/settings/shifts", data={
        "_csrf": token,
        "name": "Evening Shift",
        "start_time": "14:00",
        "end_time": "22:30",
        "grace_minutes": 20,
        "half_day_minutes": 240,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Evening Shift" in resp.text

    created = storage.query_one("SELECT * FROM shifts WHERE name = 'Evening Shift'")
    assert created is not None
    assert created["start_time"] == "14:00"
    assert created["grace_minutes"] == 20
    assert created["is_default"] == 0

    # 2. Duplicate name validation
    resp_dup = hr_client.post("/settings/shifts", data={
        "_csrf": token,
        "name": "Evening Shift",
        "start_time": "14:00",
        "end_time": "22:30",
        "grace_minutes": 15,
        "half_day_minutes": 240,
    }, follow_redirects=True)
    assert "already exists" in resp_dup.text

    # 3. Make default
    resp_def = hr_client.post(f"/settings/shifts/{created['id']}/default", data={"_csrf": token}, follow_redirects=True)
    assert resp_def.status_code == 200
    assert storage.query_one("SELECT is_default FROM shifts WHERE id = ?", (created["id"],))["is_default"] == 1

    # 4. Cannot delete default shift
    resp_del_def = hr_client.post(f"/settings/shifts/{created['id']}/delete", data={"_csrf": token}, follow_redirects=True)
    assert "Cannot delete the default shift" in resp_del_def.text

    # 5. Set General Shift back to default
    gen = storage.query_one("SELECT id FROM shifts WHERE name = 'General Shift'")
    hr_client.post(f"/settings/shifts/{gen['id']}/default", data={"_csrf": token}, follow_redirects=True)

    # 6. Delete Evening Shift
    resp_del = hr_client.post(f"/settings/shifts/{created['id']}/delete", data={"_csrf": token}, follow_redirects=True)
    assert resp_del.status_code == 200
    assert storage.query_one("SELECT id FROM shifts WHERE name = 'Evening Shift'") is None


def test_leave_application_emails_both_hr_and_employee(client: TestClient, hr_client: TestClient, storage: Storage, settings):
    """Verify that when an employee applies for leave, emails are queued for BOTH HR and the employee."""
    # Ensure HR user has an email
    storage.execute("UPDATE users SET email = 'hr@fakhriitservices.com' WHERE role = 'hr'")

    emp_id = employees_service.create_employee(storage, {
        "name": "Rahul Verma",
        "code": "RV001",
        "email": "rahul.verma@example.com",
        "joined_on": "2026-01-01",
        "monthly_gross": 40000,
    })
    user_id = auth.create_user(storage, username="rahulv", password="Password123!", role="employee", employee_id=emp_id)

    emp_client = TestClient(client.app)
    login_resp = emp_client.post("/login", data={"username": "rahulv", "password": "Password123!"}, follow_redirects=False)
    assert login_resp.status_code == 303

    # Clear notifications table
    storage.execute("DELETE FROM notifications")

    # Rahul applies for leave
    token = csrf_of(emp_client, "/me/leave")
    apply_resp = emp_client.post("/me/leave", data={
        "_csrf": token,
        "leave_type": "paid",
        "from_date": "2026-11-10",
        "to_date": "2026-11-11",
        "reason": "Family function",
    }, follow_redirects=True)
    assert apply_resp.status_code == 200

    # Verify notifications queued
    notifs = storage.query("SELECT * FROM notifications ORDER BY id ASC")
    recipients = [n["recipient"] for n in notifs]

    # Both HR and Employee must receive email notifications
    assert "hr@fakhriitservices.com" in recipients
    assert "rahul.verma@example.com" in recipients

    emp_notif = next(n for n in notifs if n["recipient"] == "rahul.verma@example.com")
    assert "Leave Request Submitted" in emp_notif["subject"]
    assert "Rahul" in emp_notif["body"]
    assert "Family function" not in emp_notif["body"]  # clean summary for employee


def test_individual_employee_custom_leave_quota_override(storage: Storage, settings):
    """Verify that custom leave quotas override company defaults for specific employees."""
    # Default settings: PL = 1.5/mo, SL = 7.0/yr, CL = 8.0/yr
    storage.set_setting("pl_accrual_per_month", "1.5")
    storage.set_setting("sl_annual_quota", "7.0")
    storage.set_setting("cl_annual_quota", "8.0")

    # Employee 1: Standard employee (follows company defaults)
    emp1_id = employees_service.create_employee(storage, {
        "name": "Standard Emp",
        "code": "STD01",
        "joined_on": "2026-01-01",
        "monthly_gross": 30000,
    })

    # Employee 2: Executive with custom leave quota overrides
    emp2_id = employees_service.create_employee(storage, {
        "name": "Executive Emp",
        "code": "EXEC01",
        "joined_on": "2026-01-01",
        "monthly_gross": 90000,
        "custom_pl_accrual": 2.5,
        "custom_sl_quota": 12.0,
        "custom_cl_quota": 15.0,
    })

    as_of = date(2026, 7, 1) # 6 months of service

    b1 = leave_service.balance(storage, settings, emp1_id, as_of=as_of)
    b2 = leave_service.balance(storage, settings, emp2_id, as_of=as_of)

    # Standard employee: 7 months * 1.5 = 10.5 PL, 7.0 SL, 8.0 CL
    assert b1["accrual_per_month"] == 1.5
    assert b1["accrued"] == 10.5
    q1 = {q["code"]: q["allocated"] for q in b1["quotas"]}
    assert q1["PL"] == 10.5
    assert q1["SL"] == 7.0
    assert q1["CL"] == 8.0

    # Executive employee: 7 months * 2.5 = 17.5 PL, 12.0 SL, 15.0 CL
    assert b2["accrual_per_month"] == 2.5
    assert b2["accrued"] == 17.5
    q2 = {q["code"]: q["allocated"] for q in b2["quotas"]}
    assert q2["PL"] == 17.5
    assert q2["SL"] == 12.0
    assert q2["CL"] == 15.0


def test_email_reply_to_and_bcc_hr(storage: Storage, settings, tmp_path):
    """Verify outgoing emails carry proper Reply-To, Bcc to hr@fakhriitservices.com, and clean From."""
    from email import policy
    from email.parser import BytesParser
    from pathlib import Path

    settings.data_dir = tmp_path
    settings.smtp_host = ""  # console driver writes to outbox
    settings.smtp_from = '"Fakhri HRMS <k6263638053@gmail.com>"'
    settings.org_name = "Fakhri IT Services"

    row_id = notify.queue_email(
        storage, "emp@example.com", "welcome", settings=settings,
        category="welcome", name="Aman", username="aman01", password="SecretPassword1!",
    )
    assert row_id is not None
    notify.dispatch(storage, settings, limit=10)

    outbox = Path(settings.data_dir) / "outbox"
    eml_files = list(outbox.glob("*.eml"))
    assert len(eml_files) > 0

    msg = BytesParser(policy=policy.default).parsebytes(eml_files[-1].read_bytes())
    assert "Fakhri IT Services" in msg["From"]
    assert "k6263638053@gmail.com" in msg["From"]
    assert "Not Provided" not in msg["From"]
    assert "hr@fakhriitservices.com" in msg["Reply-To"]
    assert "hr@fakhriitservices.com" in msg["Bcc"]
    assert "https://hrms.fakhriitservices.com/login" in msg.get_body().get_content()


def test_attendance_clear_get_redirect(hr_client: TestClient):
    """Verify GET /attendance/clear safely redirects to /attendance without 405 error."""
    resp = hr_client.get("/attendance/clear", follow_redirects=False)
    assert resp.status_code == 303
    assert "/attendance" in resp.headers["location"]


def test_manual_time_update_promotes_late_half_day_to_present(storage: Storage):
    """Verify that when an employee was marked half-day due to late arrival,
    and HR edits check-in to an on-time hour, it automatically promotes to Present."""
    emp_id = employees_service.create_employee(storage, {
        "name": "Test Emp",
        "code": "TST001",
        "joined_on": "2026-01-01",
    })
    # Simulate a late scan that resulted in half_day
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, check_in, late_minutes, source, note, created_at, updated_at)
        VALUES (?, '2026-10-29', 'half_day', '17:59:00', 449, 'scan', 'late arrival — half day', '2026-10-29T12:00:00', '2026-10-29T12:00:00')
        """,
        (emp_id,)
    )

    # HR updates check-in time to 10:01 (on time) while form may have still had status='half_day'
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-29",
        status="half_day",
        check_in="10:01",
        note="late arrival — half day",
        actor="hr",
    )

    row = attendance_service.get_row(storage, emp_id, "2026-10-29")
    assert row["status"] == "present"
    assert row["check_in"] == "10:01:00"
    assert row["note"] is None


def test_pagarbook_attendance_rules(storage: Storage):
    """Verify PagarBook-style attendance rules:
    - Present with blank check-out leaves check_out and worked_minutes as None.
    - Absent clears check_in, check_out, worked_minutes, and leave_type.
    - On Leave clears check_in and check_out.
    - Manual override with HR note is not downgraded to half-day.
    """
    emp_id = employees_service.create_employee(storage, {
        "name": "PagarBook Emp",
        "code": "PGB001",
        "joined_on": "2026-01-01",
    })

    # 1. Present with no check-out (today / active shift)
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="present",
        check_in="10:07",
        check_out="",
        actor="hr",
    )
    r1 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r1["status"] == "present"
    assert r1["check_in"] == "10:07:00"
    assert r1["check_out"] is None
    assert r1["worked_minutes"] is None

    # 2. Marking absent clears times
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="absent",
        check_in="10:07",
        check_out="18:30",
        actor="hr",
    )
    r2 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r2["status"] == "absent"
    assert r2["check_in"] is None
    assert r2["check_out"] is None
    assert r2["worked_minutes"] is None
    assert r2["leave_type"] is None

    # 3. Marking on leave clears times and sets leave_type
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="leave",
        leave_type="sick",
        check_in="10:07",
        actor="hr",
    )
    r3 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r3["status"] == "leave"
    assert r3["check_in"] is None
    assert r3["check_out"] is None
    assert r3["leave_type"] == "sick"

    # 4. Manual override note protects Present from late half-day downgrade
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-30",
        status="present",
        check_in="11:45",
        note="approved client visit",
        actor="hr",
    )
    r4 = attendance_service.get_row(storage, emp_id, "2026-10-30")
    assert r4["status"] == "present"
    assert r4["note"] == "approved client visit"



