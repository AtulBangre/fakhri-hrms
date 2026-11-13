from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest
from app.config import Settings
from app.notify import (
    format_whatsapp_chat_id,
    get_waha_session_info,
    get_waha_qr_image,
    send_test_whatsapp,
    request_waha_pairing_code,
    _send_waha_whatsapp,
)


def test_format_whatsapp_chat_id():
    assert format_whatsapp_chat_id("9876543210") == "919876543210@c.us"
    assert format_whatsapp_chat_id("+91 98765-43210") == "919876543210@c.us"
    assert format_whatsapp_chat_id("09876543210") == "919876543210@c.us"
    assert format_whatsapp_chat_id("919876543210") == "919876543210@c.us"
    assert format_whatsapp_chat_id("1234567890123@c.us") == "1234567890123@c.us"


@patch("httpx.get")
def test_get_waha_session_info(mock_get):
    settings = Settings()
    settings.waha_api_url = "http://localhost:3000"
    settings.waha_api_key = "secret"
    settings.waha_session = "default"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "WORKING",
        "me": {"id": "919876543210@c.us", "pushName": "Company Office"},
        "engine": {"engine": "NOWEB"},
    }
    mock_get.return_value = mock_resp

    info = get_waha_session_info(settings)
    assert info["online"] is True
    assert info["status"] == "WORKING"
    assert info["me"]["pushName"] == "Company Office"


@patch("httpx.post")
def test_send_test_whatsapp(mock_post):
    settings = Settings()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    res = send_test_whatsapp(settings, "9876543210", "Hello test")
    assert res["ok"] is True
    assert res["chat_id"] == "919876543210@c.us"


@patch("httpx.post")
def test_send_waha_whatsapp_text(mock_post):
    settings = Settings()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    storage = MagicMock()
    row = {
        "recipient": "9876543210",
        "body": "Attendance marked IN",
        "meta": "{}",
    }
    res = _send_waha_whatsapp(storage, settings, row)
    assert res == "waha message sent"


@patch("httpx.post")
@patch("app.notify.get_waha_session_info")
def test_request_waha_pairing_code(mock_session_info, mock_post):
    mock_session_info.return_value = {"status": "SCAN_QR_CODE", "online": True}
    settings = Settings()
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"code": "3Z5Y-E3M5"}
    mock_post.return_value = mock_resp

    res = request_waha_pairing_code(settings, "9876543210")
    assert res["ok"] is True
    assert res["code"] == "3Z5Y-E3M5"
    assert res["phone"] == "919876543210"


def test_whatsapp_new_templates():
    from app.notify import _whatsapp_text
    settings = Settings()
    settings.org_name = "Fakhri IT Services"

    # Regularization
    txt = _whatsapp_text("regularization_decided", settings, date="11 Nov 2026", status="Approved", note="Approved by HR")
    assert "Attendance correction" in txt or "missed-punch" in txt
    assert "Approved" in txt

    # Expense
    txt_exp = _whatsapp_text("expense_update", settings, category="Travel", amount="1,500.00", status="approved")
    assert "Travel" in txt_exp
    assert "1,500.00" in txt_exp

    # Birthday
    txt_bday = _whatsapp_text("birthday_wish", settings, name="Farhan")
    assert "Happy Birthday" in txt_bday
    assert "Farhan" in txt_bday

    # Attendance reminder
    txt_rem = _whatsapp_text("attendance_reminder", settings, message="Your shift started at 09:30 AM.")
    assert "Your shift started at 09:30 AM." in txt_rem

    # HR summary
    txt_sum = _whatsapp_text("hr_attendance_summary", settings, date="12 Nov 2026", present=15, late=2, half_day=1, leave=1, not_marked=0)
    assert "Today's Attendance Summary" in txt_sum
    assert "Present: 15" in txt_sum


def test_trainee_employment_type(storage):
    from app.services import employees as employees_service
    emp_id = employees_service.create_employee(
        storage,
        {
            "name": "Sameer Khan",
            "employment_type": "trainee",
            "monthly_gross": 0,
            "phone": "9876543210",
        },
    )
    emp = employees_service.get_employee(storage, emp_id)
    assert emp["employment_type"] == "trainee"
    assert emp["monthly_gross"] == 0
    assert emp["status"] == "active"


def test_loan_reject_enqueues_email_and_whatsapp(hr_client, storage):
    from app.services import employees as employees_service
    from app.services import loans as loans_service
    from app import auth
    from tests.helpers import csrf_of

    token = csrf_of(hr_client, "/loans")
    emp_id = employees_service.create_employee(
        storage,
        {"name": "Pooja Sharma", "email": "pooja@example.com", "phone": "919876543210"},
    )
    user = auth.create_user(
        storage,
        username="pooja",
        password="secretpassword",
        role="employee",
        employee_id=emp_id,
    )
    loan_id = loans_service.apply_loan(
        storage,
        employee_id=emp_id,
        loan_type="personal_loan",
        principal_amount=150000.0,
        monthly_emi=10000.0,
        reason="Medical emergency",
    )
    # Perform rejection via route
    res = hr_client.post(f"/loans/{loan_id}/reject", data={"_csrf": token, "reason": "Insufficient tenure"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/loans")

    # Check notifications
    pushes = storage.query("SELECT * FROM notifications WHERE channel = 'push'")
    assert any("Personal Loan Rejected" in p["subject"] for p in pushes)

    # Check that email was enqueued
    emails = storage.query("SELECT * FROM notifications WHERE channel = 'email' AND recipient = ?", ("pooja@example.com",))
    assert len(emails) >= 1
    assert "Personal Loan Request Not Approved" in emails[-1]["subject"]
    assert "Insufficient tenure" in emails[-1]["body"]

    # Check that WhatsApp was enqueued
    was = storage.query("SELECT * FROM notifications WHERE channel = 'whatsapp' AND recipient LIKE '%9876543210'")
    assert len(was) >= 1
    assert "rejected" in was[-1]["body"].lower()


def test_attendance_reminders_enqueues_whatsapp_with_phone(storage, settings):
    from app.services import employees as employees_service
    from app.services import alerts as alerts_service
    from app import auth
    from datetime import datetime, timezone

    emp_id = employees_service.create_employee(
        storage,
        {"name": "Rohan Verma", "email": "rohan@example.com", "phone": "9876500001", "joined_on": "2026-01-01"},
    )
    user = auth.create_user(
        storage,
        username="rohan",
        password="secretpassword",
        role="employee",
        employee_id=emp_id,
    )
    storage.set_setting("notify_whatsapp_reminders", "1")
    # Simulate morning reminder after shift start
    now = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    alerts_service._attendance_reminders(storage, settings, now)

    was = storage.query("SELECT * FROM notifications WHERE channel = 'whatsapp' AND recipient LIKE '%9876500001'")
    assert len(was) >= 1
    assert "reminder" in was[-1]["body"].lower()





