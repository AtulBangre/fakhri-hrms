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
    assert again["net_pay"] == pytest.approx(again["earned_gross"] + 5000 - again["total_deductions"])
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
