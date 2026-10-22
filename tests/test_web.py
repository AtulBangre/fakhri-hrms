"""Full-stack web tests: sessions, every page, kiosk scanning and CSV exports.

The Face API is replaced by an in-memory fake (``tests.conftest.FakeFaceService``)
so the suite runs without models, camera or network.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import Settings
from app.face import IdentificationResult
from app.main import create_app
from app.services import employees as employees_service
from tests.conftest import HR_PASSWORD, HR_USERNAME, KIOSK_PASSWORD, KIOSK_USERNAME, FakeFaceService

CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def app(settings: Settings):
    application = create_app(settings)
    with TestClient(application) as client:
        application.state.client = client
        yield application


@pytest.fixture
def client(app):
    return app.state.client


@pytest.fixture
def fake_face(app) -> FakeFaceService:
    """Swap the HTTP client for an in-memory fake after startup."""
    fake = FakeFaceService()
    app.state.face = fake
    return fake


@pytest.fixture
def hr_client(client, storage, hr_user, kiosk_user):
    """A signed-in HR session."""
    response = client.post(
        "/login",
        data={"username": HR_USERNAME, "password": HR_PASSWORD, "next": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


def csrf_of(client: TestClient, path: str = "/dashboard") -> str:
    page = client.get(path)
    match = CSRF_RE.search(page.text)
    assert match, f"no CSRF token found in {path}"
    return match.group(1)


# ----------------------------------------------------------------- auth flows
def test_login_page_renders_branded_shell(client: TestClient):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Fakhri IT Services" in response.text
    assert "#870808" in response.text  # brand primary in the theme colour
    assert "Gate / kiosk login" in response.text


def test_root_redirects_to_login_when_signed_out(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(client: TestClient, hr_user):
    response = client.post("/login", data={"username": HR_USERNAME, "password": "nope"})
    assert response.status_code == 401
    assert "Incorrect username or password" in response.text


def test_hr_login_lands_on_dashboard_and_sets_cookie(client: TestClient, hr_user, kiosk_user):
    response = client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD})
    assert response.status_code == 200
    assert "Today's scans" in response.text or "Present today" in response.text
    assert any(cookie.name == "hrms_session" for cookie in client.cookies.jar)


def test_kiosk_login_lands_on_the_scanner(client: TestClient, hr_user, kiosk_user):
    response = client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    assert response.status_code == 200
    assert "Tap to start" in response.text
    assert "attendance scanner" in response.text.lower()


def test_logout_clears_the_session(hr_client: TestClient):
    token = csrf_of(hr_client)
    response = hr_client.post("/logout", data={"_csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert hr_client.get("/dashboard", follow_redirects=False).status_code == 303


# --------------------------------------------------------------- role gating
def test_kiosk_user_cannot_reach_hr_pages(client: TestClient, hr_user, kiosk_user):
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    for path in ("/dashboard", "/employees", "/payroll", "/leave", "/settings", "/audit"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert "/kiosk" in response.headers["location"], path

    # …but the scanner itself works
    assert client.get("/kiosk").status_code == 200


def test_anonymous_api_call_is_rejected(client: TestClient, assets):
    response = client.post("/api/kiosk/scan", files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")})
    assert response.status_code == 401


def test_csrf_is_required_for_posts(hr_client: TestClient):
    response = hr_client.post(
        "/employees",
        data={"name": "No Token", "code": "NOCSRF"},
        follow_redirects=False,
    )
    assert response.status_code == 403


# ------------------------------------------------------------------- pages
def test_every_hr_page_renders(hr_client: TestClient, staff):
    for path in (
        "/dashboard",
        "/employees",
        "/employees/new",
        f"/employees/{staff[0]['id']}",
        f"/employees/{staff[0]['id']}/edit",
        f"/employees/{staff[0]['id']}/payslips",
        "/attendance",
        "/attendance/register",
        "/leave",
        "/payroll",
        "/settings",
        "/audit",
        "/kiosk",
        "/offline",
    ):
        response = hr_client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        assert "Fakhri" in response.text


def test_dashboard_shows_today_counts(hr_client: TestClient, staff):
    page = hr_client.get("/dashboard").text
    assert "Present today" in page
    assert "Headcount" in page
    assert "Face enrolment pending" in page


def test_404_page_is_branded(hr_client: TestClient):
    response = hr_client.get("/definitely-not-a-page")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_manifests_and_service_worker(client: TestClient):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["theme_color"] == "#870808"
    assert body["start_url"] == "/dashboard"

    kiosk_manifest = client.get("/kiosk.webmanifest").json()
    assert kiosk_manifest["start_url"] == "/kiosk"

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "fakhri-hrms" in worker.text


def test_icons_and_static_assets(client: TestClient):
    for path in (
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/js/kiosk.js",
        "/static/img/icon-192.png",
        "/static/img/icon-512.png",
        "/static/img/maskable-512.png",
    ):
        assert client.get(path).status_code == 200, path


def test_health_endpoints(client: TestClient, fake_face):
    assert client.get("/healthz").json()["status"] == "ok"
    body = client.get("/api/health").json()
    assert body["database"] == "ok"
    assert body["face_service"]["ok"] is True
    assert body["org"] == "Fakhri IT Services"


# ------------------------------------------------------- employee management
def test_create_employee_through_the_form(hr_client: TestClient, storage):
    token = csrf_of(hr_client, "/employees/new")
    response = hr_client.post(
        "/employees",
        data={
            "_csrf": token,
            "name": "Test Person",
            "code": "TP001",
            "department": "Engineering",
            "designation": "QA Engineer",
            "joined_on": "2026-01-05",
            "employment_type": "full_time",
            "monthly_gross": "40000",
            "pf": "1800",
            "professional_tax": "200",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/employees/" in response.headers["location"]

    employee = employees_service.get_employee_by_code(storage, "TP001")
    assert employee is not None
    assert employee["monthly_gross"] == 40000
    history = employees_service.salary_history(storage, employee["id"])
    assert len(history) == 1 and history[0]["pf"] == 1800


def test_create_employee_validation_error_is_shown(hr_client: TestClient, staff):
    token = csrf_of(hr_client, "/employees/new")
    response = hr_client.post(
        "/employees",
        data={"_csrf": token, "name": "Duplicate Code", "code": staff[0]["code"]},
    )
    assert response.status_code == 422
    assert "already used" in response.text


def test_face_enrolment_stores_a_template(hr_client: TestClient, storage, staff, fake_face, assets):
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}")
    response = hr_client.post(
        f"/employees/{staff[0]['id']}/face",
        data={"_csrf": token},
        files={"image": ("face.jpg", assets["person_a_1"], "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    employee = employees_service.get_employee(storage, staff[0]["id"])
    assert employee["face_id"] == fake_face.enrolled[staff[0]["code"]]


def test_face_enrolment_handles_empty_upload(hr_client: TestClient, staff, fake_face):
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}")
    response = hr_client.post(
        f"/employees/{staff[0]['id']}/face",
        data={"_csrf": token},
        files={"image": ("face.jpg", b"", "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


def test_face_enrolment_reports_service_outage(hr_client: TestClient, staff):
    from app.face import FaceServiceUnavailable

    class BrokenFace(FakeFaceService):
        def enrol_face(self, *args, **kwargs):
            raise FaceServiceUnavailable("Cannot reach the face service.")

    hr_client.app.state.face = BrokenFace()
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}")
    response = hr_client.post(
        f"/employees/{staff[0]['id']}/face",
        data={"_csrf": token},
        files={"image": ("face.jpg", b"bytes", "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "offline" in response.headers["location"].lower()


def test_salary_update_is_recorded_in_history(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}")
    response = hr_client.post(
        f"/employees/{staff[0]['id']}/salary",
        data={"_csrf": token, "monthly_gross": "70000", "effective_from": "2026-04-01", "note": "raise"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    history = employees_service.salary_history(storage, staff[0]["id"])
    assert history[0]["monthly_gross"] == 70000
    assert employees_service.get_employee(storage, staff[0]["id"])["monthly_gross"] == 70000


def test_employee_delete_removes_the_record(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, f"/employees/{staff[2]['id']}")
    response = hr_client.post(
        f"/employees/{staff[2]['id']}/delete", data={"_csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert employees_service.get_employee(storage, staff[2]["id"]) is None


# ---------------------------------------------------------------- attendance
def test_manual_attendance_from_the_board(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, "/attendance")
    response = hr_client.post(
        "/attendance/mark",
        data={
            "_csrf": token,
            "employee_id": staff[0]["id"],
            "work_date": "2026-01-05",
            "status": "present",
            "check_in": "09:40",
            "check_out": "18:20",
            "return_day": "2026-01-05",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    from app.services import attendance as attendance_service

    row = attendance_service.get_row(storage, staff[0]["id"], date(2026, 1, 5))
    assert row["status"] == "present"
    assert row["worked_minutes"] == 520


def test_clear_attendance_entry(hr_client: TestClient, storage, staff):
    from app.services import attendance as attendance_service

    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date="2026-01-05", status="present", check_in="09:30"
    )
    token = csrf_of(hr_client, "/attendance")
    response = hr_client.post(
        "/attendance/clear",
        data={"_csrf": token, "employee_id": staff[0]["id"], "work_date": "2026-01-05", "return_day": "2026-01-05"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert attendance_service.get_row(storage, staff[0]["id"], date(2026, 1, 5)) is None


def test_register_and_csv_exports(hr_client: TestClient, storage, staff):
    from app.services import attendance as attendance_service

    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date="2026-01-05", status="present",
        check_in="09:30", check_out="18:30",
    )
    page = hr_client.get("/attendance/register?period=2026-01")
    assert page.status_code == 200
    assert "Monthly register" in page.text
    assert "Present on this day" in page.text

    full = hr_client.get("/attendance/register.csv?period=2026-01")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/csv")
    assert "attachment" in full.headers["content-disposition"]
    assert "Aarav Sharma" in full.text

    summary = hr_client.get("/attendance/summary.csv?period=2026-01")
    assert "Payable Days" in summary.text

    download = hr_client.get("/attendance/register.csv?period=2026-01&department=Engineering")
    assert "Aarav Sharma" in download.text


# --------------------------------------------------------------------- leave
def test_apply_and_cancel_leave_from_the_ui(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, "/leave")
    response = hr_client.post(
        "/leave/apply",
        data={
            "_csrf": token,
            "employee_id": staff[0]["id"],
            "leave_type": "paid",
            "from_date": "2026-01-05",
            "to_date": "2026-01-06",
            "reason": "family function",
            "period": "2026-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    from app.services import attendance as attendance_service
    from app.services import leave as leave_service

    row = attendance_service.get_row(storage, staff[0]["id"], date(2026, 1, 5))
    assert row["status"] == "leave"

    leave_id = leave_service.list_leave(storage)[0]["id"]
    response = hr_client.post(
        f"/leave/{leave_id}/cancel", data={"_csrf": token, "period": "2026-01"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert attendance_service.get_row(storage, staff[0]["id"], date(2026, 1, 5)) is None


def test_half_day_leave_writes_a_half_day(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, "/leave")
    hr_client.post(
        "/leave/apply",
        data={
            "_csrf": token,
            "employee_id": staff[0]["id"],
            "leave_type": "casual",
            "from_date": "2026-01-07",
            "to_date": "2026-01-07",
            "half_day": "1",
            "period": "2026-01",
        },
        follow_redirects=False,
    )
    from app.services import attendance as attendance_service

    row = attendance_service.get_row(storage, staff[0]["id"], date(2026, 1, 7))
    assert row["status"] == "half_day"
    assert row["leave_type"] == "casual"


def test_leave_over_a_week_off_is_rejected_with_a_message(hr_client: TestClient, staff):
    token = csrf_of(hr_client, "/leave")
    response = hr_client.post(
        "/leave/apply",
        data={
            "_csrf": token,
            "employee_id": staff[0]["id"],
            "leave_type": "paid",
            "from_date": "2026-01-04",   # Sunday
            "to_date": "2026-01-04",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


# ------------------------------------------------------------------- payroll
def test_generate_finalise_and_pay_a_run(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, "/payroll")
    response = hr_client.post(
        "/payroll/generate", data={"_csrf": token, "period": "2026-01"}, follow_redirects=False
    )
    assert response.status_code == 303

    page = hr_client.get("/payroll?period=2026-01")
    assert page.status_code == 200
    assert "Payslips" in page.text
    assert "Aarav Sharma" in page.text

    from app.services import payroll

    run = payroll.get_run(storage, "2026-01")
    assert run["status"] == "draft"
    slip_id = payroll.list_slips(storage, "2026-01")[0]["id"]

    # adjustment
    adjustment = hr_client.post(
        f"/payroll/slip/{slip_id}",
        data={"_csrf": token, "bonus": "2500", "advance": "500", "other_deduction": "0", "period": "2026-01"},
        follow_redirects=False,
    )
    assert adjustment.status_code == 303
    assert payroll.get_slip(storage, slip_id)["bonus"] == 2500

    # finalise
    assert hr_client.post(
        "/payroll/status",
        data={"_csrf": token, "period": "2026-01", "status": "finalized"},
        follow_redirects=False,
    ).status_code == 303
    assert payroll.get_run(storage, "2026-01")["status"] == "finalized"

    # paid
    assert hr_client.post(
        "/payroll/status", data={"_csrf": token, "period": "2026-01", "status": "paid"}, follow_redirects=False
    ).status_code == 303
    assert payroll.get_run(storage, "2026-01")["status"] == "paid"

    # a paid run refuses further edits
    response = hr_client.post(
        f"/payroll/slip/{slip_id}",
        data={"_csrf": token, "bonus": "100", "period": "2026-01"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]


def test_payslip_page_is_printable(hr_client: TestClient, storage, staff):
    from app.services import payroll

    hr_client.post(
        "/payroll/generate", data={"_csrf": csrf_of(hr_client, "/payroll"), "period": "2026-01"},
        follow_redirects=False,
    )
    slip_id = payroll.list_slips(storage, "2026-01")[0]["id"]
    page = hr_client.get(f"/payroll/slip/{slip_id}")
    assert page.status_code == 200
    assert "Salary slip" in page.text
    assert "Net pay" in page.text
    assert "window.print()" in page.text


def test_payroll_csv_export(hr_client: TestClient, staff):
    hr_client.post(
        "/payroll/generate", data={"_csrf": csrf_of(hr_client, "/payroll"), "period": "2026-01"},
        follow_redirects=False,
    )
    export = hr_client.get("/payroll/export.csv?period=2026-01")
    assert export.status_code == 200
    assert "Net Pay" in export.text
    assert "Aarav Sharma" in export.text


# -------------------------------------------------------------------- kiosk
def test_kiosk_scan_marks_attendance(client: TestClient, storage, staff, fake_face, assets, hr_user, kiosk_user, app):
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id=staff[0]["code"], similarity=0.88, face_id="face_1", threshold=0.363,
        faces_in_probe=1, gallery_size=3,
    )
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    token = csrf_of(client, "/kiosk")

    response = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "check_in"
    assert body["employee"]["code"] == staff[0]["code"]
    assert body["similarity"] == 0.88
    assert body["device"] == "Main Gate"
    assert body["server_time"]

    from app.services import attendance as attendance_service

    row = attendance_service.get_row(storage, staff[0]["id"], date.today())
    assert row is not None and row["source"] == "face"


def test_kiosk_scan_reports_unknown_face(client: TestClient, fake_face, assets, hr_user, kiosk_user):
    fake_face.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=0.21, face_id=None, threshold=0.363,
        faces_in_probe=1, gallery_size=3, candidates=[],
    )
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    token = csrf_of(client, "/kiosk")
    body = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_b_1"], "image/jpeg")},
    ).json()
    assert body["ok"] is False
    assert body["action"] == "unknown_face"
    assert "enrol" in body["message"].lower()


def test_kiosk_scan_reports_no_face(client: TestClient, fake_face, assets, hr_user, kiosk_user):
    fake_face.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=None, face_id=None, threshold=None, faces_in_probe=0
    )
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    token = csrf_of(client, "/kiosk")
    body = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["no_face"], "image/png")},
    ).json()
    assert body["action"] == "no_face"
    assert body["kind"] == "warning"


def test_kiosk_scan_handles_face_service_outage(client: TestClient, hr_user, kiosk_user, assets):
    from app.face import FaceServiceUnavailable

    class OfflineFace(FakeFaceService):
        def identify(self, *args, **kwargs):
            raise FaceServiceUnavailable("Cannot reach the face service.")

    client.app.state.face = OfflineFace()
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    token = csrf_of(client, "/kiosk")
    response = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert response.status_code == 503
    assert response.json()["action"] == "service_down"


def test_kiosk_scan_rejects_orphan_template(client: TestClient, staff, fake_face, assets, hr_user, kiosk_user):
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id="GONE-999", similarity=0.9, face_id="face_9", threshold=0.363, faces_in_probe=1
    )
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    token = csrf_of(client, "/kiosk")
    response = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert response.status_code == 409
    assert response.json()["action"] == "unknown_employee"


def test_kiosk_status_endpoint_is_privacy_safe_for_the_gate(
    client: TestClient, hr_user, kiosk_user, fake_face, storage, staff
):
    """The gate screen must never expose other people's attendance."""
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    body = client.get("/api/kiosk/status").json()

    assert body["face_service"]["ok"] is True
    assert body["server_time"]
    assert body["device"] == "Main Gate"
    # No aggregate counters and no list of who scanned — that is HR data.
    assert "counts" not in body
    assert "recent" not in body
    for person in staff:
        assert person["name"] not in str(body)


