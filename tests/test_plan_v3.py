"""Regression tests for the bugs in plan.md (v3). Each test is named after its bug ID."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Storage
from app.services import account
from tests.helpers import csrf_of


# ------------------------------------------------------------------ security
def _portal_client(client: TestClient, storage: Storage, employee: dict, password: str = "portal-pass-1"):
    user, _ = auth.ensure_employee_login(storage, employee, password=password)
    response = client.post("/login", data={"username": user["username"], "password": password},
                           follow_redirects=False)
    assert response.status_code == 303
    return user


def test_sec01_disabled_login_is_signed_out(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    assert client.get("/me", follow_redirects=False).status_code == 200
    auth.set_user_active(storage, user["id"], False)
    response = client.get("/me", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_sec01_deleted_login_is_signed_out(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    auth.delete_user(storage, user["id"])
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_sec02_password_change_ends_sessions(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    auth.change_password(storage, user["id"], "a-new-password-1")
    assert client.get("/me", follow_redirects=False).status_code == 303
    # signing in again with the new password works
    client.post("/login", data={"username": user["username"], "password": "a-new-password-1"})
    assert client.get("/me", follow_redirects=False).status_code == 200


def test_sec02_sign_out_everywhere(hr_client: TestClient, storage: Storage, kiosk_user):
    before = auth.get_user(storage, kiosk_user["id"])["session_version"]
    token = csrf_of(hr_client, "/settings")
    hr_client.post(f"/settings/users/{kiosk_user['id']}/signout", data={"_csrf": token})
    assert auth.get_user(storage, kiosk_user["id"])["session_version"] == before + 1


def test_sec03_reset_does_not_reenable_disabled_login(storage: Storage, settings, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    code = account.issue_otp(storage, settings, user)
    auth.set_user_active(storage, user["id"], False)
    ok, _ = account.complete_reset(storage, settings, username=user["username"], code=code,
                                   new_password="brand-new-pass-1")
    assert not ok
    assert auth.get_user(storage, user["id"])["active"] == 0


# ------------------------------------------------------------------- payroll
from app.services import attendance as attendance_service  # noqa: E402
from app.services import employees as employees_service  # noqa: E402
from app.services import orgtime, payroll, tax  # noqa: E402


def _next_period(settings) -> str:
    first = orgtime.today(settings).replace(day=1)
    return orgtime.period_of(first + timedelta(days=40))


def test_pay04_future_month_is_refused(storage: Storage, settings, staff):
    with pytest.raises(employees_service.ValidationError, match="has not started"):
        payroll.generate_run(storage, settings, _next_period(settings))


def test_pay04_current_month_cannot_be_finalised_early(storage: Storage, settings, staff):
    current = orgtime.period_of(orgtime.today(settings))
    payroll.generate_run(storage, settings, current)  # a provisional draft is fine
    if orgtime.today(settings) < orgtime.month_bounds(current)[1]:
        with pytest.raises(employees_service.ValidationError, match="month"):
            payroll.set_run_status(storage, current, "finalized")


def test_pay07_only_draft_runs_can_be_deleted(hr_client: TestClient, storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    payroll.set_run_status(storage, "2026-01", "finalized")
    with pytest.raises(employees_service.ValidationError):
        payroll.delete_run(storage, "2026-01")
    payroll.set_run_status(storage, "2026-01", "paid")
    token = csrf_of(hr_client, "/payroll")
    response = hr_client.post("/payroll/delete", data={"_csrf": token, "period": "2026-01"}, follow_redirects=False)
    assert "err=" in response.headers["location"]
    assert payroll.get_run(storage, "2026-01") is not None


def test_pay06_recalculation_keeps_adjustments(storage: Storage, settings, aarav):
    payroll.generate_run(storage, settings, "2026-01")
    slip = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == aarav["id"])
    payroll.update_slip(storage, slip["id"], bonus=5000, advance=1000, other_deduction=200, tds=700,
                        note="Diwali bonus")
    payroll.generate_run(storage, settings, "2026-01")
    again = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == aarav["id"])
    assert (again["bonus"], again["advance"], again["other_deduction"], again["tds"], again["note"]) == \
        (5000, 1000, 200, 700, "Diwali bonus")
    assert again["net_pay"] == pytest.approx(again["earned_gross"] + 5000 - again["total_deductions"]
                                             + again["rounding"])
    # an explicit reset starts clean
    payroll.generate_run(storage, settings, "2026-01", reset_adjustments=True)
    clean = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == aarav["id"])
    assert clean["bonus"] == 0 and clean["note"] is None


def test_pay05_tds_import_handles_indian_amounts(storage: Storage, settings, aarav):
    code = aarav["code"]
    preview = tax.parse_tds_schedule(storage, f'code,annual_tds\n{code},"1,50,000"\n')
    assert not preview["errors"] and preview["rows"][0]["annual_tds"] == 150000
    result = tax.import_tds_schedule(storage, settings, csv_text=f'{code},"1,50,000"', fy="2025-26")
    assert result["applied"] == 1
    assert tax.get_profile(storage, aarav["id"], "2025-26")["confirmed_tds"] == 150000


def test_pay05_ambiguous_commas_apply_nothing(storage: Storage, settings, staff):
    text = f"{staff[0]['code']},48000\n{staff[1]['code']},1,50,000\n"
    result = tax.import_tds_schedule(storage, settings, csv_text=text, fy="2025-26")
    assert result["applied"] == 0 and result["errors"]
    assert tax.get_profile(storage, staff[0]["id"], "2025-26") == {}


# ------------------------------------------------- day classification (step 3)
from app.services import leave as leave_service  # noqa: E402
from app.services import exits as exits_service  # noqa: E402

SEPT = "2026-09"


def _working_days(storage, period):
    return [d for d in orgtime.days_in_period(period) if orgtime.day_kind(storage, d) == "working"]


def _fill(storage, employee_id, days):
    for d in days:
        attendance_service.mark_manual(storage, employee_id=employee_id, work_date=d.isoformat(),
                                       status="present", check_in="09:30", check_out="18:30")


def _employee(storage, name, joined="2025-01-01", gross=30000):
    return employees_service.get_employee(storage, employees_service.create_employee(
        storage, {"name": name, "joined_on": joined, "monthly_gross": gross}))


def test_pay01_half_day_without_leave_costs_half_a_day(storage: Storage, settings):
    e = _employee(storage, "Half Worker")
    days = _working_days(storage, SEPT)
    _fill(storage, e["id"], days[1:])
    attendance_service.mark_manual(storage, employee_id=e["id"], work_date=days[0].isoformat(),
                                   status="half_day", check_in="09:30", check_out="12:00")
    calc = payroll.compute_slip(storage, settings, e, SEPT)
    assert calc["lop_days"] == 0.5
    assert calc["earned_gross"] == pytest.approx(30000 - 0.5 * 1000)


def test_pay01_paid_half_day_leave_costs_nothing(storage: Storage, settings):
    e = _employee(storage, "Paid Half")
    days = _working_days(storage, SEPT)
    _fill(storage, e["id"], days[1:])
    leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="paid",
                              from_date=days[0].isoformat(), half_day=True)
    assert payroll.compute_slip(storage, settings, e, SEPT)["lop_days"] == 0


def test_pay02_unpaid_half_day_leave_costs_half_a_day(storage: Storage, settings):
    e = _employee(storage, "Unpaid Half")
    days = _working_days(storage, SEPT)
    _fill(storage, e["id"], days[1:])
    leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="unpaid",
                              from_date=days[0].isoformat(), half_day=True)
    assert payroll.compute_slip(storage, settings, e, SEPT)["lop_days"] == 0.5


def test_pay03_joiner_is_paid_from_the_joining_date(storage: Storage, settings):
    e = _employee(storage, "Joiner", joined="2026-09-20")
    _fill(storage, e["id"], [d for d in _working_days(storage, SEPT) if d.day >= 20])
    calc = payroll.compute_slip(storage, settings, e, SEPT)
    assert calc["earned_gross"] == pytest.approx(30000 * 11 / 30)
    assert calc["lop_days"] == 0 and calc["not_employed_days"] == 19


def test_pay03_leaver_is_paid_up_to_the_last_working_day(storage: Storage, settings):
    e = _employee(storage, "Leaver")
    _fill(storage, e["id"], [d for d in _working_days(storage, SEPT) if d.day <= 10])
    exits_service.record_exit(storage, settings, employee_id=e["id"], exit_type="resignation",
                              resignation_date="2026-08-10", last_working_day="2026-09-10", notice_days=30)
    calc = payroll.compute_slip(storage, settings, e, SEPT)
    assert calc["earned_gross"] == pytest.approx(30000 * 10 / 30)


def test_att03_today_is_not_absent_yet(storage: Storage, settings, aarav):
    today = orgtime.today(settings)
    summary = attendance_service.employee_month_summary(storage, settings, aarav["id"], orgtime.period_of(today))
    if orgtime.day_kind(storage, today) == "working":
        assert summary["pending"] == 1
    register = attendance_service.monthly_register(storage, settings, orgtime.period_of(today))
    row = next(r for r in register["rows"] if r["employee"]["id"] == aarav["id"])
    cell = row["cells"][today.day - 1]
    assert cell["code"] != "A"


def test_att03_register_matches_payslips(storage: Storage, settings):
    joiner = _employee(storage, "Reg Joiner", joined="2026-09-15")
    regular = _employee(storage, "Reg Regular")
    days = _working_days(storage, SEPT)
    _fill(storage, regular["id"], days[2:])
    attendance_service.mark_manual(storage, employee_id=regular["id"], work_date=days[0].isoformat(),
                                   status="half_day")
    _fill(storage, joiner["id"], [d for d in days if d.day >= 15])
    register = attendance_service.monthly_register(storage, settings, SEPT)
    for row in register["rows"]:
        calc = payroll.compute_slip(storage, settings, row["employee"], SEPT)
        assert row["totals"]["lop_days"] == calc["lop_days"], row["employee"]["name"]
        assert row["totals"]["payable_days"] == calc["present_days"] + calc["paid_leave_days"]
    joiner_row = next(r for r in register["rows"] if r["employee"]["id"] == joiner["id"])
    assert joiner_row["cells"][0]["code"] == "–" and joiner_row["totals"]["absent"] == 0


# ---------------------------------------------------------------- leave (step 4)
def test_lv01_paid_leave_cannot_exceed_balance(storage: Storage, settings):
    e = _employee(storage, "Short Service", joined="2026-09-01")
    with pytest.raises(employees_service.ValidationError, match="balance"):
        leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="paid",
                                  from_date="2026-10-05", to_date="2026-10-16")
    # pending ESS requests count against what is left
    leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="paid",
                              from_date="2026-10-05", status="pending")
    available = leave_service.balance(storage, settings, e["id"])["available"]
    with pytest.raises(employees_service.ValidationError):
        leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="sick",
                                  from_date="2026-10-06", to_date="2026-10-16", status="pending")
    assert available >= 0
    # HR may override explicitly; unpaid leave is never limited
    leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="paid",
                              from_date="2026-10-19", to_date="2026-10-23", allow_negative=True)
    leave_service.apply_leave(storage, settings, employee_id=e["id"], leave_type="unpaid",
                              from_date="2026-09-21", to_date="2026-09-25")


def test_lv02_overlapping_leave_is_refused(storage: Storage, settings, aarav):
    leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                              from_date="2026-09-07", to_date="2026-09-09")
    with pytest.raises(employees_service.ValidationError, match="already"):
        leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="casual",
                                  from_date="2026-09-09", status="pending")


def test_lv03_cancel_only_removes_its_own_days(storage: Storage, settings, aarav):
    first = leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                      from_date="2026-09-07")["leave"]
    # force ids so one is a numeric prefix of the other (#N and #N0)
    storage.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'leave_requests'", (first["id"] * 10 - 1,))
    second = leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                       from_date="2026-09-08")["leave"]
    assert str(second["id"]).startswith(str(first["id"]))
    assert attendance_service.get_row(storage, aarav["id"], "2026-09-08")["leave_id"] == second["id"]
    # even a day that sits inside the first leave's range but belongs to the second survives
    storage.execute("UPDATE leave_requests SET to_date = '2026-09-08' WHERE id = ?", (first["id"],))
    leave_service.cancel_leave(storage, first["id"])
    assert attendance_service.get_row(storage, aarav["id"], "2026-09-07") is None
    assert attendance_service.get_row(storage, aarav["id"], "2026-09-08")["status"] == "leave"


def test_lv04_worked_days_are_not_charged(storage: Storage, settings, aarav):
    _fill(storage, aarav["id"], [date(2026, 9, 8)])
    result = leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                       from_date="2026-09-07", to_date="2026-09-09")
    assert result["leave"]["days"] == 2 and len(result["skipped"]) == 1
    _fill(storage, aarav["id"], [date(2026, 9, 14)])
    with pytest.raises(employees_service.ValidationError, match="no day"):
        leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                  from_date="2026-09-14")


def test_lv05_half_day_leave_survives_gate_scans(storage: Storage, settings, aarav):
    today = orgtime.today(settings)
    if orgtime.day_kind(storage, today) != "working":
        pytest.skip("needs a working day")
    leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                              from_date=today.isoformat(), half_day=True)
    attendance_service.mark_by_face(storage, settings, aarav, moment=datetime.combine(today, datetime.min.time()).replace(hour=14))
    attendance_service.mark_by_face(storage, settings, aarav, moment=datetime.combine(today, datetime.min.time()).replace(hour=18))
    row = attendance_service.get_row(storage, aarav["id"], today)
    assert row["status"] == "half_day" and row["leave_type"] == "paid" and row["check_in"] and row["check_out"]


def test_lv05_half_day_leave_after_a_morning_scan(storage: Storage, settings, aarav):
    _fill(storage, aarav["id"], [date(2026, 9, 8)])
    result = leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                       from_date="2026-09-08", half_day=True)
    row = attendance_service.get_row(storage, aarav["id"], "2026-09-08")
    assert result["applied"] and row["status"] == "half_day" and row["check_in"] == "09:30:00"
    leave_service.cancel_leave(storage, result["leave"]["id"])
    row = attendance_service.get_row(storage, aarav["id"], "2026-09-08")
    assert row["status"] == "present" and row["leave_type"] is None  # the worked day is kept


def test_lv06_half_day_on_a_week_off_is_refused(storage: Storage, settings, aarav):
    sunday = next(d for d in orgtime.days_in_period(SEPT) if orgtime.day_kind(storage, d) == "week_off")
    with pytest.raises(employees_service.ValidationError):
        leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                  from_date=sunday.isoformat(), half_day=True)


# ---------------------------------------------------------- payroll (step 5)
from app.services import records  # noqa: E402

AUG = "2026-08"


def _finalise(storage, settings, period):
    payroll.generate_run(storage, settings, period)
    payroll.set_run_status(storage, period, "finalized")


def test_pay10_finalised_month_is_locked(hr_client: TestClient, storage: Storage, settings, aarav):
    _finalise(storage, settings, AUG)
    blocked = (
        lambda: attendance_service.mark_manual(storage, employee_id=aarav["id"], work_date="2026-08-05", status="absent"),
        lambda: attendance_service.clear_day(storage, employee_id=aarav["id"], work_date="2026-08-05"),
        lambda: leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="unpaid",
                                          from_date="2026-08-06"),
        lambda: leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="unpaid",
                                          from_date="2026-07-30", to_date="2026-08-03", status="pending"),
    )
    for action in blocked:
        with pytest.raises(employees_service.ValidationError, match="finalised"):
            action()
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post("/attendance/holidays", data={"_csrf": token, "day": "2026-08-15", "name": "I-Day"},
                              follow_redirects=False)
    assert "err=" in response.headers["location"]
    # reopening unlocks it again
    payroll.reopen_run(storage, AUG)
    attendance_service.mark_manual(storage, employee_id=aarav["id"], work_date="2026-08-05", status="absent")


def test_pay11_employees_only_see_final_payslips(ess_client: TestClient, storage: Storage, settings, ess_credentials):
    employee_id = ess_credentials["employee"]["id"]
    payroll.generate_run(storage, settings, AUG)
    slip = next(s for s in payroll.list_slips(storage, AUG) if s["employee_id"] == employee_id)
    assert f"/me/payslips/{slip['id']}" not in ess_client.get("/me/payslips").text
    assert ess_client.get(f"/me/payslips/{slip['id']}", follow_redirects=False).status_code == 303
    assert ess_client.get(f"/me/payslips/{slip['id']}.pdf", follow_redirects=False).status_code == 303
    payroll.set_run_status(storage, AUG, "finalized")
    assert f"/me/payslips/{slip['id']}" in ess_client.get("/me/payslips").text


def test_pay14_approved_expenses_are_reimbursed_through_payroll(storage: Storage, settings, aarav):
    claim = records.submit_claim(storage, settings, employee_id=aarav["id"], amount=1250,
                                 expense_date="2026-08-10", category="travel")
    records.decide_claim(storage, claim_id=claim["id"], approve=True, decided_by="hr")
    payroll.generate_run(storage, settings, AUG)
    slip = next(s for s in payroll.list_slips(storage, AUG) if s["employee_id"] == aarav["id"])
    assert slip["reimbursement"] == 1250
    assert slip["net_pay"] == pytest.approx(slip["earned_gross"] + slip["bonus"] + 1250 - slip["total_deductions"]
                                            + slip["rounding"])
    payroll.set_run_status(storage, AUG, "finalized")
    assert records.get_claim(storage, claim["id"])["status"] == "paid"


def test_pay14_claims_can_be_marked_paid_by_hand(storage: Storage, settings, aarav):
    claim = records.submit_claim(storage, settings, employee_id=aarav["id"], amount=500,
                                 expense_date="2026-08-10", category="food")
    records.decide_claim(storage, claim_id=claim["id"], approve=True, decided_by="hr")
    records.mark_claim_paid(storage, claim_id=claim["id"], actor="hr")
    assert records.get_claim(storage, claim["id"])["status"] == "paid"


def test_pay08_pf_and_esi_follow_earned_wages(storage: Storage, settings):
    e = _employee(storage, "Statutory", gross=18000)
    employees_service.set_salary(storage, e["id"], monthly_gross=18000, basic=9000, pf=1080, esi=135,
                                 effective_from="2025-01-01")
    days = _working_days(storage, SEPT)
    _fill(storage, e["id"], days[10:])            # 10 working days absent
    calc = payroll.compute_slip(storage, settings, e, SEPT)
    share = calc["earned_gross"] / 18000
    assert share < 1
    assert calc["pf"] == pytest.approx(1080 * share, abs=0.01)
    assert calc["esi"] == pytest.approx(135 * share, abs=0.01)


def test_pay09_net_pay_never_negative(storage: Storage, settings, aarav):
    payroll.generate_run(storage, settings, AUG)
    slip = next(s for s in payroll.list_slips(storage, AUG) if s["employee_id"] == aarav["id"])
    with pytest.raises(employees_service.ValidationError, match="exceed"):
        payroll.update_slip(storage, slip["id"], advance=slip["earned_gross"] + 10_000)
    assert float(slip["net_pay"]).is_integer()  # rounded to the rupee


# ------------------------------------------------------------ workflows (step 6)
from app.services import appraisal  # noqa: E402,F401
from tests.conftest import HR_PASSWORD, HR_USERNAME  # noqa: E402


def test_sec04_login_lockout(client: TestClient, storage: Storage, hr_user):
    codes = [client.post("/login", data={"username": HR_USERNAME, "password": f"wrong{i}"}).status_code
             for i in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # even the right password is refused while locked
    locked = client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    assert locked.status_code == 429


def test_sec05_inactive_employee_loses_portal_login(client: TestClient, storage: Storage, staff, hr_user):
    user = _portal_client(client, storage, staff[0])
    employees_service.set_status(storage, staff[0]["id"], "inactive")
    assert auth.get_user(storage, user["id"])["active"] == 0
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_emp01_code_change_keeps_face_recognition(app, client: TestClient, storage: Storage, staff,
                                                  hr_user, kiosk_user):
    from app.face import IdentificationResult
    from tests.conftest import FakeFaceService, KIOSK_PASSWORD, KIOSK_USERNAME
    fake = FakeFaceService()
    app.state.face = fake
    employees_service.enrol_face(storage, fake, staff[0]["id"], b"jpeg-bytes")
    external_id = next(iter(fake.enrolled))
    employees_service.update_employee(storage, staff[0]["id"], {"code": "NEWCODE1"})
    fake.identify_result = IdentificationResult(
        matched=True, external_id=external_id, similarity=0.9, face_id=fake.enrolled[external_id],
        threshold=0.36, faces_in_probe=1, gallery_size=1, message="ok")
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    body = client.post("/api/kiosk/scan", files={"image": ("f.jpg", b"jpeg", "image/jpeg")}).json()
    assert body["ok"] and body["action"] == "check_in", body


def test_emp01_legacy_template_blocks_code_change(storage: Storage, staff):
    storage.execute("UPDATE employees SET face_id = 'legacy-face', face_key = NULL WHERE id = ?", (staff[0]["id"],))
    with pytest.raises(employees_service.ValidationError, match="face"):
        employees_service.update_employee(storage, staff[0]["id"], {"code": "OTHER9"})


def test_emp02_deleting_a_login_with_history_works(storage: Storage, settings, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    leave_service.apply_leave(storage, settings, employee_id=staff[0]["id"], leave_type="unpaid",
                              from_date="2026-11-02", status="pending", requested_by=user["id"])
    auth.delete_user(storage, user["id"])
    assert auth.get_user(storage, user["id"]) is None
    assert storage.query_one("SELECT applied_by FROM leave_requests")["applied_by"] is None


def test_emp03_deleting_an_employee_removes_files(storage: Storage, settings, staff):
    doc = records.add_document(storage, settings, employee_id=staff[0]["id"], data=b"%PDF-1.4 x",
                               original_name="pan.pdf", category="pan")
    path = settings.data_dir / "uploads" / "documents" / doc["stored_name"]
    assert path.exists()
    employees_service.delete_employee(storage, staff[0]["id"], settings=settings)
    assert not path.exists()


def test_emp03_employee_on_a_final_payslip_cannot_be_deleted(storage: Storage, settings, staff):
    _finalise(storage, settings, AUG)
    with pytest.raises(employees_service.ValidationError, match="inactive"):
        employees_service.delete_employee(storage, staff[0]["id"], settings=settings)


def test_emp04_hr_email_receives_requests(hr_client: TestClient, storage: Storage, settings, hr_user, staff):
    token = csrf_of(hr_client, "/settings")
    hr_client.post(f"/settings/users/{hr_user['id']}/email", data={"_csrf": token, "email": "hr@example.com"})
    assert auth.get_user(storage, hr_user["id"])["email"] == "hr@example.com"
    from app.services import regularization
    day = next(d for d in reversed(_working_days(storage, orgtime.period_of(orgtime.today(settings))))
               if d < orgtime.today(settings))
    regularization.submit(storage, settings, employee_id=staff[0]["id"], work_date=day.isoformat(),
                          check_in="09:30", check_out="18:30", reason="scanner missed me")
    assert storage.query_one("SELECT recipient FROM notifications WHERE recipient = 'hr@example.com'")


def test_att01_att02_register_links_and_bad_month(hr_client: TestClient, storage: Storage, staff):
    employees_service.update_employee(storage, staff[0]["id"], {"department": "R&D"})
    page = hr_client.get("/attendance/register?period=2026-09&department=R%26D").text
    assert "department=R%26D" in page
    bad = hr_client.get("/attendance/register?period=Sept-2026&department=R%26D")
    assert "not a valid month" in bad.text and 'value="R&amp;D" selected' in bad.text
    assert orgtime.parse_period("2026-9") == "2026-09" and orgtime.parse_period("09/2026") == "2026-09"


def test_exit01_fnf_pays_every_unpaid_month(storage: Storage, settings):
    e = _employee(storage, "Long Notice")
    for period in ("2026-08", SEPT):
        _fill(storage, e["id"], _working_days(storage, period))
    payroll.generate_run(storage, settings, "2026-07")       # last paid month: July
    row = exits_service.record_exit(storage, settings, employee_id=e["id"], exit_type="resignation",
                                    resignation_date="2026-08-01", last_working_day="2026-09-30", notice_days=30)
    fnf = exits_service.compute_fnf(storage, settings, row)
    labels = [line["label"] for line in fnf["earnings"]]
    assert "Salary August 2026" in labels and "Salary September 2026" in labels


# --------------------------------------------------------------- hardening (step 7)
import io  # noqa: E402

from app import notify, uploads  # noqa: E402


def test_pay12_mid_month_raise_is_split_by_days(storage: Storage, settings):
    e = _employee(storage, "Raise Mid Month")
    employees_service.set_salary(storage, e["id"], monthly_gross=60000, effective_from="2026-09-16")
    _fill(storage, e["id"], _working_days(storage, SEPT))
    calc = payroll.compute_slip(storage, settings, e, SEPT)
    assert calc["monthly_gross"] == pytest.approx(30000 * 15 / 30 + 60000 * 15 / 30)


def test_lv07_overwriting_a_leave_day_gives_it_back(storage: Storage, settings, aarav):
    result = leave_service.apply_leave(storage, settings, employee_id=aarav["id"], leave_type="paid",
                                       from_date="2026-09-07", to_date="2026-09-08")
    attendance_service.mark_manual(storage, employee_id=aarav["id"], work_date="2026-09-08", status="present",
                                   check_in="09:30", check_out="18:30")
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (result["leave"]["id"],))
    assert leave["days"] == 1
    attendance_service.mark_manual(storage, employee_id=aarav["id"], work_date="2026-09-07", status="present",
                                   check_in="09:30", check_out="18:30")
    leave = storage.query_one("SELECT * FROM leave_requests WHERE id = ?", (result["leave"]["id"],))
    assert leave["status"] == "cancelled"


def test_att04_department_spellings_are_merged(storage: Storage):
    a = employees_service.create_employee(storage, {"name": "A", "department": "Sales"})
    b = employees_service.create_employee(storage, {"name": "B", "department": "  sales  "})
    assert employees_service.get_employee(storage, b)["department"] == "Sales"
    employees_service.update_employee(storage, a, {"department": "SALES"})
    assert employees_service.departments(storage) == ["Sales"]


def test_att05_field_checkout_needs_a_checkin_and_good_gps(storage: Storage, settings, aarav):
    from app.services import field
    field.add_location(storage, name="Office", lat=22.7, lng=75.8, radius_m=200)
    result = field.check_in(storage, settings, employee=aarav, lat=22.7, lng=75.8, kind="check_out")
    assert not result["ok"] and "checked in" in result["message"]
    result = field.check_in(storage, settings, employee=aarav, lat=22.7, lng=75.8, accuracy=5000)
    assert not result["ok"] and "GPS" in result["message"]


def test_att06_no_scans_after_the_last_working_day(storage: Storage, settings, aarav):
    yesterday = orgtime.today(settings) - timedelta(days=1)
    exits_service.record_exit(storage, settings, employee_id=aarav["id"], exit_type="resignation",
                              resignation_date=(yesterday - timedelta(days=30)).isoformat(),
                              last_working_day=yesterday.isoformat())
    outcome = attendance_service.mark_by_face(storage, settings, employees_service.get_employee(storage, aarav["id"]))
    assert not outcome.ok and "last working day" in outcome.message


def test_sec06_uploads_are_capped_while_reading():
    with pytest.raises(uploads.UploadError, match="too large"):
        uploads.read_capped(io.BytesIO(b"x" * 2000), limit=1000)
    assert uploads.read_capped(io.BytesIO(b"abc"), limit=1000) == b"abc"


def test_sec07_security_headers_and_hidden_docs(client: TestClient):
    response = client.get("/login")
    assert response.headers["X-Frame-Options"] in ("DENY", "SAMEORIGIN")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]
    assert client.get("/api/docs").status_code == 404


def test_exit02_settled_leaver_is_not_paid_again(storage: Storage, settings):
    e = _employee(storage, "Gone")
    row = exits_service.record_exit(storage, settings, employee_id=e["id"], exit_type="resignation",
                                    resignation_date="2026-07-01", last_working_day="2026-07-31")
    for item in exits_service.checklist(storage, row["id"]):
        exits_service.toggle_item(storage, item["id"], done=True)
    exits_service.settle(storage, settings, row["id"])
    payroll.generate_run(storage, settings, AUG, include_inactive=True)
    assert all(s["employee_id"] != e["id"] for s in payroll.list_slips(storage, AUG))


def test_apr01_exited_employee_does_not_block_closing(storage: Storage, settings):
    from app.services import appraisal as appraisal_service
    stays, leaves = _employee(storage, "Stays"), _employee(storage, "Leaves")
    cycle = appraisal_service.create_cycle(storage, name="H2", period_start="2026-04-01",
                                           period_end="2026-09-30", goals_template="Delivery")
    appraisal_service.launch_cycle(storage, cycle["id"])
    today = orgtime.today(settings).isoformat()
    row = exits_service.record_exit(storage, settings, employee_id=leaves["id"], exit_type="termination",
                                    resignation_date=today, last_working_day=today)
    for item in exits_service.checklist(storage, row["id"]):
        exits_service.toggle_item(storage, item["id"], done=True)
    exits_service.settle(storage, settings, row["id"])
    mine = next(a for a in appraisal_service.cycle_appraisals(storage, cycle["id"]) if a["employee_id"] == stays["id"])
    full = appraisal_service.get_appraisal(storage, mine["id"])
    appraisal_service.review(storage, mine["id"], scores={g["id"]: 4 for g in full["goals"]}, notes={},
                             comment=None, complete=True)
    others = [a for a in appraisal_service.cycle_appraisals(storage, cycle["id"])
              if a["status"] not in ("completed", "withdrawn")]
    for item in others:
        appraisal_service.withdraw(storage, item["id"])
    appraisal_service.close_cycle(storage, cycle["id"])


def test_ntf01_a_message_is_claimed_once(storage: Storage, settings):
    row_id = notify.queue(storage, channel="email", recipient="a@example.com", subject="s", body="<p>b</p>",
                          settings=settings)
    assert notify.claim(storage, row_id) is True
    assert notify.claim(storage, row_id) is False  # a second sender skips it


def test_ntf01_failures_back_off(storage: Storage, settings, monkeypatch):
    row_id = notify.queue(storage, channel="email", recipient="a@example.com", subject="s", body="<p>b</p>",
                          settings=settings)
    monkeypatch.setattr(notify, "_send_email", lambda *a, **k: (_ for _ in ()).throw(notify.SendError("boom")))
    notify.dispatch(storage, settings)
    row = storage.query_one("SELECT * FROM notifications WHERE id = ?", (row_id,))
    assert row["status"] == "pending" and row["next_attempt_at"]
    assert notify.dispatch(storage, settings) == []  # not due yet
