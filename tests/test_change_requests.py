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
