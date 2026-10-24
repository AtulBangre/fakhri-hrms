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


# ------------------------------------------------------------ Feature 3
from app.services import field as field_service  # noqa: E402


def _check(ess_client, lat="23.2600", lng="77.4127"):
    token = csrf_of(ess_client, "/me")
    return ess_client.post("/api/field/check", data={"_csrf": token, "lat": lat, "lng": lng, "kind": "check_in"})


def test_f3_default_is_kiosk_only(ess_client: TestClient, storage: Storage, ess_credentials):
    field_service.add_location(storage, name="Office", lat=23.2599, lng=77.4126, radius_m=300)
    body = _check(ess_client).json()
    assert body["ok"] is False and "gate scanner" in body["message"]
    assert 'href="/me/field"' not in ess_client.get("/me").text  # menu item hidden
    assert "gate kiosk only" in ess_client.get("/me/field").text


def test_f3_geofence_only(ess_client: TestClient, storage: Storage, ess_credentials):
    storage.set_setting("allow_employee_self_punch", "geofence_only")
    field_service.add_location(storage, name="Office", lat=23.2599, lng=77.4126, radius_m=300)
    assert _check(ess_client, lat="23.3000", lng="77.4500").json()["ok"] is False   # outside
    assert _check(ess_client).json()["ok"] is True                                   # inside
    assert 'href="/me/field"' in ess_client.get("/me").text


def test_f3_always_allowed_marks_from_anywhere(ess_client: TestClient, storage: Storage, ess_credentials):
    storage.set_setting("allow_employee_self_punch", "always_allowed")
    response = _check(ess_client, lat="19.0760", lng="72.8777")                       # no location configured
    assert response.status_code == 200 and response.json()["ok"] is True
    row = attendance_service.get_row(storage, ess_credentials["employee"]["id"], orgtime.today(ess_client.app.state.settings))
    assert row["source"] == "field" and row["device_label"] == "Field · Remote"
    check = field_service.checks_for(storage, employee_id=ess_credentials["employee"]["id"])[0]
    assert check["lat"] == pytest.approx(19.0760)  # position still recorded


def test_f3_per_employee_override(ess_client: TestClient, storage: Storage, ess_credentials):
    employees_service.update_employee(storage, ess_credentials["employee"]["id"], {"self_punch_mode": "always_allowed"})
    assert _check(ess_client).json()["ok"] is True                                   # company says disabled
    storage.set_setting("allow_employee_self_punch", "always_allowed")
    employees_service.update_employee(storage, ess_credentials["employee"]["id"], {"self_punch_mode": "disabled"})
    assert _check(ess_client).json()["ok"] is False                                  # override wins
    with pytest.raises(employees_service.ValidationError):
        employees_service.update_employee(storage, ess_credentials["employee"]["id"], {"self_punch_mode": "bogus"})


def test_f3_employee_gets_403_at_the_kiosk(ess_client: TestClient):
    assert ess_client.get("/kiosk", follow_redirects=False).status_code == 403
    assert ess_client.get("/api/kiosk/status").status_code == 403


def test_f3_hr_sets_the_override_on_the_edit_form(hr_client: TestClient, storage: Storage, staff):
    token = csrf_of(hr_client, f"/employees/{staff[0]['id']}/edit")
    assert "Where can they mark attendance?" in hr_client.get(f"/employees/{staff[0]['id']}/edit").text
    hr_client.post(f"/employees/{staff[0]['id']}", data={
        "_csrf": token, "name": staff[0]["name"], "code": staff[0]["code"], "joined_on": staff[0]["joined_on"],
        "status": "active", "self_punch_mode": "geofence_only"})
    assert employees_service.get_employee(storage, staff[0]["id"])["self_punch_mode"] == "geofence_only"


# ------------------------------------------------------------ Feature 4
def _at(hh, mm, day=date(2026, 9, 7)):
    return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)


