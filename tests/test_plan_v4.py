"""Plan v4: duplicate faces, punch permissions, late policy and the kiosk confirmation card."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Storage
from app.face import IdentificationResult
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import orgtime, payroll
from tests.conftest import FakeFaceService
from tests.helpers import csrf_of

FACE_A = b"\xff\xd8\xff face of person A"
FACE_A_NEW = b"\xff\xd8\xff person A, new angle"


# ------------------------------------------------------------ Feature 1
def test_f1_same_face_cannot_be_enrolled_twice(storage: Storage, staff):
    fake = FakeFaceService()
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A)
    with pytest.raises(employees_service.DuplicateFaceError) as caught:
        employees_service.enrol_face(storage, fake, staff[1]["id"], FACE_A)
    assert staff[0]["name"] in str(caught.value) and staff[0]["code"] in str(caught.value)
    assert employees_service.get_employee(storage, staff[1]["id"])["face_id"] is None


def test_f1_reenrolling_yourself_is_allowed(storage: Storage, staff):
    fake = FakeFaceService()
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A)
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A)        # same photo again
    fake.identify_result = IdentificationResult(                              # new angle still matches A
        matched=True, external_id=f"emp-{staff[0]['id']}", similarity=0.71, face_id="x", threshold=0.363,
        faces_in_probe=1, candidates=[{"external_id": f"emp-{staff[0]['id']}", "similarity": 0.71}])
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A_NEW)


def test_f1_second_candidate_above_threshold_is_caught(storage: Storage, staff):
    fake = FakeFaceService()
    fake.identify_result = IdentificationResult(
        matched=True, external_id=f"emp-{staff[0]['id']}", similarity=0.8, face_id="x", threshold=0.363,
        faces_in_probe=1, candidates=[
            {"external_id": f"emp-{staff[0]['id']}", "similarity": 0.8},
            {"external_id": staff[1]["code"], "similarity": 0.5},              # legacy code key
            {"external_id": "emp-9999", "similarity": 0.6},                    # orphan template
        ])
    with pytest.raises(employees_service.DuplicateFaceError) as caught:
        employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A_NEW)
    assert staff[1]["code"] in str(caught.value)


def test_f1_below_threshold_and_orphans_do_not_block(storage: Storage, staff):
    fake = FakeFaceService()
    fake.identify_result = IdentificationResult(
        matched=False, external_id=None, similarity=0.3, face_id=None, threshold=0.363, faces_in_probe=1,
        candidates=[{"external_id": f"emp-{staff[1]['id']}", "similarity": 0.30},
                    {"external_id": "emp-9999", "similarity": 0.9}])
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A)


def test_f1_hr_sees_the_owner(hr_client: TestClient, app, storage: Storage, staff):
    fake = FakeFaceService()
    app.state.face = fake
    employees_service.enrol_face(storage, fake, staff[0]["id"], FACE_A)
    token = csrf_of(hr_client, f"/employees/{staff[1]['id']}")
    response = hr_client.post(f"/employees/{staff[1]['id']}/face", data={"_csrf": token},
                              files={"image": ("a.jpg", FACE_A, "image/jpeg")}, follow_redirects=False)
    page = hr_client.get(response.headers["location"]).text
    assert "already registered to" in page and staff[0]["name"] in page
