"""Notifications, noticeboard, OTP reset and the profile-change workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.services import account as account_service
from app.services import announcements as announcement_service
from app.services import employees as employees_service
from app.services import leave as leave_service, payroll
from tests.conftest import HR_PASSWORD, HR_USERNAME
from tests.helpers import csrf_of


# ------------------------------------------------------------------- helpers
def outbox_files(settings) -> list[Path]:
    outbox = Path(settings.data_dir) / "outbox"
    return sorted(outbox.glob("*.eml")) if outbox.exists() else []


def last_email(settings) -> dict:
    files = outbox_files(settings)
    assert files, "no email was written to the outbox"
    message = BytesParser(policy=policy.default).parsebytes(files[-1].read_bytes())
    body = message.get_body(preferencelist=("html",))
    return {
        "to": message["to"],
        "subject": message["subject"],
        "html": body.get_content() if body else "",
        "attachments": [
            (part.get_filename(), part.get_payload(decode=True))
            for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ],
    }


# --------------------------------------------------------------- the outbox
def test_queue_and_dispatch_writes_a_real_email(storage, settings):
    row_id = notify.queue_email(
        storage, "priya@example.com", "welcome", settings=settings,
        category="welcome", name="Priya", username="fits002", password="secret123",
    )
    assert row_id
    assert notify.outbox_stats(storage)["pending"] == 1

    results = notify.dispatch(storage, settings)
    assert results and results[0]["ok"] is True

    email = last_email(settings)
    assert email["to"] == "priya@example.com"
    assert "portal" in email["subject"].lower()
    assert "fits002" in email["html"]
    assert notify.outbox_stats(storage)["sent"] == 1


def test_missing_recipient_is_not_queued(storage, settings):
    assert notify.queue_email(storage, "", "welcome", settings=settings, name="x", username="y") is None
    assert notify.queue_email(storage, None, "welcome", settings=settings, name="x", username="y") is None
    assert notify.outbox_stats(storage)["pending"] == 0


def test_whatsapp_and_sms_use_the_console_driver(storage, settings):
    notify.queue_whatsapp(
        storage, "+91 90000 00001", "punch_in", settings=settings,
        name="Aarav", time="09:32", date="2026-10-09", late_minutes=0, shift_start="09:30",
    )
    notify.queue_sms(
        storage, "+919000000002", "check_out", settings=settings,
        name="Ishita", time="18:10", worked="8h 38m",
    )
    results = notify.dispatch(storage, settings)
    assert all(row["ok"] for row in results)
    outbox = Path(settings.data_dir) / "outbox"
    assert list(outbox.glob("*-whatsapp-*.txt"))
    assert list(outbox.glob("*-sms-*.txt"))


def test_failed_message_is_retried_then_marked_failed(storage, settings):
    settings.smtp_host = "127.0.0.1"
    settings.smtp_port = 9          # nothing listens here
    settings.smtp_timeout = 1
    row_id = notify.queue_email(
        storage, "x@example.com", "welcome", settings=settings, name="x", username="y"
    )
    for _ in range(3):
        notify.dispatch(storage, settings)
        # skip the back-off wait so the next attempt is due now
        storage.execute("UPDATE notifications SET next_attempt_at = NULL WHERE id = ?", (row_id,))
    row = storage.query_one("SELECT * FROM notifications WHERE id = ?", (row_id,))
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert row["error"]

    settings.smtp_host = ""         # back to the console driver
    assert notify.retry_failed(storage) == 1
    assert notify.dispatch(storage, settings)[0]["ok"] is True


def test_every_template_renders(settings):
    cases = {
        "welcome": dict(name="A", username="a", password="p"),
        "leave_requested": dict(name="A", code="X1", leave_type="Paid", from_date="2026-01-01",
                                to_date="2026-01-02", days=2, reason=""),
        "leave_decided": dict(name="A", from_date="2026-01-01", to_date="2026-01-02", days=2,
                              decision="approved", note=""),
        "punch_in": dict(name="A", time="09:30", date="2026-01-01", late_minutes=0, shift_start="09:30"),
        "check_out": dict(name="A", time="18:30", worked="8h"),
        "payslip": dict(name="A", period="January 2026", gross="Rs 1", deductions="Rs 0", net="Rs 1"),
        "otp": dict(name="A", code="123456", minutes=10),
        "announcement": dict(title="T", body_html="<p>b</p>", date="2026-01-01"),
        "profile_changed": dict(name="A", fields="Phone"),
        "profile_decided": dict(name="A", decision="approved", field_label="Phone", note=""),
    }
    for template, ctx in cases.items():
        subject, html, text = notify.render(template, settings, **ctx)
        assert subject and html and text, template
        assert "<html" in html.lower()


# ------------------------------------------------------- attendance triggers
def test_kiosk_check_in_queues_a_confirmation_email(
    client: TestClient, storage, settings, staff, fake_face, assets, hr_user, kiosk_user
):
    from app.face import IdentificationResult

    employees_service.update_employee(storage, staff[0]["id"], {"email": "aarav@example.com"})
    storage.set_setting("shift_start", "23:00")  # never "late", whatever time the suite runs
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id=staff[0]["code"], similarity=0.9, face_id="f1",
        threshold=0.363, faces_in_probe=1,
    )
    client.post("/login", data={"username": "gate", "password": "gatepass123"})
    token = csrf_of(client, "/kiosk")
    response = client.post(
        "/api/kiosk/scan", data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert response.status_code == 200, response.text

    queued = storage.query("SELECT * FROM notifications WHERE category='attendance_punch_in'")
    assert len(queued) == 1
    assert queued[0]["recipient"] == "aarav@example.com"
    assert notify.dispatch(storage, settings)[0]["ok"] is True
    email = last_email(settings)
    assert "CHECK IN" in email["subject"].upper() or "IN at" in email["subject"]


def test_attendance_notifications_can_be_switched_off(
    client: TestClient, storage, settings, staff, fake_face, assets, hr_user, kiosk_user
):
    from app.face import IdentificationResult

    settings.notify_punch_in = False
    settings.notify_whatsapp_attendance = False
    employees_service.update_employee(storage, staff[0]["id"], {"email": "a@example.com"})
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id=staff[0]["code"], similarity=0.9, face_id="f1",
        threshold=0.363, faces_in_probe=1,
    )
    client.post("/login", data={"username": "gate", "password": "gatepass123"})
    token = csrf_of(client, "/kiosk")
    client.post(
        "/api/kiosk/scan", data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert storage.query("SELECT * FROM notifications WHERE category LIKE 'attendance%'") == []


# ------------------------------------------------------------------ leave
def test_leave_request_emails_hr_and_decision_emails_employee(
    client: TestClient, storage, settings, staff, ess_credentials, hr_user, kiosk_user
):
    employees_service.update_employee(storage, staff[0]["id"], {"email": "aarav@example.com"})
    hr = auth.get_user_by_username(storage, HR_USERNAME)
    storage.execute("UPDATE users SET email = ? WHERE id = ?", ("hr@example.com", hr["id"]))

    # employee requests
    assert client.post(
        "/login",
        data={"username": ess_credentials["username"], "password": ess_credentials["password"]},
        follow_redirects=False,
    ).status_code == 303
    token = csrf_of(client, "/me/leave")
    client.post(
        "/me/leave",
        data={"_csrf": token, "leave_type": "paid", "from_date": "2026-01-05", "to_date": "2026-01-06"},
        follow_redirects=False,
    )
    assert notify.dispatch(storage, settings)
    email = last_email(settings)
    assert email["to"] == "hr@example.com"
    assert "leave request" in email["subject"].lower()

    # HR approves
    leave_id = leave_service.list_leave(storage, employee_id=staff[0]["id"])[0]["id"]
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    hr_token = csrf_of(client, "/leave")
    client.post(f"/leave/{leave_id}/approve", data={"_csrf": hr_token, "period": "2026-01"},
                follow_redirects=False)
    notify.dispatch(storage, settings, limit=10) if False else notify.dispatch(storage, settings)
    emails = storage.query("SELECT * FROM notifications WHERE category='leave_approved'")
    assert len(emails) == 1
    assert emails[0]["recipient"] == "aarav@example.com"


# --------------------------------------------------------------- payslips
def test_bulk_payslip_email_attaches_a_pdf(
    hr_client: TestClient, storage, settings, staff
):
    employees_service.update_employee(storage, staff[0]["id"], {"email": "aarav@example.com"})
    payroll.generate_run(storage, settings, "2026-01")
    payroll.set_run_status(storage, "2026-01", "finalized")

    token = csrf_of(hr_client, "/payroll")
    response = hr_client.post(
        "/payroll/email",
        data={"_csrf": token, "period": "2026-01", "send_now": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    sent = storage.query("SELECT * FROM notifications WHERE category='payslip'")
    assert len(sent) == 1
    assert sent[0]["status"] == "sent"

    email = last_email(settings)
    assert email["attachments"], "payslip PDF was not attached"
    name, payload = email["attachments"][0]
    assert name.endswith(".pdf")
    assert payload.startswith(b"%PDF-")


def test_payslip_email_refuses_a_draft_run(hr_client: TestClient, storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-02")
    token = csrf_of(hr_client, "/payroll")
    response = hr_client.post(
        "/payroll/email", data={"_csrf": token, "period": "2026-02"}, follow_redirects=False
    )
    assert "err=" in response.headers["location"]
    assert storage.query("SELECT * FROM notifications WHERE category='payslip'") == []


def test_payslip_pdf_download(hr_client: TestClient, storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-03")
    slip = payroll.list_slips(storage, "2026-03")[0]
    response = hr_client.get(f"/payroll/slip/{slip['id']}.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "attachment" in response.headers["content-disposition"]


def test_employee_can_download_only_their_own_payslip_pdf(
    ess_client: TestClient, storage, settings, staff
):
    payroll.generate_run(storage, settings, "2026-04")
    payroll.set_run_status(storage, "2026-04", "finalized")  # employees only see final payslips
    mine = [s for s in payroll.list_slips(storage, "2026-04") if s["employee_id"] == staff[0]["id"]][0]
    other = [s for s in payroll.list_slips(storage, "2026-04") if s["employee_id"] == staff[1]["id"]][0]

    ok = ess_client.get(f"/me/payslips/{mine['id']}.pdf")
    assert ok.status_code == 200 and ok.content.startswith(b"%PDF-")

    blocked = ess_client.get(f"/me/payslips/{other['id']}.pdf", follow_redirects=False)
    assert blocked.status_code == 303
    assert "err=" in blocked.headers["location"]


# ------------------------------------------------------------ noticeboard
def test_announcement_shows_on_both_dashboards(
    hr_client: TestClient, storage, settings, staff, ess_credentials
):
    announcement_service.create(
        storage, title="Diwali holiday", body="Office closed 20-22 Oct.", level="warning",
        audience="all", pinned=True, actor="hr",
    )
    assert "Diwali holiday" in hr_client.get("/dashboard").text

    hr_client.post("/logout", data={"_csrf": csrf_of(hr_client, "/dashboard")}, follow_redirects=False)
    hr_client.post(
        "/login",
        data={"username": ess_credentials["username"], "password": ess_credentials["password"]},
        follow_redirects=False,
    )
    page = hr_client.get("/me").text
    assert "Diwali holiday" in page
    assert "Office closed" in page


def test_expired_or_hidden_announcements_disappear(
    hr_client: TestClient, storage, settings, staff
):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    announcement_service.create(
        storage, title="Old news", body="gone", expires_on=past, actor="hr"
    )
    assert "Old news" not in hr_client.get("/dashboard").text

    live = announcement_service.create(storage, title="Live news", body="here", actor="hr")
    assert "Live news" in hr_client.get("/dashboard").text
    announcement_service.toggle(storage, live["id"], actor="hr")
    assert "Live news" not in hr_client.get("/dashboard").text


def test_announcement_broadcast_queues_email_for_everyone_with_an_address(
    hr_client: TestClient, storage, settings, staff
):
    employees_service.update_employee(storage, staff[0]["id"], {"email": "one@example.com"})
    employees_service.update_employee(storage, staff[1]["id"], {"email": "two@example.com"})
    # staff[2] deliberately has no email

    token = csrf_of(hr_client, "/announcements")
    response = hr_client.post(
        "/announcements",
        data={"_csrf": token, "title": "Payroll released", "body": "Check your portal.",
              "level": "success", "audience": "all", "also_email": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    queued = storage.query("SELECT recipient FROM notifications WHERE category='announcement'")
    assert sorted(row["recipient"] for row in queued) == ["one@example.com", "two@example.com"]


# ---------------------------------------------------------- password reset
def test_forgot_password_emails_a_code_and_reset_works(
    client: TestClient, storage, settings, staff, ess_credentials, hr_user, kiosk_user
):
    employees_service.update_employee(
        storage, ess_credentials["employee"]["id"], {"email": "aarav@example.com"}
    )
    page = client.get("/forgot-password")
    assert page.status_code == 200

    response = client.post("/forgot-password", data={"username": ess_credentials["username"]})
    assert response.status_code == 200
    assert "reset" in response.text.lower()

    assert notify.dispatch(storage, settings)[0]["ok"] is True
    email = last_email(settings)
    assert "code" in email["subject"].lower()
    import re

    match = re.search(r">\s*(\d{6})\s*<", email["html"])
    assert match, "no 6-digit code found in the email"
    code = match.group(1)

    reset = client.post(
        "/reset-password",
        data={"username": ess_credentials["username"], "code": code,
              "password": "brand-new-pass", "confirm": "brand-new-pass"},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert auth.authenticate(storage, ess_credentials["username"], "brand-new-pass") is not None
    assert auth.authenticate(storage, ess_credentials["username"], ess_credentials["password"]) is None


def test_wrong_otp_is_rejected(storage, settings, staff, ess_credentials):
    employees_service.update_employee(
        storage, ess_credentials["employee"]["id"], {"email": "aarav@example.com"}
    )
    account_service.request_reset(storage, settings, ess_credentials["username"])
    ok, message = account_service.complete_reset(
        storage, settings, username=ess_credentials["username"], code="000000",
        new_password="whatever-1",
    )
    assert ok is False
    assert "incorrect" in message.lower()


def test_unknown_account_does_not_reveal_itself(client: TestClient, storage, settings):
    response = client.post("/forgot-password", data={"username": "does-not-exist"})
    assert response.status_code == 200
    # same generic wording as a real account
    assert "if that account exists" in response.text.lower()
    assert storage.query("SELECT * FROM otp_codes") == []


def test_otp_expires(storage, settings, staff, ess_credentials):
    employees_service.update_employee(
        storage, ess_credentials["employee"]["id"], {"email": "a@example.com"}
    )
    username, _ = account_service.request_reset(storage, settings, ess_credentials["username"])
    code = None
    # read the code from the queued email
    row = storage.query_one("SELECT body FROM notifications WHERE category='otp' ORDER BY id DESC LIMIT 1")
    assert row
    import re
    digits = re.findall(r">\s*(\d{6})\s*<", row["body"])
    code = digits[0]
    # expire it
    storage.execute(
        "UPDATE otp_codes SET expires_at = ? WHERE user_id = (SELECT id FROM users WHERE username = ?)",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), username),
    )
    ok, message = account_service.complete_reset(
        storage, settings, username=username, code=code, new_password="new-password-1"
    )
    assert ok is False
    assert "expired" in message.lower()


def test_otp_has_a_cooldown(storage, settings, staff, ess_credentials):
    """A second request within the cooldown must be refused politely, not crash."""
    employees_service.update_employee(
        storage, ess_credentials["employee"]["id"], {"email": "a@example.com"}
    )
    username, first = account_service.request_reset(storage, settings, ess_credentials["username"])
    assert username  # a code went out

    second_username, second_message = account_service.request_reset(
        storage, settings, ess_credentials["username"]
    )
    assert second_username is None
    assert "wait a minute" in second_message.lower()
    # still only one code was ever issued
    assert storage.scalar("SELECT COUNT(*) AS n FROM otp_codes") == 1


# -------------------------------------------------- profile change workflow
def test_employee_requests_a_change_and_hr_approves_it(
    ess_client: TestClient, storage, settings, staff, ess_credentials
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/profile")
    response = ess_client.post(
        "/me/profile",
        data={"_csrf": token, "phone": "9000000000", "bank_name": "HDFC Bank",
              "account_number": "123456789012", "ifsc": "HDFC0001234",
              "address": "", "email": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    pending = account_service.pending_changes(storage, employee_id=employee_id)
    assert {row["field"] for row in pending} == {"phone", "bank_name", "account_number", "ifsc"}
    # nothing has changed yet
    assert employees_service.get_employee(storage, employee_id)["phone"] is None

    # HR sees the queue and approves the bank name
    ess_client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    page = ess_client.get("/profile-requests")
    assert "Profile change requests" in page.text
    assert "HDFC Bank" in page.text

    row = next(r for r in pending if r["field"] == "bank_name")
    hr_token = csrf_of(ess_client, "/profile-requests")
    ess_client.post(f"/profile-requests/{row['id']}/approve", data={"_csrf": hr_token},
                    follow_redirects=False)
    assert employees_service.get_employee(storage, employee_id)["bank_name"] == "HDFC Bank"


def test_hr_rejection_leaves_the_employee_unchanged(
    ess_client: TestClient, storage, settings, staff, ess_credentials
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/profile")
    ess_client.post("/me/profile", data={"_csrf": token, "phone": "9111111111"},
                    follow_redirects=False)
    row = account_service.pending_changes(storage, employee_id=employee_id)[0]

    ess_client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    hr_token = csrf_of(ess_client, "/profile-requests")
    ess_client.post(f"/profile-requests/{row['id']}/reject",
                    data={"_csrf": hr_token, "note": "wrong number"}, follow_redirects=False)
    assert employees_service.get_employee(storage, employee_id)["phone"] is None
    assert storage.query_one("SELECT status FROM profile_change_requests WHERE id = ?", (row["id"],))["status"] == "rejected"


def test_unchanged_fields_do_not_create_requests(storage, settings, staff, ess_credentials):
    employee = employees_service.get_employee(storage, ess_credentials["employee"]["id"])
    created = account_service.submit_change_requests(
        storage, settings, employee=employee,
        values={"phone": employee.get("phone") or "", "bank_name": ""},
    )
    assert created == []


def test_employee_cannot_approve_their_own_request(
    ess_client: TestClient, storage, settings, staff, ess_credentials
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/profile")
    ess_client.post("/me/profile", data={"_csrf": token, "phone": "9222222222"}, follow_redirects=False)
    row = account_service.pending_changes(storage, employee_id=employee_id)[0]

    response = ess_client.post(f"/profile-requests/{row['id']}/approve", data={"_csrf": token},
                               follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/me"
    assert employees_service.get_employee(storage, employee_id)["phone"] is None


def test_employee_can_withdraw_their_request(
    ess_client: TestClient, storage, settings, staff, ess_credentials
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/profile")
    ess_client.post("/me/profile", data={"_csrf": token, "phone": "9333333333"}, follow_redirects=False)
    row = account_service.pending_changes(storage, employee_id=employee_id)[0]

    response = ess_client.post(f"/me/profile/request/{row['id']}/cancel", data={"_csrf": token},
                               follow_redirects=False)
    assert response.status_code == 303
    assert account_service.pending_changes(storage, employee_id=employee_id) == []


# --------------------------------------------------------------- surfaces
def test_new_pages_render(hr_client: TestClient, storage, settings, staff):
    for path in ("/announcements", "/notifications", "/profile-requests", "/forgot-password"):
        response = hr_client.get(path)
        assert response.status_code == 200, path


def test_login_page_links_to_forgot_password(client: TestClient):
    page = client.get("/login")
    assert "/forgot-password" in page.text


def test_notification_log_shows_messages(hr_client: TestClient, storage, settings):
    notify.queue_email(storage, "x@example.com", "welcome", settings=settings, name="X", username="x")
    page = hr_client.get("/notifications")
    assert "x@example.com" in page.text
    assert "pending" in page.text


def test_dispatch_button_from_the_ui(hr_client: TestClient, storage, settings):
    notify.queue_email(storage, "y@example.com", "welcome", settings=settings, name="Y", username="y")
    token = csrf_of(hr_client, "/notifications")
    response = hr_client.post("/notifications/dispatch", data={"_csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    assert notify.outbox_stats(storage)["sent"] == 1
