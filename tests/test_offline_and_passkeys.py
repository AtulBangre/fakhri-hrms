"""Offline gate scans and sync, passkey routes, the loss-of-pay ledger and login links."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.face import FaceServiceUnavailable, IdentificationResult
from app.services import alerts, attendance as attendance_service, orgtime
from tests.conftest import KIOSK_PASSWORD, KIOSK_USERNAME
from tests.helpers import csrf_of

SCAN_ID = "a1b2c3d4e5f60718"


def _match(fake_face, code: str) -> None:
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id=code, similarity=0.9, face_id="face_1", threshold=0.363,
        faces_in_probe=1, gallery_size=3,
    )


def _sync(client: TestClient, token: str, *, when: datetime, scan_id: str = SCAN_ID):
    return client.post(
        "/api/kiosk/offline-sync",
        data={"scan_id": scan_id, "captured_at": str(int(when.timestamp() * 1000))},
        files={"image": ("scan.jpg", b"jpeg", "image/jpeg")},
        headers={"X-CSRF-Token": token},
    )


@pytest.fixture
def gate(client: TestClient, hr_user, kiosk_user):
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    return client, csrf_of(client, "/kiosk")


def _earlier(settings, hours: float = 1) -> datetime:
    """A moment earlier today (or just after midnight), so tests hold at any hour."""
    now = orgtime.local_now(settings)
    moment = now - timedelta(hours=hours)
    return moment if moment.date() == now.date() else now.replace(hour=0, minute=1)


# ---------------------------------------------------------------- applying a scan
def test_offline_scan_marks_at_the_time_it_was_taken(gate, storage, settings, staff, fake_face):
    client, token = gate
    _match(fake_face, staff[0]["code"])
    when = _earlier(settings)
    body = _sync(client, token, when=when).json()
    assert body == {"status": "done", "action": "check_in"}
    row = attendance_service.get_row(storage, staff[0]["id"], when.date())
    assert row["check_in"] == orgtime.hhmmss(when) and row["source"] == "face_offline"
    assert "offline" in orgtime.source_label(row)


def test_resending_a_scan_does_not_mark_twice(gate, storage, settings, staff, fake_face):
    client, token = gate
    _match(fake_face, staff[0]["code"])
    when = _earlier(settings, 3)
    assert _sync(client, token, when=when).json()["action"] == "check_in"
    assert _sync(client, token, when=when).json() == {"status": "done", "action": "already_synced"}
    assert attendance_service.get_row(storage, staff[0]["id"], when.date())["check_out"] is None


def test_scans_apply_as_check_in_then_check_out_in_order(storage, settings, staff):
    day = datetime(2026, 6, 22, 9, 30, tzinfo=settings.tz)
    first = attendance_service.mark_offline_scan(storage, settings, staff[0], moment=day)
    second = attendance_service.mark_offline_scan(storage, settings, staff[0], moment=day.replace(hour=18))
    assert (first.action, second.action, second.worked_minutes) == ("check_in", "check_out", 540)


def test_a_scan_older_than_the_recorded_check_in_is_left_for_hr(storage, settings, staff):
    day = datetime(2026, 6, 22, 10, 0, tzinfo=settings.tz)
    attendance_service.mark_by_face(storage, settings, staff[0], moment=day)
    outcome = attendance_service.mark_offline_scan(storage, settings, staff[0], moment=day.replace(hour=9))
    assert outcome.ok is False and outcome.action == "out_of_order"
    assert attendance_service.get_row(storage, staff[0]["id"], day.date())["check_in"] == "10:00:00"


def test_the_check_out_wait_applies_offline_too(storage, settings, staff):
    day = datetime(2026, 6, 22, 9, 30, tzinfo=settings.tz)
    attendance_service.mark_offline_scan(storage, settings, staff[0], moment=day)
    assert attendance_service.mark_offline_scan(storage, settings, staff[0], moment=day.replace(hour=10)).action == "too_early"


# ------------------------------------------------------------------- bad scans
@pytest.mark.parametrize("offset,label", [(timedelta(hours=-80), "old"), (timedelta(hours=2), "future")])
def test_times_outside_the_window_are_refused_and_hr_is_told(gate, storage, settings, staff, fake_face, offset, label):
    client, token = gate
    _match(fake_face, staff[0]["code"])
    body = _sync(client, token, when=orgtime.local_now(settings) + offset).json()
    assert body == {"status": "done", "action": "too_old"}
    assert storage.scalar("SELECT COUNT(*) FROM attendance") == 0
    assert storage.scalar("SELECT COUNT(*) FROM notifications WHERE category = 'offline_scan'") >= 1


def test_an_unmatched_face_is_dropped_and_hr_is_told(gate, storage, settings, fake_face):
    client, token = gate
    fake_face.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=0.1, face_id=None, threshold=0.363, faces_in_probe=1, gallery_size=3,
    )
    body = _sync(client, token, when=_earlier(settings)).json()
    assert body["status"] == "done" and body["action"] == "unknown_face"
    assert storage.scalar("SELECT COUNT(*) FROM attendance") == 0
    assert storage.scalar("SELECT COUNT(*) FROM notifications WHERE category = 'offline_scan'") >= 1


def test_face_service_outage_keeps_the_scan_for_later(gate, settings, fake_face, monkeypatch):
    client, token = gate

    def down(*_args, **_kwargs):
        raise FaceServiceUnavailable("no route")

    monkeypatch.setattr(fake_face, "identify", down)
    response = _sync(client, token, when=_earlier(settings))
    assert response.status_code == 503 and response.json()["status"] == "later"


def test_sync_needs_the_csrf_token_and_a_gate_or_hr_login(client: TestClient, ess_client: TestClient, settings):
    now = orgtime.local_now(settings)
    assert _sync(ess_client, "whatever", when=now).status_code in (303, 401, 403)


def test_sync_refuses_a_missing_csrf_token(gate, settings):
    client, _token = gate
    assert _sync(client, "wrong", when=orgtime.local_now(settings)).status_code == 403


def test_a_bad_scan_id_is_refused(gate, settings):
    client, token = gate
    assert _sync(client, token, when=orgtime.local_now(settings), scan_id="../etc").status_code == 400


def test_offline_can_be_switched_off(gate, settings):
    client, token = gate
    settings.kiosk_offline = False
    assert _sync(client, token, when=_earlier(settings)).status_code == 503
    assert "offlineEnabled: false" in client.get("/kiosk").text


def test_status_gives_the_gate_its_clock_and_token(gate):
    client, token = gate
    body = client.get("/api/kiosk/status").json()
    assert abs(body["server_epoch_ms"] / 1000 - time.time()) < 5 and body["csrf"] == token


def test_old_scan_ids_are_cleared_but_recent_ones_stay(storage, settings):
    today = orgtime.today(settings)
    old = f"offline_scan:{(today - timedelta(days=30)).isoformat()}:aaa"
    recent = f"offline_scan:{(today - timedelta(days=5)).isoformat()}:bbb"
    storage.set_setting(old, "1")
    storage.set_setting(recent, "1")
    alerts._housekeeping(storage, today)
    assert storage.get_setting(old, "") == "" and storage.get_setting(recent, "") == "1"


# -------------------------------------------------------------------- passkeys
def test_passkeys_are_hidden_on_an_address_they_cannot_use(client: TestClient):
    assert client.get("/api/passkey/available").json() == {"available": False}
    assert client.post("/api/passkey/login/options").status_code == 404


def test_passkey_login_refuses_a_forged_challenge(client: TestClient, settings):
    settings.public_base_url = "http://testserver"
    # http is only allowed on localhost, so this address stays refused too
    assert client.post("/api/passkey/login/verify", json={"credential": {}}).status_code == 404


def test_passkey_pages_need_a_login_and_csrf(ess_client: TestClient):
    assert ess_client.get("/account/passkeys").status_code == 200
    assert ess_client.post("/api/passkey/register/options").status_code == 403


def test_passkey_pages_are_closed_to_the_kiosk(gate):
    client, token = gate
    assert client.post("/api/passkey/register/options", headers={"X-CSRF-Token": token}).status_code == 403


# ------------------------------------------------- employee face, approved by HR
def _send_face(client: TestClient, photo: bytes):
    return client.post("/me/face", data={"_csrf": csrf_of(client, "/me/face")},
                       files={"image": ("face.jpg", photo, "image/jpeg")}, follow_redirects=False)


def _face_request(storage):
    return storage.query_one("SELECT * FROM profile_change_requests WHERE field = 'face_photo' ORDER BY id DESC")


def _photo_path(settings, stored_name):
    return settings.data_dir / "uploads" / "face_requests" / stored_name


@pytest.fixture
def face_clear(fake_face):
    """The photo check finds one clear face and nobody it matches."""
    fake_face.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=None, face_id=None, threshold=0.363, faces_in_probe=1, gallery_size=0,
    )
    return fake_face


JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"


def test_employee_home_offers_face_registration(ess_client: TestClient):
    assert "Register your face" in ess_client.get("/me").text
    assert "Register face" in ess_client.get("/me/face").text


def test_a_submitted_face_is_not_enrolled_until_hr_approves(ess_client, storage, settings, staff, face_clear):
    assert _send_face(ess_client, JPEG).status_code == 303
    row = _face_request(storage)
    assert row["status"] == "pending" and row["employee_id"] == staff[0]["id"]
    assert _photo_path(settings, row["new_value"]).exists()
    assert not storage.query_one("SELECT face_id FROM employees WHERE id = ?", (staff[0]["id"],))["face_id"]
    assert face_clear.enrolled == {}
    assert "Face photo sent" in ess_client.get("/me").text


def test_hr_approval_enrols_the_face_and_deletes_the_photo(hr_client, storage, settings, staff, face_clear):
    from app.services import account

    account.submit_face_request(storage, settings, employee=staff[0], photo=JPEG)
    row = _face_request(storage)
    stored = row["new_value"]
    page = hr_client.get("/profile-requests").text
    assert "Face registration" in page and stored not in page
    assert hr_client.get(f"/profile-requests/{row['id']}/photo").content == JPEG

    response = hr_client.post(f"/profile-requests/{row['id']}/approve",
                              data={"_csrf": csrf_of(hr_client, "/profile-requests")}, follow_redirects=False)
    assert "ok=" in response.headers["location"]
    assert storage.query_one("SELECT face_id FROM employees WHERE id = ?", (staff[0]["id"],))["face_id"]
    assert _face_request(storage)["status"] == "approved"
    assert not _photo_path(settings, stored).exists() and stored not in _face_request(storage)["new_value"]
    assert hr_client.get(f"/profile-requests/{row['id']}/photo", follow_redirects=False).status_code == 303


def test_rejecting_keeps_the_gallery_clean_and_tells_the_employee(ess_client, storage, settings, staff, face_clear):
    from app.services import account

    _send_face(ess_client, JPEG)
    row = _face_request(storage)
    account.decide(storage, settings, request_id=row["id"], approve=False, decided_by="hr", note="Photo is blurry",
                   face=face_clear)
    assert face_clear.enrolled == {} and not _photo_path(settings, row["new_value"]).exists()
    assert "Photo is blurry" in ess_client.get("/me/face").text
    pushed = storage.query_one("SELECT body FROM notifications WHERE channel = 'push' AND body LIKE '%blurry%'")
    assert pushed is not None


def test_a_failed_enrol_leaves_the_request_pending_for_hr(hr_client, storage, settings, staff, face_clear, monkeypatch):
    from app.services import account, employees as employees_service

    account.submit_face_request(storage, settings, employee=staff[0], photo=JPEG)
    row = _face_request(storage)

    def clash(*_args, **_kwargs):
        raise employees_service.ValidationError("This face already belongs to someone else.")

    monkeypatch.setattr(employees_service, "enrol_face", clash)
    response = hr_client.post(f"/profile-requests/{row['id']}/approve",
                              data={"_csrf": csrf_of(hr_client, "/profile-requests")}, follow_redirects=False)
    assert "err=" in response.headers["location"]
    assert _face_request(storage)["status"] == "pending" and _photo_path(settings, row["new_value"]).exists()


def test_a_new_photo_replaces_the_old_one(ess_client, storage, settings, face_clear):
    _send_face(ess_client, JPEG)
    first = _face_request(storage)["new_value"]
    _send_face(ess_client, JPEG + b"2")
    assert not _photo_path(settings, first).exists()
    assert storage.scalar("SELECT COUNT(*) FROM profile_change_requests WHERE field = 'face_photo'") == 1


def test_withdrawing_deletes_the_photo(ess_client, storage, settings, face_clear):
    _send_face(ess_client, JPEG)
    row = _face_request(storage)
    ess_client.post(f"/me/profile/request/{row['id']}/cancel", data={"_csrf": csrf_of(ess_client, "/me/profile")})
    assert _face_request(storage) is None and not _photo_path(settings, row["new_value"]).exists()


def test_a_photo_with_no_clear_face_is_refused_at_once(ess_client, storage, fake_face):
    fake_face.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=None, face_id=None, threshold=0.363, faces_in_probe=0, gallery_size=0,
    )
    assert "err=" in _send_face(ess_client, JPEG).headers["location"]
    assert _face_request(storage) is None


def test_a_file_that_is_not_a_photo_is_refused(ess_client, storage, face_clear):
    assert "err=" in _send_face(ess_client, b"%PDF-1.7 not a photo").headers["location"]
    assert _face_request(storage) is None


def test_employee_cannot_see_or_approve_requests(ess_client, storage, settings, staff, face_clear):
    _send_face(ess_client, JPEG)
    row = _face_request(storage)
    assert ess_client.get(f"/profile-requests/{row['id']}/photo", follow_redirects=False).status_code in (303, 403)
    token = csrf_of(ess_client, "/me/face")
    assert ess_client.post(f"/profile-requests/{row['id']}/approve", data={"_csrf": token},
                           follow_redirects=False).status_code in (303, 403)
    assert _face_request(storage)["status"] == "pending"


# ---------------------------------------------------------------------- the rest
def test_lop_ledger_explains_each_deduction(hr_client: TestClient, storage, settings, staff):
    attendance_service.mark_manual(storage, employee_id=staff[0]["id"], work_date="2026-03-03", status="absent")
    ledger = attendance_service.lop_ledger(storage, settings, "2026-03")
    row = next(r for r in ledger["rows"] if r["employee"]["id"] == staff[0]["id"])
    assert 3 in row["absent"] and row["lop_days"] >= 1 and row["salary_cut"] is None
    page = hr_client.get("/attendance/lop?period=2026-03")
    assert page.status_code == 200 and staff[0]["name"] in page.text


def test_employee_cannot_open_the_lop_ledger(ess_client: TestClient):
    assert ess_client.get("/attendance/lop", follow_redirects=False).status_code in (303, 403)


def test_register_offers_a_shift_filter(hr_client: TestClient, staff):
    page = hr_client.get("/attendance/register").text
    assert 'id="shiftQuickFilter"' in page and "Company shift" in page


def test_portal_email_has_a_link_and_never_the_password(hr_client: TestClient, storage, staff):
    storage.execute("UPDATE employees SET email = 'x@example.com', phone = '+919000000000' WHERE id = ?", (staff[2]["id"],))
    page = f"/employees/{staff[2]['id']}"
    response = hr_client.post(f"{page}/portal", data={"_csrf": csrf_of(hr_client, page), "password": "secret-pass-77"})
    assert "secret-pass-77" in response.text  # HR still sees it once
    rows = storage.query("SELECT channel, body FROM notifications WHERE category = 'welcome'")
    assert {row["channel"] for row in rows} == {"email", "whatsapp"}
    for row in rows:
        assert "secret-pass-77" not in row["body"] and "/set-password?token=" in row["body"]


def test_exit_decisions_reach_the_employee(hr_client: TestClient, storage, settings, staff, ess_credentials):
    from app.services import exits

    exit_id = exits.resign(storage, settings, employee_id=staff[0]["id"], reason="Moving city")["id"]
    page = f"/exits/{exit_id}"
    response = hr_client.post(f"{page}/approve", data={
        "_csrf": csrf_of(hr_client, page), "last_working_day": "2026-12-31", "notice_days": "30",
    }, follow_redirects=False)
    assert response.status_code == 303 and "err=" not in response.headers["location"]
    rows = storage.query("SELECT subject, recipient FROM notifications WHERE category = 'exit'")
    assert [r["subject"] for r in rows] == ["Your resignation was approved"]
    assert rows[0]["recipient"] == f"user:{ess_credentials['user']['id']}"


def test_rejecting_a_resignation_reaches_the_employee(hr_client: TestClient, storage, settings, staff, ess_credentials):
    from app.services import exits

    exit_id = exits.resign(storage, settings, employee_id=staff[0]["id"], reason="Moving city")["id"]
    page = f"/exits/{exit_id}"
    hr_client.post(f"{page}/reject", data={"_csrf": csrf_of(hr_client, page)}, follow_redirects=False)
    assert storage.scalar("SELECT COUNT(*) FROM notifications WHERE category = 'exit'") == 1


def test_appraisal_launch_tells_people_and_a_finished_review_shows_no_rating(
    hr_client: TestClient, storage, staff, ess_credentials
):
    from app.services import appraisal

    cycle = appraisal.create_cycle(storage, name="H2", period_start="2026-07-01", period_end="2026-12-31",
                                  goals_template="Quality of work | 100")
    cycle_id = cycle["id"] if isinstance(cycle, dict) else cycle
    back = f"/appraisals/cycles/{cycle_id}"
    response = hr_client.post(f"{back}/launch", data={"_csrf": csrf_of(hr_client, back)}, follow_redirects=False)
    assert response.status_code == 303 and "err=" not in response.headers["location"]
    assert storage.scalar("SELECT COUNT(*) FROM notifications WHERE category = 'appraisal'") >= 1

    row = next(r for r in appraisal.cycle_appraisals(storage, cycle_id) if r["employee_id"] == staff[0]["id"])
    page = f"/appraisals/{row['id']}"
    response = hr_client.post(f"{page}/review", data={"_csrf": csrf_of(hr_client, page), "complete": "1",
                                                      "final_rating": "4.5", "comment": "Good year"},
                              follow_redirects=False)
    assert response.status_code == 303
    done = storage.query("SELECT subject, body FROM notifications WHERE category = 'appraisal' ORDER BY id DESC LIMIT 1")
    assert "4.5" not in done[0]["body"]


def test_login_link_can_be_used(client: TestClient, storage, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="old-password-1")
    token = auth.issue_password_link(storage, user["id"])
    assert client.get(f"/set-password?token={token}").status_code == 200