@pytest.mark.parametrize("hhmm, late, fine, half", [
    ((9, 40), 0, 0, False),      # inside grace (09:30 + 15)
    ((9, 55), 10, 50, False),    # slab 1: up to 15 min past grace
    ((10, 0), 15, 50, False),    # slab 1 edge
    ((10, 10), 25, 100, False),  # slab 2
    ((10, 29), 44, 100, False),  # still slab 2
    ((10, 30), 45, 0, True),     # 60 min after shift start → half day, no fine
])
def test_f4_slabs_and_half_day(storage: Storage, hhmm, late, fine, half):
    assessed = orgtime.late_assessment(storage, _at(*hhmm))
    assert (assessed["late_minutes"], assessed["fine"], assessed["half_day"]) == (late, fine, half)


def test_f4_policy_none_and_count_only(storage: Storage):
    storage.set_setting("late_policy_mode", "none")
    assert orgtime.late_assessment(storage, _at(11, 0)) | {} == {"late_minutes": 75, "fine": 0.0,
                                                                 "half_day": False, "policy": "none"}
    storage.set_setting("late_policy_mode", "lop_count_only")
    assessed = orgtime.late_assessment(storage, _at(10, 5))
    assert assessed["fine"] == 0 and assessed["half_day"] is False
    assert orgtime.late_marks_rule(storage) == 3
    storage.set_setting("late_policy_mode", "tiered_fine_only")
    assert orgtime.late_marks_rule(storage) == 0


def test_f4_gate_scan_records_fine_and_half_day(storage: Storage, settings, staff):
    day = date(2026, 9, 7)
    fine = attendance_service.mark_by_face(storage, settings, staff[0], moment=_at(9, 55, day))
    assert fine.late_fine == 50 and attendance_service.get_row(storage, staff[0]["id"], day)["late_fine"] == 50
    half = attendance_service.mark_by_face(storage, settings, staff[1], moment=_at(10, 45, day))
    row = attendance_service.get_row(storage, staff[1]["id"], day)
    assert half.late_half_day and row["status"] == "half_day" and row["late_fine"] == 0


def test_f4_manual_entry_follows_the_policy(storage: Storage, staff):
    row = attendance_service.mark_manual(storage, employee_id=staff[0]["id"], work_date="2026-09-08",
                                         status="present", check_in="10:10", check_out="18:30")
    assert row["late_fine"] == 100 and row["status"] == "present"
    row = attendance_service.mark_manual(storage, employee_id=staff[0]["id"], work_date="2026-09-09",
                                         status="present", check_in="11:00", check_out="18:30")
    assert row["status"] == "half_day" and row["late_fine"] == 0


def test_f4_payslip_deducts_fines(storage: Storage, settings, staff):
    e = staff[0]
    for d in [d for d in orgtime.days_in_period("2026-09") if orgtime.day_kind(storage, d) == "working"]:
        attendance_service.mark_manual(storage, employee_id=e["id"], work_date=d.isoformat(), status="present",
                                       check_in="09:30", check_out="18:30")
    attendance_service.mark_manual(storage, employee_id=e["id"], work_date="2026-09-08", status="present",
                                   check_in="09:55", check_out="18:30")    # ₹50
    attendance_service.mark_manual(storage, employee_id=e["id"], work_date="2026-09-09", status="present",
                                   check_in="10:20", check_out="18:30")    # ₹100
    calc = payroll.compute_slip(storage, settings, employees_service.get_employee(storage, e["id"]), "2026-09")
    assert calc["late_fine"] == 150 and calc["lop_days"] == 0
    payroll.generate_run(storage, settings, "2026-09")
    slip = next(s for s in payroll.list_slips(storage, "2026-09") if s["employee_id"] == e["id"])
    assert slip["late_fine"] == 150
    assert slip["total_deductions"] == pytest.approx(slip["pf"] + slip["esi"] + slip["professional_tax"] + 150)
    assert "Late Fines" in payroll.payroll_csv(storage, "2026-09").splitlines()[0]
    # an HR adjustment keeps the fine in the total
    updated = payroll.update_slip(storage, slip["id"], bonus=500)
    assert updated["total_deductions"] == pytest.approx(slip["total_deductions"])