def test_hr_still_gets_counters_from_the_same_endpoint(
    client: TestClient, hr_user, kiosk_user, fake_face, storage, staff
):
    """HR opening the scanner for a test keeps its monitoring view."""
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD})
    body = client.get("/api/kiosk/status").json()
    assert body["counts"]["headcount"] == 3
    assert "inside_now" in body["counts"]
    assert isinstance(body["recent"], list)


def test_kiosk_screen_shows_no_other_peoples_data(
    client: TestClient, hr_user, kiosk_user, fake_face, storage, staff
):
    """A shared gate device must not leak attendance records or names."""
    from app.services import attendance as attendance_service

    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date="2026-01-05", status="present",
        check_in="09:30", check_out="18:30",
    )
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    page = client.get("/kiosk").text

    # the previous design showed these — they must be gone
    assert "Recent scans" not in page
    assert "Inside now" not in page
    assert "Not marked" not in page
    assert "Headcount" not in page
    for person in staff:
        assert person["name"] not in page, f"{person['name']} leaked onto the kiosk screen"

    # only the two controls, plus the sign-out form, are interactive
    assert page.count("dock-btn") >= 2
    assert "flipBtn" in page and "soundBtn" in page
    assert 'action="/logout"' in page
    assert "scanBtn" not in page and "autoBtn" not in page and "stopBtn" not in page


