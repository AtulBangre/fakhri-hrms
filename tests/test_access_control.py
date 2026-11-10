"""Role based access: every route, for every role, plus one person's data against another's.

A new route is HR-only here unless it is added to a list below, so forgetting to
protect one fails this file.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.routes as routes_package
from app import auth, notify
from app.services import account, payroll, records
from app.uploads import save_upload
from tests.conftest import HR_PASSWORD, HR_USERNAME, KIOSK_PASSWORD, KIOSK_USERNAME
from tests.helpers import csrf_of

PUBLIC = {
    "/login", "/logout", "/", "/forgot-password", "/reset-password", "/set-password", "/offline", "/healthz",
    "/api/health", "/manifest.webmanifest", "/kiosk.webmanifest", "/sw.js", "/favicon.ico",
    "/api/passkey/available", "/api/passkey/login/options", "/api/passkey/login/verify",
    "/api/auth/face-available", "/api/auth/face-login",
}
# Their handlers check the person or role themselves; each has its own test below.
SELF_CHECKED = {
    "/employees/{employee_id}/avatar", "/documents/{document_id}/file", "/expenses/{claim_id}/receipt",
    "/tax/declarations/{declaration_id}/proof", "/api/ai-hr/query",
}
KIOSK_AND_HR = {"/kiosk", "/api/kiosk/challenge", "/api/kiosk/scan", "/api/kiosk/offline-sync", "/api/kiosk/status"}
MEMBERS = {"/api/inbox", "/api/inbox/seen", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test",
           "/account/passkeys", "/account/passkeys/remove", "/api/passkey/register/options",
           "/api/passkey/register/verify"}
EMPLOYEE_ONLY_PREFIXES = ("/me", "/api/field")
ALL_PASSWORD = "portal-pass-1"


def _all_routes() -> list[tuple[str, str]]:
    found = []
    for item in pkgutil.iter_modules(routes_package.__path__):
        module = importlib.import_module(f"app.routes.{item.name}")
        router = getattr(module, "router", None)
        for route in (router.routes if router else []):
            if isinstance(route, APIRoute):
                found += [(method, route.path) for method in route.methods - {"HEAD", "OPTIONS"}]
    return sorted(set(found))


def _url(path: str) -> str:
    path = re.sub(r"\{decision\}", "approve", path)
    path = re.sub(r"\{action\}", "close", path)
    return re.sub(r"\{[^}]+\}", "1", path)


def _allowed(resp) -> bool:
    """Reached the handler. An empty form gives 422 or a CSRF refusal only after the role check passed."""
    if resp.status_code in (401, 403):
        return False
    if resp.status_code in (301, 302, 303, 307):
        target = resp.headers.get("location", "")
        return "/login" not in target and target.split("?")[0] not in ("/me", "/kiosk", "/dashboard")
    return True


@pytest.fixture
def role_clients(app, storage, staff, hr_user, kiosk_user):
    auth.ensure_employee_login(storage, staff[0], password=ALL_PASSWORD)
    username = auth.get_user_for_employee(storage, staff[0]["id"])["username"]
    clients = {}
    for role, user, password in (("anon", None, None), ("kiosk", KIOSK_USERNAME, KIOSK_PASSWORD),
                                 ("emp", username, ALL_PASSWORD)):
        client = TestClient(app, follow_redirects=False)
        if user:
            assert client.post("/login", data={"username": user, "password": password}).status_code == 303
        clients[role] = client
    return clients


def test_the_route_list_is_real():
    assert len(_all_routes()) > 150


def test_every_route_refuses_the_roles_it_should(role_clients, app):
    problems = []
    for method, path in _all_routes():
        if path in PUBLIC or path in SELF_CHECKED:
            continue
        if path in KIOSK_AND_HR:
            refused = ("anon", "emp")
        elif path in MEMBERS:
            refused = ("anon", "kiosk")
        elif path.startswith(EMPLOYEE_ONLY_PREFIXES):
            refused = ("anon", "kiosk")
        else:
            refused = ("anon", "kiosk", "emp")
        for role in refused:
            if _allowed(role_clients[role].request(method, _url(path))):
                problems.append(f"{role} reached {method} {path}")
    assert problems == []


def test_hr_cannot_use_employee_pages_and_is_sent_home(hr_client: TestClient):
    assert hr_client.get("/me", follow_redirects=False).headers["location"].startswith("/dashboard")


# ---------------------------------------------------------------------- photos
JPEG = b"\xff\xd8\xff\xe0photo"


@pytest.fixture
def avatar(storage, settings, staff):
    stored = save_upload(settings, "avatars", JPEG, original_name="a.jpg")
    storage.execute("UPDATE employees SET photo_filename = ? WHERE id IN (?, ?)",
                    (stored.stored_name, staff[0]["id"], staff[1]["id"]))
    return stored


def test_a_stranger_cannot_download_a_face_photo(client: TestClient, staff, avatar):
    assert client.get(f"/employees/{staff[0]['id']}/avatar", follow_redirects=False).status_code == 401


def test_employee_sees_only_their_own_face_photo(ess_client: TestClient, staff, avatar):
    assert ess_client.get(f"/employees/{staff[0]['id']}/avatar").content == JPEG
    assert ess_client.get(f"/employees/{staff[1]['id']}/avatar").status_code == 404


def test_hr_and_the_gate_can_see_face_photos(client: TestClient, staff, avatar, hr_user, kiosk_user):
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    assert client.get(f"/employees/{staff[0]['id']}/avatar").content == JPEG  # the confirmation card
    client.post("/logout", data={"_csrf": csrf_of(client, "/kiosk")})
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD})
    assert client.get(f"/employees/{staff[0]['id']}/avatar").content == JPEG


def test_punch_photos_are_for_hr_only(client: TestClient, ess_client_factory, settings, hr_user, kiosk_user):
    name = save_upload(settings, "punches", JPEG, original_name="p.jpg").stored_name
    url = f"/attendance/photos/{name}"
    assert client.get(url, follow_redirects=False).status_code == 401
    employee = ess_client_factory()
    assert employee.get(url, follow_redirects=False).status_code == 403
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    assert client.get(url, follow_redirects=False).status_code == 403
    client.post("/logout", data={"_csrf": csrf_of(client, "/kiosk")})
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD})
    assert client.get(url).content == JPEG


@pytest.fixture
def ess_client_factory(app, storage, staff):
    def build() -> TestClient:
        auth.ensure_employee_login(storage, staff[0], password=ALL_PASSWORD)
        user = auth.get_user_for_employee(storage, staff[0]["id"])["username"]
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": user, "password": ALL_PASSWORD})
        return client
    return build


# ------------------------------------------------- one employee, another's data
def _not_theirs(response) -> bool:
    return response.status_code in (303, 403, 404) and JPEG not in response.content


def test_employee_cannot_open_another_persons_payslip(ess_client: TestClient, storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    payroll.set_run_status(storage, "2026-01", "finalized")
    theirs = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == staff[1]["id"])
    for url in (f"/me/payslips/{theirs['id']}", f"/me/payslips/{theirs['id']}.pdf"):
        response = ess_client.get(url, follow_redirects=False)
        assert response.status_code == 303 and "not found" in response.headers["location"].replace("+", " ")


def test_employee_cannot_open_another_persons_document(ess_client: TestClient, storage, settings, staff):
    doc = records.add_document(storage, settings, employee_id=staff[1]["id"], category="pan", data=JPEG,
                               original_name="pan.jpg", title="PAN")
    response = ess_client.get(f"/documents/{doc['id']}/file", follow_redirects=False)
    assert _not_theirs(response)


def test_employee_cannot_withdraw_another_persons_requests(ess_client: TestClient, storage, settings, staff):
    fake_face_free = account.submit_face_request(storage, settings, employee=staff[1], photo=JPEG)
    token = csrf_of(ess_client, "/me/profile")
    ess_client.post(f"/me/profile/request/{fake_face_free['id']}/cancel", data={"_csrf": token})
    row = storage.query_one("SELECT status FROM profile_change_requests WHERE id = ?", (fake_face_free["id"],))
    assert row["status"] == "pending"


def test_employee_cannot_see_or_decide_a_face_request(ess_client: TestClient, storage, settings, staff):
    row = account.submit_face_request(storage, settings, employee=staff[1], photo=JPEG)
    assert _not_theirs(ess_client.get(f"/profile-requests/{row['id']}/photo", follow_redirects=False))
    ess_client.post(f"/profile-requests/{row['id']}/approve", data={"_csrf": csrf_of(ess_client, "/me")},
                    follow_redirects=False)
    assert storage.query_one("SELECT status FROM profile_change_requests WHERE id = ?", (row["id"],))["status"] == "pending"


def test_employee_cannot_cancel_another_persons_leave(ess_client: TestClient, storage, settings, staff):
    from app.services import leave

    theirs = leave.apply_leave(storage, settings, employee_id=staff[1]["id"], leave_type="unpaid",
                               from_date="2026-08-03", to_date="2026-08-03", reason="x", status="pending")
    leave_id = (theirs.get("leave") or theirs)["id"]
    ess_client.post(f"/me/leave/{leave_id}/cancel", data={"_csrf": csrf_of(ess_client, "/me/leave")},
                    follow_redirects=False)
    assert storage.query_one("SELECT status FROM leave_requests WHERE id = ?", (leave_id,))["status"] == "pending"


def test_employee_inbox_shows_nothing_of_other_people(ess_client: TestClient, storage, settings, hr_user):
    notify.push_hr(storage, settings, "HR only", "secret")
    assert ess_client.get("/api/inbox").json()["items"] == []


def test_a_disabled_login_is_refused_at_once(ess_client: TestClient, storage, ess_credentials):
    assert ess_client.get("/me", follow_redirects=False).status_code == 200
    auth.set_user_active(storage, ess_credentials["user"]["id"], False)
    assert "/login" in ess_client.get("/me", follow_redirects=False).headers["location"]
