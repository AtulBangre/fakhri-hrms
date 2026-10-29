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
from app.services import employees as employees_service, leave as leave_service, orgtime
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
