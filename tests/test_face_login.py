"""Tests for Employee Face Recognition Sign-in (/api/auth/face-login and /api/auth/face-available)."""

from __future__ import annotations

import base64
from fastapi.testclient import TestClient

from app import auth
from app.face import IdentificationResult
from tests.conftest import FakeFaceService


def test_face_available_endpoint(client: TestClient, fake_face: FakeFaceService):
    response = client.get("/api/auth/face-available")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True


def test_face_login_success_and_session(client: TestClient, storage, staff, fake_face: FakeFaceService):
    employee = staff[0]
    photo = b"sample_face_bytes_emp1"
    key = f"emp-{employee['id']}"

    # Enrol face into fake face gallery and update employee record
    res = fake_face.enrol_face(photo, external_id=key)
    fake_face.photos[photo] = key
    storage.execute("UPDATE employees SET face_id = ? WHERE id = ?", (res.face_id, employee["id"]))

    # Also create employee portal login
    user, _ = auth.ensure_employee_login(storage, employee, password="emp-pass-123")

    # Call face login endpoint
    b64_img = base64.b64encode(photo).decode("ascii")
    response = client.post(
        "/api/auth/face-login",
        json={"image": f"data:image/jpeg;base64,{b64_img}", "next": "/me"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["redirect"] == "/me"
    assert data["name"] == employee["name"]

    # Verify session cookie was set and can access /me
    assert "hrms_session" in response.cookies
    me_resp = client.get("/me")
    assert me_resp.status_code == 200
    assert employee["name"] in me_resp.text

    # Verify audit log
    audit_row = storage.query_one(
        "SELECT * FROM audit_log WHERE entity = 'user' AND action = 'login' ORDER BY id DESC LIMIT 1"
    )
    assert audit_row is not None
    assert "face recognition" in audit_row["detail"]


def test_face_login_auto_provisions_user(client: TestClient, storage, staff, fake_face: FakeFaceService):
    """If an active employee has their face enrolled but never created a password account, face login creates one."""
    employee = staff[1]
    photo = b"sample_face_bytes_emp2"
    key = f"emp-{employee['id']}"

    res = fake_face.enrol_face(photo, external_id=key)
    fake_face.photos[photo] = key
    storage.execute("UPDATE employees SET face_id = ? WHERE id = ?", (res.face_id, employee["id"]))

    # Ensure no pre-existing user row
    storage.execute("DELETE FROM users WHERE employee_id = ?", (employee["id"],))
    assert auth.get_user_for_employee(storage, employee["id"]) is None

    # Face login should auto-create the user
    response = client.post(
        "/api/auth/face-login",
        files={"image": ("face.jpg", photo, "image/jpeg")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

    # User row should now exist
    new_user = auth.get_user_for_employee(storage, employee["id"])
    assert new_user is not None
    assert new_user["active"] == 1


def test_face_login_unrecognised_face(client: TestClient, fake_face: FakeFaceService):
    unknown_photo = b"stranger_photo_bytes"
    response = client.post(
        "/api/auth/face-login",
        files={"image": ("face.jpg", unknown_photo, "image/jpeg")},
    )
    # fake_face.photos has no entry, but identify will return no_face or no match
    assert response.status_code in (400, 401)
    data = response.json()
    assert data["ok"] is False


def test_face_login_rejects_spoof(client: TestClient, fake_face: FakeFaceService):
    photo = b"some_spoofed_photo"
    fake_face.identify_result = IdentificationResult(
        matched=True,
        external_id="emp-1",
        similarity=0.95,
        face_id="face_0001",
        threshold=0.36,
        faces_in_probe=1,
        decision="spoof",
        is_live=False,
        spoof_reason="printed_photo",
        liveness_score=0.12,
    )

    response = client.post(
        "/api/auth/face-login",
        files={"image": ("face.jpg", photo, "image/jpeg")},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["ok"] is False
    assert "Liveness check failed" in data["detail"]


def test_face_login_inactive_employee_rejected(client: TestClient, storage, staff, fake_face: FakeFaceService):
    employee = staff[2]
    photo = b"sample_face_bytes_emp3"
    key = f"emp-{employee['id']}"

    res = fake_face.enrol_face(photo, external_id=key)
    fake_face.photos[photo] = key
    storage.execute("UPDATE employees SET face_id = ?, status = 'resigned' WHERE id = ?", (res.face_id, employee["id"]))

    response = client.post(
        "/api/auth/face-login",
        files={"image": ("face.jpg", photo, "image/jpeg")},
    )
    assert response.status_code == 403
    data = response.json()
    assert "inactive" in data["detail"].lower()


def test_face_login_disabled_user_rejected(client: TestClient, storage, staff, fake_face: FakeFaceService):
    employee = staff[0]
    photo = b"sample_face_bytes_emp_disabled"
    key = f"emp-{employee['id']}"

    res = fake_face.enrol_face(photo, external_id=key)
    fake_face.photos[photo] = key
    storage.execute("UPDATE employees SET face_id = ? WHERE id = ?", (res.face_id, employee["id"]))

    user, _ = auth.ensure_employee_login(storage, employee)
    storage.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))

    response = client.post(
        "/api/auth/face-login",
        files={"image": ("face.jpg", photo, "image/jpeg")},
    )
    assert response.status_code == 403
    data = response.json()
    assert "disabled" in data["detail"].lower()
