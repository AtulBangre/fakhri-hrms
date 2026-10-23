"""Attendance regularization, exit / F&F settlement, appraisals and the UI fixes around them."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Storage
from app.services import appraisal, attendance as attendance_service, employees as employees_service
from app.services import exits, orgtime, payroll, regularization
from tests.helpers import csrf_of


def _last_working_day(storage, settings, back: int = 2) -> date:
    day = orgtime.today(settings) - timedelta(days=back)
    while orgtime.day_kind(storage, day) != "working":
        day -= timedelta(days=1)
    return day


# ----------------------------------------------------------- regularization
def test_employee_requests_and_hr_approves_a_correction(
    ess_client: TestClient, storage: Storage, settings, ess_credentials, hr_user
):
    employee_id = ess_credentials["employee"]["id"]
    day = _last_working_day(storage, settings)
    token = csrf_of(ess_client, "/me/attendance")
    response = ess_client.post("/me/attendance/regularize", data={
        "_csrf": token, "work_date": day.isoformat(), "requested_status": "present",
        "check_in": "09:40", "check_out": "18:35", "reason": "Scanner did not detect me",
    }, follow_redirects=False)
    assert "ok=" in response.headers["location"], response.headers["location"]
    assert "correction pending" in ess_client.get(f"/me/attendance?period={day.isoformat()[:7]}").text

    request_id = regularization.list_requests(storage, employee_id=employee_id)[0]["id"]
    regularization.approve(storage, request_id, actor="hr")
    row = attendance_service.get_row(storage, employee_id, day)
    assert row["status"] == "present" and row["check_in"] == "09:40:00" and row["worked_minutes"] == 535
    assert regularization.get(storage, request_id)["status"] == "approved"


def test_regularization_validation(storage: Storage, settings, aarav):
    future = orgtime.today(settings) + timedelta(days=1)
    with pytest.raises(regularization.ValidationError):
        regularization.submit(storage, settings, employee_id=aarav["id"], work_date=future.isoformat(),
                              check_in="09:30", check_out="18:30", reason="tomorrow please")
    day = _last_working_day(storage, settings)
    with pytest.raises(regularization.ValidationError):  # out before in
        regularization.submit(storage, settings, employee_id=aarav["id"], work_date=day.isoformat(),
                              check_in="18:00", check_out="09:00", reason="wrong way round")
    regularization.submit(storage, settings, employee_id=aarav["id"], work_date=day.isoformat(),
                          check_in="09:30", check_out=None, reason="forgot to check out")
    with pytest.raises(regularization.ValidationError):  # duplicate pending
        regularization.submit(storage, settings, employee_id=aarav["id"], work_date=day.isoformat(),
                              check_in="09:30", check_out=None, reason="again please")


def test_finalised_payroll_blocks_regularization(storage: Storage, settings, aarav):
    day = _last_working_day(storage, settings)
    request = regularization.submit(storage, settings, employee_id=aarav["id"], work_date=day.isoformat(),
                                    check_in="09:30", check_out="18:30", reason="missed the punch")
    payroll.generate_run(storage, settings, day.isoformat()[:7])
    payroll.set_run_status(storage, day.isoformat()[:7], "finalized")
    with pytest.raises(regularization.ValidationError, match="Reopen"):
        regularization.approve(storage, request["id"], actor="hr")


def test_bulk_approve_and_withdraw(hr_client: TestClient, storage: Storage, settings, staff):
    day = _last_working_day(storage, settings)
    ids = [
        regularization.submit(storage, settings, employee_id=person["id"], work_date=day.isoformat(),
                              check_in="09:30", check_out="18:30", reason="gate scanner was down")["id"]
        for person in staff[:2]
    ]
    third = regularization.submit(storage, settings, employee_id=staff[2]["id"], work_date=day.isoformat(),
                                  check_in="09:30", check_out="18:30", reason="gate scanner was down")
    assert hr_client.get("/attendance/regularizations").status_code == 200
    token = csrf_of(hr_client, "/attendance/regularizations")
    response = hr_client.post("/attendance/regularizations/bulk",
                              data={"_csrf": token, "action": "approve", "ids": ids}, follow_redirects=False)
    assert "2+request" in response.headers["location"]
    assert all(regularization.get(storage, i)["status"] == "approved" for i in ids)
    regularization.withdraw(storage, third["id"], employee_id=staff[2]["id"])
    assert regularization.get(storage, third["id"])["status"] == "withdrawn"
    with pytest.raises(regularization.ValidationError):
        regularization.withdraw(storage, ids[0], employee_id=staff[1]["id"])  # not theirs


# --------------------------------------------------------------------- exits
def test_full_exit_flow_settles_and_deactivates(
    ess_client: TestClient, storage: Storage, settings, ess_credentials, hr_user
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/exit")
    lwd = orgtime.today(settings) + timedelta(days=10)
    ess_client.post("/me/exit", data={"_csrf": token, "reason": "Moving cities", "proposed_last_day": lwd.isoformat()})
    row = exits.open_exit_for(storage, employee_id)
    assert row and row["status"] == "requested"

    exits.approve(storage, row["id"], last_working_day=lwd.isoformat(), notice_days=30, actor="hr")
    statement = exits.compute_fnf(storage, settings, exits.get(storage, row["id"]))
    labels = {line["label"] for line in statement["deductions"]}
    assert "Notice period shortfall" in labels  # 10 of 30 days served
    assert any(line["label"] == "Leave encashment" for line in statement["earnings"])

    with pytest.raises(exits.ValidationError, match="checklist"):
        exits.settle(storage, settings, row["id"], actor="hr")
    for item in exits.checklist(storage, row["id"]):
        exits.toggle_item(storage, item["id"], done=True, actor="hr")
    settled = exits.settle(storage, settings, row["id"], actor="hr")
    assert settled["status"] == "settled" and settled["fnf_net"] == statement["net"]
    assert json.loads(settled["fnf_snapshot"])["net"] == statement["net"]
    assert employees_service.get_employee(storage, employee_id)["status"] == "inactive"
    assert auth.get_user_for_employee(storage, employee_id)["active"] == 0


def test_fnf_gratuity_waiver_and_assets(storage: Storage, settings):
    employee_id = employees_service.create_employee(storage, {
        "name": "Long Server", "joined_on": "2019-01-01", "monthly_gross": 60000, "status": "active",
    })
    employees_service.set_salary(storage, employee_id, monthly_gross=60000, basic=30000,
                                 effective_from="2019-01-01")
    storage.execute(
        "INSERT INTO assets (tag, name, category, cost, status, created_at) VALUES ('LAP-9','Laptop','laptop',45000,'assigned','x')"
    )
    asset_id = storage.scalar("SELECT id FROM assets WHERE tag = 'LAP-9'")
    storage.execute("INSERT INTO asset_assignments (asset_id, employee_id, assigned_on) VALUES (?,?,?)",
                    (asset_id, employee_id, "2024-01-01"))
    today = orgtime.today(settings)
    row = exits.record_exit(storage, settings, employee_id=employee_id, exit_type="resignation",
                            resignation_date=today.isoformat(), last_working_day=today.isoformat(),
                            notice_days=30, notice_waived=True, actor="hr")
    fnf = exits.compute_fnf(storage, settings, row)
    gratuity = next(line for line in fnf["earnings"] if line["label"] == "Gratuity")
    years = fnf["service_days"] // 365 + (1 if fnf["service_days"] % 365 > 182 else 0)
    assert gratuity["amount"] == pytest.approx(15 / 26 * 30000 * years, rel=1e-3)
    assert not any(line["label"] == "Notice period shortfall" for line in fnf["deductions"])
    assert any(line["label"] == "Unreturned assets" and line["amount"] == 45000 for line in fnf["deductions"])

    exits.update_settlement(storage, row["id"], extra_earnings=0, extra_deductions=0, recover_assets=False,
                            note=None, actor="hr")
    fnf2 = exits.compute_fnf(storage, settings, exits.get(storage, row["id"]))
    assert fnf2["net"] == pytest.approx(fnf["net"] + 45000)


def test_exit_pages_render(hr_client: TestClient, storage: Storage, settings, staff):
    today = orgtime.today(settings)
    row = exits.record_exit(storage, settings, employee_id=staff[0]["id"], exit_type="termination",
                            resignation_date=today.isoformat(), last_working_day=today.isoformat(), actor="hr")
    for path in ("/exits", "/exits?status=all", f"/exits/{row['id']}", f"/exits/{row['id']}/statement"):
        response = hr_client.get(path)
        assert response.status_code == 200, path
    item = exits.checklist(storage, row["id"])[0]
    token = csrf_of(hr_client, f"/exits/{row['id']}")
    hr_client.post(f"/exit-checklist/{item['id']}", data={"_csrf": token, "done": "1"})
    assert exits.checklist(storage, row["id"])[0]["done"] == 1


# ----------------------------------------------------------------- appraisals
def test_appraisal_cycle_end_to_end(ess_client: TestClient, storage: Storage, ess_credentials, hr_user):
    cycle = appraisal.create_cycle(storage, name="FY review", period_start="2025-04-01",
                                   period_end="2026-03-31", goals_template="Quality | 60\nTeamwork | 40")
    assert appraisal.launch_cycle(storage, cycle["id"], actor="hr") >= 1
    mine = appraisal.for_employee(storage, ess_credentials["employee"]["id"])[0]
    goals = {g["title"]: g["id"] for g in mine["goals"]}

    assert ess_client.get("/me/appraisal").status_code == 200
    token = csrf_of(ess_client, "/me/appraisal")
    ess_client.post(f"/me/appraisal/{mine['id']}", data={
        "_csrf": token, f"score_{goals['Quality']}": "5", f"score_{goals['Teamwork']}": "3",
        "comment": "Good year", "submit": "1",
    })
    after = appraisal.get_appraisal(storage, mine["id"])
    assert after["status"] == "manager_review" and after["self_score"] == pytest.approx(4.2)

    with pytest.raises(appraisal.ValidationError):
        appraisal.close_cycle(storage, cycle["id"])
    done = appraisal.review(storage, mine["id"], scores={goals["Quality"]: 4, goals["Teamwork"]: 4},
                            notes={}, comment="Solid", increment_pct=8, complete=True, actor="hr")
    assert done["status"] == "completed" and done["final_rating"] == 4.0 and done["increment_pct"] == 8
    others = [a for a in appraisal.cycle_appraisals(storage, cycle["id"]) if a["status"] != "completed"]
    for item in others:
        full = appraisal.get_appraisal(storage, item["id"])
        appraisal.review(storage, item["id"], scores={g["id"]: 3 for g in full["goals"]}, notes={},
                         comment=None, complete=True, actor="hr")
    appraisal.close_cycle(storage, cycle["id"])
    assert appraisal.get_cycle(storage, cycle["id"])["status"] == "closed"


def test_self_review_needs_every_score(storage: Storage, aarav):
    cycle = appraisal.create_cycle(storage, name="Q", period_start="2026-01-01", period_end="2026-03-31",
                                   goals_template="A\nB")
    appraisal.launch_cycle(storage, cycle["id"])
    item = appraisal.for_employee(storage, aarav["id"])[0]
    with pytest.raises(appraisal.ValidationError, match="Score every goal"):
        appraisal.submit_self(storage, item["id"], employee_id=aarav["id"],
                              scores={item["goals"][0]["id"]: 4}, notes={}, comment=None, submit=True)
    with pytest.raises(appraisal.ValidationError):
        appraisal.submit_self(storage, item["id"], employee_id=aarav["id"] + 99, scores={}, notes={},
                              comment=None, submit=False)


def test_appraisal_pages_render(hr_client: TestClient, storage: Storage, staff):
    cycle = appraisal.create_cycle(storage, name="H1", period_start="2026-04-01", period_end="2026-09-30",
                                   goals_template="Delivery | 50\nQuality | 50")
    assert hr_client.get(f"/appraisals/cycles/{cycle['id']}").status_code == 200
    appraisal.launch_cycle(storage, cycle["id"])
    item = appraisal.cycle_appraisals(storage, cycle["id"])[0]
    for path in ("/appraisals", f"/appraisals/cycles/{cycle['id']}", f"/appraisals/{item['id']}"):
        assert hr_client.get(path).status_code == 200, path


# ------------------------------------------------------------------- UI fixes
def test_register_shows_status_codes(hr_client: TestClient, storage: Storage, settings, aarav):
    day = _last_working_day(storage, settings)
    attendance_service.mark_manual(storage, employee_id=aarav["id"], work_date=day.isoformat(),
                                   status="present", check_in="09:30", check_out="18:30")
    page = hr_client.get(f"/attendance/register?period={day.isoformat()[:7]}").text
    assert 'class="cell P' in page


def test_settings_form_saves_everything_at_once(hr_client: TestClient, storage: Storage):
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post("/settings/values", data={
        "_csrf": token, "shift_start": "10:00", "week_off_days__present": "1", "week_off_days": ["5", "6"],
        "notice_period_days": "45",
    }, follow_redirects=False)
    assert "Saved" in response.headers["location"]
    assert storage.get_setting("shift_start") == "10:00"
    assert storage.get_setting("week_off_days") == "5,6"
    assert storage.get_setting("notice_period_days") == "45"


def test_sidebar_shows_pending_badges(hr_client: TestClient, storage: Storage, settings, aarav):
    regularization.submit(storage, settings, employee_id=aarav["id"],
                          work_date=_last_working_day(storage, settings).isoformat(),
                          check_in="09:30", check_out="18:30", reason="scanner missed me")
    page = hr_client.get("/dashboard").text
    assert '<em class="badge-count">1</em>' in page
    assert "Attendance corrections" in page


def test_number_filter_keeps_half_days():
    from app.deps import number, plain
    assert number(0.5) == "0.5" and number(1234.5) == "1,234.5" and number(3) == "3"
    assert plain(2070.0) == "2070" and plain(12.5) == "12.5"