def test_kiosk_result_hides_the_name_by_default(client: TestClient, settings):
    """HRMS_KIOSK_SHOW_NAME is off unless an operator deliberately turns it on."""
    assert settings.kiosk_show_name is False


def test_hr_can_use_the_kiosk_too(client: TestClient, storage, staff, fake_face, assets, hr_user, kiosk_user):
    """HR opening /kiosk (for a test scan) is recorded against the HR console label."""
    fake_face.identify_result = IdentificationResult(
        matched=True, external_id=staff[1]["code"], similarity=0.8, face_id="face_2", threshold=0.363, faces_in_probe=1
    )
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD})
    token = csrf_of(client, "/kiosk")
    body = client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("scan.jpg", assets["person_a_1"], "image/jpeg")},
    ).json()
    assert body["ok"] is True
    assert body["device"] == "HR console"


# ------------------------------------------------------------------ settings
def test_settings_page_shows_face_health(hr_client: TestClient, fake_face, storage):
    page = hr_client.get("/settings")
    assert page.status_code == 200
    assert "Face service" in page.text
    assert "connected" in page.text
    assert "Shift start" in page.text


def test_update_a_setting(hr_client: TestClient, storage):
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post(
        "/settings/value", data={"_csrf": token, "key": "shift_start", "value": "10:00"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert storage.get_setting("shift_start") == "10:00"


def test_unknown_setting_is_rejected(hr_client: TestClient):
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post(
        "/settings/value", data={"_csrf": token, "key": "not_a_setting", "value": "x"}, follow_redirects=False
    )
    assert "err=" in response.headers["location"]


def test_create_and_disable_a_kiosk_login(hr_client: TestClient, storage):
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post(
        "/settings/users",
        data={
            "_csrf": token,
            "username": "gate2",
            "password": "backgate123",
            "role": "kiosk",
            "display_name": "Back gate",
            "device_label": "Back Gate",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    user = auth.get_user_by_username(storage, "gate2")
    assert user is not None and user["role"] == "kiosk"
    assert user["device_label"] == "Back Gate"

    # short password is refused
    response = hr_client.post(
        "/settings/users",
        data={"_csrf": token, "username": "short", "password": "123", "role": "kiosk"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]

    # duplicate is refused
    response = hr_client.post(
        "/settings/users",
        data={"_csrf": token, "username": "gate2", "password": "backgate123", "role": "kiosk"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]


def test_password_reset_through_settings(hr_client: TestClient, storage):
    user = auth.get_user_by_username(storage, HR_USERNAME)
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post(
        f"/settings/users/{user['id']}/password",
        data={"_csrf": token, "password": "freshpassword1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert auth.authenticate(storage, HR_USERNAME, "freshpassword1") is not None


# ------------------------------------------------------------------- holidays
def test_add_and_remove_a_holiday(hr_client: TestClient, storage):
    token = csrf_of(hr_client, "/attendance")
    response = hr_client.post(
        "/attendance/holidays",
        data={"_csrf": token, "day": "2026-01-26", "name": "Republic Day", "return_to": "/attendance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    holiday = storage.query_one("SELECT * FROM holidays WHERE day = '2026-01-26'")
    assert holiday["name"] == "Republic Day"

    # duplicate is refused
    response = hr_client.post(
        "/attendance/holidays",
        data={"_csrf": token, "day": "2026-01-26", "name": "Again", "return_to": "/attendance"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]

    response = hr_client.post(
        "/attendance/holidays/delete",
        data={"_csrf": token, "holiday_id": holiday["id"], "return_to": "/attendance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert storage.query_one("SELECT * FROM holidays WHERE day = '2026-01-26'") is None


# --------------------------------------------------------------------- audit
def test_audit_log_records_actions(hr_client: TestClient, storage):
    storage.audit(actor="hr", action="unit_test", entity="thing", entity_id=1, detail="hello")
    page = hr_client.get("/audit")
    assert page.status_code == 200
    assert "unit_test" in page.text


# ------------------------------------------------------- asset delivery / cache
def test_html_is_never_cached(client: TestClient):
    """A cached shell would pair new markup with old CSS — the exact bug we hit."""
    response = client.get("/login")
    assert response.headers.get("cache-control") == "no-store"
    kiosk = client.get("/kiosk", follow_redirects=False)
    if kiosk.status_code == 200:
        assert kiosk.headers.get("cache-control") == "no-store"


def test_versioned_assets_are_immutable(client: TestClient):
    from app.deps import ASSET_VERSION

    response = client.get(f"/static/css/app.css?v={ASSET_VERSION}")
    assert response.status_code == 200
    assert "immutable" in response.headers.get("cache-control", "")
    # …and a bare URL must revalidate, never be cached blindly
    bare = client.get("/static/css/app.css")
    assert bare.headers.get("cache-control") == "no-cache"


def test_pages_link_assets_with_the_content_hash(client: TestClient, hr_client: TestClient):
    from app.deps import ASSET_VERSION

    for path in ("/login", "/dashboard", "/kiosk"):
        page = hr_client.get(path)
        if page.status_code != 200:
            continue
        assert f"app.css?v={ASSET_VERSION}" in page.text, path
        assert "static/js/pwa.js?v=" in page.text, path
        bare = "app.css\"" if path == "/login" else None
        assert bare not in page.text if bare else True


def test_service_worker_is_network_first_for_assets(client: TestClient):
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    text = worker.text
    # a bumped cache name is what purges the stale copy on existing devices
    assert 'const CACHE = "fakhri-hrms-v2"' in text
    assert "network first, cache as the offline fallback" in text
    # and it must not go back to serving assets from cache first
    assert "caches.match(request).then((cached) => {\n        if (cached) return cached;" not in text


def test_asset_version_changes_when_a_file_changes(tmp_path):
    """The hash is what busts caches, so it has to actually track file contents."""
    from app.deps import compute_asset_version

    (tmp_path / "css").mkdir()
    (tmp_path / "js").mkdir()
    (tmp_path / "css" / "app.css").write_text("body { color: red }")
    (tmp_path / "js" / "app.js").write_text("console.log(1)")
    first = compute_asset_version(tmp_path)
    assert len(first) == 10

    # same content → same version (so unchanged files stay cached)
    assert compute_asset_version(tmp_path) == first

    (tmp_path / "css" / "app.css").write_text("body { color: maroon }")
    assert compute_asset_version(tmp_path) != first


def test_kiosk_page_registers_the_service_worker(client: TestClient, hr_client: TestClient):
    """The gate device must also pick up updates, not only the HR console."""
    page = hr_client.get("/kiosk")
    assert 'src="/static/js/pwa.js' in page.text


def test_every_class_on_the_kiosk_screen_has_css():
    """Catch the class of bug where new markup meets old/absent styles.

    If a template class has no rule in app.css, the element renders with browser
    defaults — which is exactly what an unstyled-looking kiosk is.
    """
    import re
    from pathlib import Path

    from app.deps import STATIC_DIR

    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "kiosk.html").read_text()
    css = (STATIC_DIR / "css" / "app.css").read_text()

    classes = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        for name in attr.split():
            if "{" not in name and "}" not in name:   # skip Jinja conditionals
                classes.add(name)
    ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))

    missing = [name for name in sorted(classes) if f".{name}" not in css]
    assert not missing, f"kiosk markup uses classes with no CSS rule: {missing}"

    # the hooks the JavaScript drives must exist in the markup as well
    for hook in (
        "video", "cam", "guide", "flash", "flashIco", "flashTxt", "flashTime",
        "startBtn", "flipBtn", "soundBtn", "soundIco", "soundLbl", "liveDot",
        "clock", "stateDot",
    ):
        assert hook in ids, f"kiosk.js expects #{hook} in kiosk.html"
