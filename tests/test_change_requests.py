"""Change requests from plan.md: A1–A3 (ESS), C1–C3 (onboarding) and follow-ups F1–F5."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Storage
from app.services import attendance as attendance_service
from app.services import orgtime, records
from tests.helpers import csrf_of

PDF = b"%PDF-1.4 test document\n%%EOF\n"


def _past_working_day(storage, settings, back=3):
    day = orgtime.today(settings) - timedelta(days=back)
    while orgtime.day_kind(storage, day) != "working":
        day -= timedelta(days=1)
    return day


# ----------------------------------------------------------------- A1
def test_a1_no_per_row_fix_but_one_request_button(ess_client: TestClient, storage: Storage, settings,
                                                   ess_credentials):
    page = ess_client.get("/me/attendance").text
    assert "Request correction" in page
    assert 'data-toggle-row="fix-' not in page and "> Fix<" not in page
    day = _past_working_day(storage, settings)
    token = csrf_of(ess_client, "/me/attendance")
    response = ess_client.post("/me/attendance/regularize", data={
        "_csrf": token, "work_date": day.isoformat(), "requested_status": "present",
        "check_in": "09:40", "check_out": "18:30", "reason": "Scanner did not detect me",
    }, follow_redirects=False)
    assert "ok=" in response.headers["location"]
    assert "correction pending" in ess_client.get(f"/me/attendance?period={day.isoformat()[:7]}").text


def test_a1_failed_request_reopens_the_form(ess_client: TestClient, settings):
    token = csrf_of(ess_client, "/me/attendance")
    future = (orgtime.today(settings) + timedelta(days=2)).isoformat()
    response = ess_client.post("/me/attendance/regularize", data={
        "_csrf": token, "work_date": future, "check_in": "09:30", "reason": "too early to ask",
    }, follow_redirects=False)
    assert "request=1" in response.headers["location"]


# ----------------------------------------------------------------- A2
def test_a2_source_labels():
    face = {"status": "present", "source": "face", "device_label": "Main Gate", "note": None}
    field = {"status": "present", "source": "field", "device_label": "Field · Client site", "note": None}
    corrected = {"status": "present", "source": "manual", "note": "Regularized: scanner missed me"}
    by_hr = {"status": "absent", "source": "manual", "note": None}
    leave = {"status": "leave", "source": "leave", "note": "leave #4 — wedding"}
    assert orgtime.source_label(face) == "Gate scanner · Main Gate"
    assert orgtime.source_label(field) == "Field (GPS) · Client site"
    assert orgtime.source_label(corrected) == "Corrected on request"
    assert orgtime.source_label(by_hr) == "Marked by HR"
    assert orgtime.source_label(leave) == "Leave"
    assert orgtime.user_note(leave) is None and orgtime.user_note(corrected) is None
    assert orgtime.user_note({"note": "client visit"}) == "client visit"


def test_a2_same_label_on_ess_and_hr_board(hr_client: TestClient, storage: Storage, settings, staff):
    day = _past_working_day(storage, settings)
    attendance_service.mark_manual(storage, employee_id=staff[0]["id"], work_date=day.isoformat(),
                                   status="present", check_in="09:30", check_out="18:30")
    assert "Marked by HR" in hr_client.get(f"/attendance?day={day.isoformat()}").text
    register = attendance_service.monthly_register(storage, settings, day.isoformat()[:7])
    row = next(r for r in register["rows"] if r["employee"]["id"] == staff[0]["id"])
    assert "Marked by HR" in row["cells"][day.day - 1]["title"]
    from app import auth
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    hr_client.cookies.clear()
    hr_client.post("/login", data={"username": user["username"], "password": "portal-pass-1"})
    assert "Marked by HR" in hr_client.get(f"/me/attendance?period={day.isoformat()[:7]}").text


# ----------------------------------------------------------------- A3
def test_a3_documents_live_on_the_profile(ess_client: TestClient, storage: Storage, settings, ess_credentials):
    records.add_document(storage, settings, employee_id=ess_credentials["employee"]["id"], data=PDF,
                         original_name="pan-card.pdf", category="pan")
    redirect = ess_client.get("/me/documents", follow_redirects=False)
    assert redirect.headers["location"] == "/me/profile?tab=documents"
    page = ess_client.get("/me/profile?tab=documents").text
    assert "pan-card.pdf" in page and 'action="/me/documents"' in page
    assert "pan-card.pdf" not in ess_client.get("/me/profile").text  # Details tab
    assert 'href="/me/documents"' not in page  # gone from the sidebar
    token = csrf_of(ess_client, "/me/profile")
    response = ess_client.post("/me/documents", data={"_csrf": token, "category": "resume"},
                               files={"file": ("cv.pdf", PDF, "application/pdf")}, follow_redirects=False)
    assert response.headers["location"].startswith("/me/profile?") and "tab=documents" in response.headers["location"]


# ----------------------------------------------------------------- C1
import pytest  # noqa: E402

from app import auth  # noqa: E402
from app.services import employees as employees_service  # noqa: E402

VALID_AADHAAR = "234567890124"  # passes the Verhoeff check


def test_c1_identity_validation_and_masked_aadhaar(storage: Storage):
    employee_id = employees_service.create_employee(storage, {
        "name": "KYC Person", "pan": "abcde1234f", "ifsc": "hdfc0001234", "account_number": "1234 5678 9012",
        "aadhaar": VALID_AADHAAR, "address": "12 MG Road", "bank_name": "HDFC",
    })
    employee = employees_service.get_employee(storage, employee_id)
    assert employee["pan"] == "ABCDE1234F" and employee["ifsc"] == "HDFC0001234"
    assert employee["account_number"] == "123456789012" and employee["aadhaar_last4"] == "0124"
    assert VALID_AADHAAR not in str(storage.query("SELECT * FROM employees"))  # full number never stored
    for bad in ({"pan": "ABC123"}, {"ifsc": "HDFC1234567"}, {"account_number": "12AB"},
                {"aadhaar": "234567890123"}):
        with pytest.raises(employees_service.ValidationError):
            employees_service.create_employee(storage, {"name": "Bad", **bad})


# ------------------------------------------------------------ C2 + C3
def _onboard(hr_client, **extra):
    token = csrf_of(hr_client, "/employees/new")
    data = {"_csrf": token, "name": "New Joiner", "code": "NJ001", "joined_on": "2026-10-01",
            "monthly_gross": "30000", "create_login": "1", "send_welcome": "1", **extra}
    files = extra.pop("files", None) if "files" in extra else None
    return hr_client.post("/employees", data={k: v for k, v in data.items() if k != "files"},
                          files=files, follow_redirects=False)


def test_c2_c3_onboarding_with_documents_and_email_link(hr_client: TestClient, storage: Storage):
    response = _onboard(hr_client, email="joiner@example.com", pan="ABCDE1234F", files={
        "doc_pan": ("pan.pdf", PDF, "application/pdf"),
        "doc_resume": ("cv.pdf", PDF, "application/pdf"),
        "doc_aadhaar": ("bad.exe", b"MZ not a document", "application/octet-stream"),
    })
    assert response.status_code == 303
    location = response.headers["location"]
    assert "2+document" in location and "bad.exe" in location  # saved anyway, failure reported
    employee = employees_service.get_employee_by_code(storage, "NJ001")
    assert {d["category"] for d in records.list_documents(storage, employee_id=employee["id"])} == {"pan", "resume"}
    user = auth.get_user_for_employee(storage, employee["id"])
    assert user["username"] == "nj001"
    mail = storage.query_one("SELECT * FROM notifications WHERE category = 'welcome'")
    assert mail["recipient"] == "joiner@example.com" and "/set-password?token=" in mail["body"]
    assert "Password</td>" not in mail["body"]  # no password in the email


def test_c2_skip_documents(hr_client: TestClient, storage: Storage):
    _onboard(hr_client, skip_documents="1", files={"doc_pan": ("pan.pdf", PDF, "application/pdf")})
    employee = employees_service.get_employee_by_code(storage, "NJ001")
    assert records.list_documents(storage, employee_id=employee["id"]) == []


def test_c3_without_email_hr_sees_credentials_once(hr_client: TestClient, storage: Storage):
    response = _onboard(hr_client)
    assert response.status_code == 200 and "shown once" in response.text
    assert "nj001" in response.text
    assert "password" not in str(response.headers.get("location", ""))


def test_c3_required_fields(hr_client: TestClient):
    token = csrf_of(hr_client, "/employees/new")
    response = hr_client.post("/employees", data={"_csrf": token, "name": "No Salary", "code": "NS1",
                                                 "joined_on": "2026-10-01"})
    assert response.status_code == 422 and "monthly gross" in response.text


def test_c3_set_password_link_is_single_use(client: TestClient, storage: Storage, staff, hr_user):
    user, _ = auth.ensure_employee_login(storage, staff[0])
    token = auth.issue_password_link(storage, user["id"])
    assert client.get(f"/set-password?token={token}").status_code == 200
    client.post("/set-password", data={"token": token, "password": "my-own-pass-1", "confirm": "my-own-pass-1"})
    assert auth.authenticate(storage, user["username"], "my-own-pass-1")
    again = client.post("/set-password", data={"token": token, "password": "other-pass-22", "confirm": "other-pass-22"})
    assert "expired or was already used" in again.text


def test_c3_login_with_email_and_change_password(client: TestClient, storage: Storage, staff, hr_user):
    employees_service.update_employee(storage, staff[0]["id"], {"email": "aarav@example.com"})
    user, _ = auth.ensure_employee_login(storage, staff[0], password="first-pass-1")
    response = client.post("/login", data={"username": "aarav@example.com", "password": "first-pass-1"},
                           follow_redirects=False)
    assert response.status_code == 303
    token = csrf_of(client, "/me/password")
    wrong = client.post("/me/password", data={"_csrf": token, "current": "nope", "password": "second-pass-2",
                                              "confirm": "second-pass-2"}, follow_redirects=False)
    assert "err=" in wrong.headers["location"]
    done = client.post("/me/password", data={"_csrf": token, "current": "first-pass-1", "password": "second-pass-2",
                                             "confirm": "second-pass-2"}, follow_redirects=False)
    assert "ok=" in done.headers["location"]
    assert client.get("/me", follow_redirects=False).status_code == 200  # still signed in here
    assert auth.authenticate(storage, user["username"], "second-pass-2")


# ------------------------------------------------------------------ F2
def test_f2_edit_form_status_change_handles_the_portal_login(hr_client: TestClient, storage: Storage, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    base = {"name": staff[0]["name"], "code": staff[0]["code"], "joined_on": staff[0]["joined_on"]}
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}/edit")
    hr_client.post(f"/employees/{staff[0]['id']}", data={"_csrf": token, **base, "status": "inactive"})
    assert auth.get_user(storage, user["id"])["active"] == 0
    hr_client.post(f"/employees/{staff[0]['id']}", data={"_csrf": token, **base, "status": "active",
                                                         "enable_portal": "1"})
    assert auth.get_user(storage, user["id"])["active"] == 1


# ------------------------------------------------------------- F1, F3, F5
from app import notify  # noqa: E402
from app.services import exits as exits_service  # noqa: E402
from app.services import leave as leave_service  # noqa: E402


def test_f1_fnf_email_carries_the_pdf(storage: Storage, settings, staff):
    employees_service.update_employee(storage, staff[0]["id"], {"email": "leaver@example.com"})
    today = orgtime.today(settings).isoformat()
    row = exits_service.record_exit(storage, settings, employee_id=staff[0]["id"], exit_type="resignation",
                                    resignation_date=today, last_working_day=today, notice_days=0)
    for item in exits_service.checklist(storage, row["id"]):
        exits_service.toggle_item(storage, item["id"], done=True)
    exits_service.settle(storage, settings, row["id"])
    mail = storage.query_one("SELECT * FROM notifications WHERE category = 'fnf'")
    attachments = notify._attachments_for(storage, settings, mail)
    assert attachments and attachments[0][0].endswith(".pdf") and attachments[0][1].startswith(b"%PDF")


def test_f3_overwriting_a_leave_day_needs_confirmation(hr_client: TestClient, storage: Storage, settings, staff):
    day = _past_working_day(storage, settings, back=20)
    leave_service.apply_leave(storage, settings, employee_id=staff[0]["id"], leave_type="paid",
                              from_date=day.isoformat(), allow_negative=True)
    token = csrf_of(hr_client, "/attendance")
    form = {"_csrf": token, "employee_id": staff[0]["id"], "work_date": day.isoformat(), "status": "present",
            "check_in": "09:30", "check_out": "18:30"}
    refused = hr_client.post("/attendance/mark", data=form, follow_redirects=False)
    assert "err=" in refused.headers["location"]
    assert attendance_service.get_row(storage, staff[0]["id"], day)["status"] == "leave"
    hr_client.post("/attendance/mark", data={**form, "release_leave": "1"})
    assert attendance_service.get_row(storage, staff[0]["id"], day)["status"] == "present"


def test_f5_four_attempts_with_30_minute_last_wait(storage: Storage, settings, monkeypatch):
    assert notify.RETRY_BACKOFF_MINUTES == (1, 5, 30)
