"""Documents, assets, expense claims and GPS field attendance."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import field as field_service
from app.services import records
from tests.conftest import HR_PASSWORD, HR_USERNAME
from tests.helpers import csrf_of

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 400
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 400


def _upload(client: TestClient, path: str, token: str, **form) -> object:
    """Post a multipart form with a file (and any extra fields)."""
    files = {k: v for k, v in form.items() if isinstance(v, tuple)}
    data = {k: v for k, v in form.items() if not isinstance(v, tuple)}
    data["_csrf"] = token
    return client.post(path, data=data, files=files, follow_redirects=False)


# ================================================================== documents
def test_hr_uploads_a_document_and_it_is_stored(hr_client, storage, settings, staff):
    token = csrf_of(hr_client, "/documents")
    response = _upload(
        hr_client, "/documents", token,
        employee_id=str(staff[0]["id"]), category="aadhaar", title="Aadhaar front",
        file=("aadhaar.pdf", PDF, "application/pdf"),
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    rows = records.list_documents(storage, employee_id=staff[0]["id"])
    assert len(rows) == 1
    assert rows[0]["category"] == "aadhaar"
    assert rows[0]["filename"] == "aadhaar.pdf"
    assert rows[0]["size"] == len(PDF)
    # the file on disk uses a random name, not the uploaded one
    stored = Path(settings.data_dir) / "uploads" / "documents" / rows[0]["stored_name"]
    assert stored.exists() and stored.name != "aadhaar.pdf"


def test_upload_rejects_a_wrong_file_type(hr_client, storage, staff):
    token = csrf_of(hr_client, "/documents")
    response = _upload(
        hr_client, "/documents", token, employee_id=str(staff[0]["id"]), category="pan",
        file=("cv.exe", b"MZ\x90\x00binary", "application/octet-stream"),
    )
    assert "err=" in response.headers["location"]
    assert records.list_documents(storage) == []


def test_upload_rejects_an_oversized_file(hr_client, storage, staff):
    token = csrf_of(hr_client, "/documents")
    big = PDF + b"0" * (9 * 1024 * 1024)
    response = _upload(
        hr_client, "/documents", token, employee_id=str(staff[0]["id"]), category="other",
        file=("huge.pdf", big, "application/pdf"),
    )
    assert "err=" in response.headers["location"]
    assert records.list_documents(storage) == []


def test_employee_sees_only_their_own_documents(
    ess_client, storage, settings, staff, ess_credentials
):
    """Documents are created through the service, then read as the employee.

    Note: the ``ess_client`` and ``hr_client`` fixtures share one underlying
    TestClient, so a test must never request both — the second sign-in would
    replace the first session.
    """
    mine = records.add_document(
        storage, settings, employee_id=staff[0]["id"], data=PDF, original_name="mine.pdf",
        category="pan", uploaded_by="hr",
    )
    other = records.add_document(
        storage, settings, employee_id=staff[1]["id"], data=JPEG, original_name="other.jpg",
        category="pan", uploaded_by="hr",
    )
    page = ess_client.get("/me/documents")
    assert page.status_code == 200
    assert "mine.pdf" in page.text
    assert "other.jpg" not in page.text
    assert staff[1]["name"] not in page.text

    # the owner can download, a colleague cannot
    ok = ess_client.get(f"/documents/{mine['id']}/file")
    assert ok.status_code == 200 and ok.content == PDF

    blocked = ess_client.get(f"/documents/{other['id']}/file", follow_redirects=False)
    assert blocked.status_code == 303
    assert "err=" in blocked.headers["location"]


def test_hr_can_verify_and_delete_a_document(hr_client, storage, settings, staff):
    doc = records.add_document(
        storage, settings, employee_id=staff[0]["id"], data=PDF, original_name="x.pdf",
        category="aadhaar", uploaded_by="hr",
    )
    on_disk = Path(settings.data_dir) / "uploads" / "documents" / doc["stored_name"]
    assert on_disk.exists()

    token = csrf_of(hr_client, "/documents")
    hr_client.post(f"/documents/{doc['id']}/verify", data={"_csrf": token}, follow_redirects=False)
    assert records.get_document(storage, doc["id"])["verified"] == 1

    hr_client.post(f"/documents/{doc['id']}/delete", data={"_csrf": token}, follow_redirects=False)
    assert records.list_documents(storage) == []
    assert not on_disk.exists()


def test_document_summary_flags_missing_kyc(storage, settings, staff):
    summary = records.document_summary(storage, staff[0]["id"])
    assert set(summary["missing_required"]) == {"aadhaar", "pan"}
    records.add_document(storage, settings, employee_id=staff[0]["id"], data=PDF,
                         original_name="a.pdf", category="aadhaar")
    after = records.document_summary(storage, staff[0]["id"])
    assert after["missing_required"] == ["pan"]
    assert after["total"] == 1


# ===================================================================== assets
def test_asset_lifecycle_assign_and_return(hr_client, storage, staff):
    token = csrf_of(hr_client, "/assets")
    created = hr_client.post(
        "/assets",
        data={"_csrf": token, "tag": "fits-lap-001", "name": "Dell Latitude", "category": "laptop",
              "serial": "SN123", "cost": "55000"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    asset = records.get_asset_by_tag(storage, "FITS-LAP-001")
    assert asset["tag"] == "FITS-LAP-001"       # normalised to upper case
    assert asset["status"] == "in_stock"
    assert asset["holder"] is None

    hr_client.post(
        f"/assets/{asset['id']}/assign",
        data={"_csrf": token, "employee_id": staff[0]["id"], "condition_note": "new"},
        follow_redirects=False,
    )
    assigned = records.get_asset(storage, asset["id"])
    assert assigned["status"] == "assigned"
    assert assigned["holder"]["employee_name"] == staff[0]["name"]

    # a second assignment must be refused until it comes back
    response = hr_client.post(
        f"/assets/{asset['id']}/assign", data={"_csrf": token, "employee_id": staff[1]["id"]},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]

    hr_client.post(f"/assets/{asset['id']}/return", data={"_csrf": token}, follow_redirects=False)
    returned = records.get_asset(storage, asset["id"])
    assert returned["status"] == "in_stock" and returned["holder"] is None
    assert len(records.asset_history(storage, asset["id"])) == 1


def test_duplicate_asset_tag_is_rejected(storage):
    records.add_asset(storage, tag="LAP-1", name="First")
    with pytest.raises(ValueError):
        records.add_asset(storage, tag="lap-1", name="Second")


def test_employee_sees_their_own_assets(ess_client, storage, staff, ess_credentials):
    mine = records.add_asset(storage, tag="SIM-1", name="Airtel SIM", category="sim")
    theirs = records.add_asset(storage, tag="SIM-2", name="Jio SIM", category="sim")
    records.assign_asset(storage, asset_id=mine["id"], employee_id=staff[0]["id"])
    records.assign_asset(storage, asset_id=theirs["id"], employee_id=staff[1]["id"])

    page = ess_client.get("/me/assets")
    assert page.status_code == 200
    assert "SIM-1" in page.text and "Airtel SIM" in page.text
    assert "SIM-2" not in page.text


# =================================================================== expenses
def test_employee_submits_a_claim_with_a_receipt(ess_client, storage, settings, staff, ess_credentials):
    token = csrf_of(ess_client, "/me/expenses")
    response = ess_client.post(
        "/me/expenses",
        data={"_csrf": token, "category": "travel", "amount": "1250.50",
              "expense_date": "2026-01-05", "description": "Cab to client"},
        files={"receipt": ("bill.jpg", JPEG, "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    claims = records.list_claims(storage, employee_id=staff[0]["id"])
    assert len(claims) == 1
    claim = claims[0]
    assert claim["amount"] == 1250.50
    assert claim["status"] == "pending"
    assert claim["has_receipt"] is True
    receipt = Path(settings.data_dir) / "uploads" / "receipts" / claim["receipt_name"]
    assert receipt.exists()


def test_claim_validation(ess_client, storage, staff, ess_credentials, settings):
    token = csrf_of(ess_client, "/me/expenses")
    for payload in (
        {"category": "travel", "amount": "0", "expense_date": "2026-01-05"},
        {"category": "travel", "amount": "-50", "expense_date": "2026-01-05"},
        {"category": "travel", "amount": "100", "expense_date": "2099-01-01"},  # future
        {"category": "travel", "amount": "999999999", "expense_date": "2026-01-05"},
    ):
        response = ess_client.post(
            "/me/expenses", data={"_csrf": token, **payload}, follow_redirects=False
        )
        assert "err=" in response.headers["location"], payload
    assert records.list_claims(storage, employee_id=staff[0]["id"]) == []


def test_hr_approves_a_claim_and_the_employee_sees_it(
    client, storage, settings, staff, ess_credentials, hr_user, kiosk_user
):
    claim = records.submit_claim(
        storage, settings, employee_id=staff[0]["id"], amount=800, expense_date="2026-02-01",
        category="food", description="Team lunch",
    )
    # HR signs in
    client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    token = csrf_of(client, "/expenses")
    page = client.get("/expenses")
    assert "Team lunch" in page.text

    response = client.post(f"/expenses/{claim['id']}/approve", data={"_csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert records.get_claim(storage, claim["id"])["status"] == "approved"

    # the employee sees the decision on their own page
    client.post(
        "/login",
        data={"username": ess_credentials["username"], "password": ess_credentials["password"]},
        follow_redirects=False,
    )
    page = client.get("/me/expenses")
    assert "approved" in page.text


def test_receipt_access_is_restricted(
    ess_client, storage, settings, staff, ess_credentials
):
    mine = records.submit_claim(
        storage, settings, employee_id=staff[0]["id"], amount=100, expense_date="2026-02-02",
        receipt=(JPEG, "mine.jpg"),
    )
    theirs = records.submit_claim(
        storage, settings, employee_id=staff[1]["id"], amount=200, expense_date="2026-02-03",
        receipt=(PNG, "theirs.png"),
    )
    assert ess_client.get(f"/expenses/{mine['id']}/receipt").status_code == 200

    blocked = ess_client.get(f"/expenses/{theirs['id']}/receipt", follow_redirects=False)
    assert blocked.status_code == 303
    assert "err=" in blocked.headers["location"]


def test_expense_stats_totals(storage, settings, staff):
    records.submit_claim(storage, settings, employee_id=staff[0]["id"], amount=100, expense_date="2026-02-01")
    second = records.submit_claim(storage, settings, employee_id=staff[0]["id"], amount=250, expense_date="2026-02-02")
    records.decide_claim(storage, claim_id=second["id"], approve=True, decided_by="hr")
    stats = records.expense_stats(storage)
    assert stats["pending"]["count"] == 1 and stats["pending"]["total"] == 100
    assert stats["approved"]["count"] == 1 and stats["approved"]["total"] == 250


# ================================================================ field / GPS
def test_haversine_is_accurate_enough():
    # Bhopal → Indore is ~170 km in a straight line
    d = field_service.haversine_m(23.2599, 77.4126, 22.7196, 75.8577)
    assert 165_000 < d < 175_000
    # same point
    assert field_service.haversine_m(23.0, 77.0, 23.0, 77.0) < 1


def test_field_check_is_blocked_until_a_location_exists(ess_client, storage, staff, ess_credentials, settings):
    storage.set_setting("allow_employee_self_punch", "geofence_only")
    token = csrf_of(ess_client, "/me/field")
    response = ess_client.post(
        "/api/field/check",
        data={"_csrf": token, "lat": "23.2599", "lng": "77.4126", "kind": "check_in"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["inside"] is False
    assert "no work location" in body["message"].lower() or "not configured" in body["message"].lower()


def test_field_check_inside_marks_attendance(ess_client, storage, settings, staff, ess_credentials):
    storage.set_setting("allow_employee_self_punch", "geofence_only")
    field_service.add_location(storage, name="Client site", lat=23.2599, lng=77.4126, radius_m=300)
    token = csrf_of(ess_client, "/me/field")
    response = ess_client.post(
        "/api/field/check",
        data={"_csrf": token, "lat": "23.2600", "lng": "77.4127", "accuracy": "12",
              "kind": "check_in", "note": "client visit"},
        files={"selfie": ("selfie.jpg", JPEG, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["inside"] is True
    assert body["location"]["name"] == "Client site"

    row = attendance_service.get_row(storage, staff[0]["id"], __import__("datetime").date.today())
    assert row is not None
    assert row["source"] == "field"
    assert row["device_label"] == "Field · Client site"

    checks = field_service.checks_for(storage, employee_id=staff[0]["id"])
    assert checks and checks[0]["inside"] == 1
    assert checks[0]["selfie_name"]


def test_field_check_outside_does_not_mark(ess_client, storage, settings, staff, ess_credentials):
    storage.set_setting("allow_employee_self_punch", "geofence_only")
    field_service.add_location(storage, name="Office", lat=23.2599, lng=77.4126, radius_m=100)
    token = csrf_of(ess_client, "/me/field")
    response = ess_client.post(
        "/api/field/check",
        data={"_csrf": token, "lat": "23.3000", "lng": "77.4500", "kind": "check_in"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["inside"] is False
    assert body["distance_m"] and body["distance_m"] > 100
    assert "not marked" in body["message"].lower()

    assert attendance_service.get_row(storage, staff[0]["id"], __import__("datetime").date.today()) is None
    # but the failed attempt is still on record for HR
    checks = field_service.checks_for(storage, employee_id=staff[0]["id"])
    assert checks and checks[0]["inside"] == 0


def test_nearest_location_picks_the_closest(storage):
    field_service.add_location(storage, name="Far", lat=23.0000, lng=77.0000, radius_m=100)
    field_service.add_location(storage, name="Near", lat=23.2599, lng=77.4126, radius_m=100)
    location, distance = field_service.nearest(storage, 23.2600, 77.4127)
    assert location["name"] == "Near"
    assert distance < 50


def test_hr_location_management(hr_client, storage):
    token = csrf_of(hr_client, "/locations")
    response = hr_client.post(
        "/locations",
        data={"_csrf": token, "name": "Warehouse", "lat": "23.2", "lng": "77.4", "radius_m": "150"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = field_service.list_locations(storage)
    assert len(rows) == 1 and rows[0]["name"] == "Warehouse"

    location_id = rows[0]["id"]
    hr_client.post(f"/locations/{location_id}/toggle", data={"_csrf": token}, follow_redirects=False)
    assert field_service.get_location(storage, location_id)["active"] == 0

    hr_client.post(f"/locations/{location_id}/delete", data={"_csrf": token}, follow_redirects=False)
    assert field_service.list_locations(storage) == []


def test_location_needs_a_sane_radius(storage):
    with pytest.raises(ValueError):
        field_service.add_location(storage, name="Tiny", lat=1, lng=1, radius_m=5)
    with pytest.raises(ValueError):
        field_service.add_location(storage, name="", lat=1, lng=1)


# ------------------------------------------------------------------- surfaces
def test_new_pages_render(hr_client, staff, storage, settings):
    for path in ("/documents", "/assets", "/expenses", "/locations"):
        assert hr_client.get(path).status_code == 200, path


def test_employee_pages_render(ess_client, staff, storage, settings):
    for path in ("/me/documents", "/me/assets", "/me/expenses", "/me/field"):
        assert ess_client.get(path).status_code == 200, path


def test_hr_cannot_open_employee_pages(hr_client):
    response = hr_client.get("/me/documents", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
