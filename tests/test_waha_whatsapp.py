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



