"""Regression tests for the issues listed in report.md (audit of 20 Nov 2026)."""

from __future__ import annotations

import re
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.services import attendance as attendance_service
from app.services import deductions as deductions_service
from app.services import employees as employees_service
from app.services import exits as exits_service
from app.services import leave as leave_service
from app.services import loans as loans_service
from app.services import orgtime, payroll
from tests.conftest import HR_PASSWORD, HR_USERNAME, FakeFaceService

PERIOD = "2026-10"


def _employee(storage, name, gross=30000, **extra):
    return employees_service.get_employee(storage, employees_service.create_employee(
        storage, {"name": name, "joined_on": "2025-01-01", "monthly_gross": gross, **extra}))


def _working_days(storage, period):
    return [d for d in orgtime.days_in_period(period) if orgtime.day_kind(storage, d) == "working"]


def _fill(storage, employee_id, days):
    for day in days:
        attendance_service.mark_manual(storage, employee_id=employee_id, work_date=day.isoformat(),
                                       status="present", check_in="09:30", check_out="18:30")


def _next_working_day(storage, settings, offset):
    day = orgtime.today(settings) + timedelta(days=offset)
    while orgtime.day_kind(storage, day) != "working":
        day += timedelta(days=1)
    return day


def _loan(storage, employee_id, principal, emi):
    return loans_service.create_loan(storage, employee_id=employee_id, principal_amount=principal,
                                     monthly_emi=emi, loan_type="personal_loan", disbursed_on="2026-09-01")


def _loan_row(storage, loan_id):
    return storage.query_one("SELECT total_repaid, remaining_balance, status FROM employee_loans WHERE id = ?",
                             (loan_id,))


# --------------------------------------------------------------- C1: stored XSS
def test_c1_leave_history_escapes_user_text(hr_client, storage, settings, staff):
    leave_service.apply_leave(storage, settings, employee_id=staff[0]["id"], leave_type="unpaid",
                              from_date="2026-01-07", status="pending",
                              reason='<img src=x onerror="alert(1)">')
    page = hr_client.get("/leave").text
    assert "window.escapeHtml" in page and "esc(l.reason" in page
    assert "+ (l.reason || '—') +" not in page  # the old raw interpolation
    data = hr_client.get(f"/leave/employee/{staff[0]['id']}/history",
                         headers={"Accept": "application/json"}).json()
    assert data["leaves"][0]["status_label"] in ("Awaiting HR", "Awaiting manager")


def test_c1_copy_buttons_quote_values_safely(hr_client, storage, staff):
    # validation blocks this today, but older rows / imports may still carry such text
    storage.execute("UPDATE employees SET esic_ip = ? WHERE id = ?", ("1');alert(1);//", staff[0]["id"]))
    page = hr_client.get(f"/employees/{staff[0]['id']}").text
    assert "copyToClipboard('1');alert(1)" not in page
    assert "copyToClipboard(\"1\\u0027);alert(1);//\", this)" in page


# ------------------------------------------------- C2/C3: remote employee kiosk
@pytest.fixture
def remote_pair(app, storage, settings):
    ann = _employee(storage, "Remote Ann")
    bob = _employee(storage, "Colleague Bob")
    employees_service.update_employee(storage, ann["id"], {"remote_work_enabled": 1})
    fake = FakeFaceService()
    for person, photo in ((ann, b"ann-photo"), (bob, b"bob-photo")):
        result = fake.enrol_face(photo, external_id=employees_service.get_employee(storage, person["id"])["code"])
        storage.execute("UPDATE employees SET face_id = ? WHERE id = ?", (result.face_id, person["id"]))
    app.state.face = fake
    user, _ = auth.ensure_employee_login(storage, employees_service.get_employee(storage, ann["id"]),
                                         password="annpass1234")
    client = TestClient(app)
    client.post("/login", data={"username": user["username"], "password": "annpass1234"})
    token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/me").text).group(1)
    return client, token, ann, bob


def test_c3_scanning_a_colleague_is_refused_not_a_crash(remote_pair, storage):
    client, token, _ann, bob = remote_pair
    response = client.post("/api/kiosk/scan", data={"_csrf": token},
                           files={"image": ("s.jpg", b"bob-photo", "image/jpeg")})
    assert response.status_code == 403
    assert response.json()["action"] == "identity_mismatch"
    assert attendance_service.get_row(storage, bob["id"], orgtime.today(storage.settings)) is None


def test_c2_employee_cannot_use_offline_sync(remote_pair, storage):
    client, token, _ann, bob = remote_pair
    captured = int((time.time() - 3 * 3600) * 1000)
    response = client.post("/api/kiosk/offline-sync", headers={"X-CSRF-Token": token},
                           files={"image": ("s.jpg", b"bob-photo", "image/jpeg")},
                           data={"scan_id": "ab" * 16, "captured_at": str(captured)})
    assert response.json()["action"] == "not_allowed"
    assert storage.query("SELECT id FROM attendance WHERE employee_id = ?", (bob["id"],)) == []


# ---------------------------------------------- H1/H2: leave through a manager
@pytest.fixture
def reportee(storage):
    manager = _employee(storage, "Manager")
    person = _employee(storage, "Reportee")
    employees_service.update_employee(storage, person["id"], {"manager_id": manager["id"]})
    return person


def test_h1_duplicate_requests_awaiting_manager_are_refused(storage, settings, reportee):
    day = _next_working_day(storage, settings, 10)
    first = leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="paid",
                                      from_date=day.isoformat(), status="pending")
    assert first["leave"]["status"] == "pending_manager"
    with pytest.raises(employees_service.ValidationError, match="pending request"):
        leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="paid",
                                  from_date=day.isoformat(), status="pending")


def test_h2_requests_awaiting_manager_count_against_balance(storage, settings, reportee):
    available = leave_service.balance(storage, settings, reportee["id"])["pl_available"]
    chunk = int(available * 0.6)
    start = _next_working_day(storage, settings, 7)
    end = start
    counted = 1
    while counted < chunk:
        end += timedelta(days=1)
        if orgtime.day_kind(storage, end) == "working":
            counted += 1
    leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="paid",
                              from_date=start.isoformat(), to_date=end.isoformat(), status="pending")
    later = end + timedelta(days=30)
    with pytest.raises(employees_service.ValidationError, match="Not enough paid leave"):
        leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="paid",
                                  from_date=later.isoformat(), to_date=(later + timedelta(days=int(chunk * 1.5))).isoformat(),
                                  status="pending")


def test_h2_hr_approving_from_manager_stage_checks_balance(storage, settings, reportee):
    day = _next_working_day(storage, settings, 10)
    leave = leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="paid",
                                      from_date=day.isoformat(), status="pending")["leave"]
    # the balance shrinks after the request was made (e.g. HR books other leave)
    storage.execute("UPDATE employees SET custom_pl_accrual = 0 WHERE id = ?", (reportee["id"],))
    storage.set_setting("pl_policy_mode", "fixed")
    with pytest.raises(employees_service.ValidationError, match="Not enough"):
        leave_service.approve_leave(storage, leave_id=leave["id"], actor="hr")


# ------------------------------------------------------------ M1: managers
def test_m1_inactive_manager_sends_request_straight_to_hr(storage, settings, reportee):
    manager_id = employees_service.get_employee(storage, reportee["id"])["manager_id"]
    storage.execute("UPDATE employees SET status = 'inactive' WHERE id = ?", (manager_id,))
    day = _next_working_day(storage, settings, 10)
    leave = leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="unpaid",
                                      from_date=day.isoformat(), status="pending")["leave"]
    assert leave["status"] == "pending"


def test_m1_reporting_loops_are_refused(storage):
    a, b = _employee(storage, "Alpha"), _employee(storage, "Beta")
    employees_service.update_employee(storage, a["id"], {"manager_id": b["id"]})
    with pytest.raises(employees_service.ValidationError, match="reports"):
        employees_service.update_employee(storage, b["id"], {"manager_id": a["id"]})
    with pytest.raises(employees_service.ValidationError, match="does not exist"):
        employees_service.update_employee(storage, b["id"], {"manager_id": 99999})


# --------------------------------------------------------------- M4: HR note
def test_m4_rejection_note_keeps_the_original_reason(storage, settings, staff):
    leave = leave_service.apply_leave(storage, settings, employee_id=staff[0]["id"], leave_type="unpaid",
                                      from_date="2026-01-07", status="pending", reason="Family function")["leave"]
    rejected = leave_service.reject_leave(storage, leave_id=leave["id"], actor="hr", reason="Audit week")
    assert rejected["reason"] == "Family function"
    assert rejected["decision_note"] == "Audit week"


# ------------------------------------------------- H3: ledger on recalculation
def test_h3_deduction_added_after_first_run_is_picked_up(storage, settings):
    person = _employee(storage, "Ledger")
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    deductions_service.add_deduction(storage, employee_id=person["id"], period=PERIOD, amount=1500,
                                     reason="Damage", category="other")
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT * FROM payslips WHERE employee_id = ?", (person["id"],))
    assert slip["other_deduction"] == 1500.0
    assert slip["other_override"] is None


def test_h3_hr_override_survives_recalculation(storage, settings):
    person = _employee(storage, "Override")
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT id FROM payslips WHERE employee_id = ?", (person["id"],))
    payroll.update_slip(storage, slip["id"], bonus=500, advance=0, other_deduction=700,
                        bonus_note="Diwali", actor="hr")
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT * FROM payslips WHERE employee_id = ?", (person["id"],))
    assert (slip["other_deduction"], slip["bonus"], slip["bonus_note"]) == (700.0, 500.0, "Diwali")


# ---------------------------------------------------------- H4–H6: loans
def test_h4_loan_emi_never_makes_net_pay_negative(storage, settings):
    person = _employee(storage, "Short Month", gross=10000)
    _loan(storage, person["id"], 50000, 20000)
    _fill(storage, person["id"], _working_days(storage, PERIOD)[:3])
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT * FROM payslips WHERE employee_id = ?", (person["id"],))
    assert slip["net_pay"] >= 0
    assert slip["advance"] < 20000
    assert "reduced" in (slip["note"] or "")


def test_h5_repayment_follows_the_amount_actually_deducted(storage, settings):
    person = _employee(storage, "Skip EMI", gross=50000)
    loan_id = _loan(storage, person["id"], 50000, 10000)
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT id FROM payslips WHERE employee_id = ?", (person["id"],))
    payroll.update_slip(storage, slip["id"], bonus=0, advance=0, other_deduction=0, actor="hr")
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    assert _loan_row(storage, loan_id)["remaining_balance"] == 50000.0


def test_h6_reopen_reverses_repayments_and_allows_recalc_and_delete(storage, settings):
    person = _employee(storage, "Reopen", gross=50000)
    loan_id = _loan(storage, person["id"], 50000, 10000)
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    assert _loan_row(storage, loan_id)["remaining_balance"] == 40000.0
    payroll.reopen_run(storage, PERIOD, actor="hr")
    assert _loan_row(storage, loan_id)["remaining_balance"] == 50000.0
    payroll.generate_run(storage, settings, PERIOD)          # used to raise FOREIGN KEY constraint failed
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    assert _loan_row(storage, loan_id)["remaining_balance"] == 40000.0  # booked once, not twice
    payroll.reopen_run(storage, PERIOD, actor="hr")
    payroll.delete_run(storage, PERIOD, actor="hr")
    assert _loan_row(storage, loan_id) == {"total_repaid": 0.0, "remaining_balance": 50000.0, "status": "active"}


def test_h6_holding_a_slip_after_finalising_reverses_its_emi(storage, settings):
    person = _employee(storage, "Hold Later", gross=50000)
    loan_id = _loan(storage, person["id"], 50000, 10000)
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    slip = storage.query_one("SELECT id FROM payslips WHERE employee_id = ?", (person["id"],))
    payroll.set_slip_status(storage, slip["id"], "on_hold", actor="hr", reason="dispute")
    assert _loan_row(storage, loan_id)["remaining_balance"] == 50000.0
    payroll.set_slip_status(storage, slip["id"], "draft", actor="hr")  # "release" in a final run
    assert storage.scalar("SELECT status FROM payslips WHERE id = ?", (slip["id"],)) == "finalized"
    assert _loan_row(storage, loan_id)["remaining_balance"] == 40000.0


# ------------------------------------------------------------- H7 / M3
def test_h7_zero_percent_retention_deducts_nothing(storage, settings):
    person = _employee(storage, "No Retention")
    employees_service.update_employee(storage, person["id"], {"retention_enabled": 1, "retention_percent": 0})
    person = employees_service.get_employee(storage, person["id"])
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    assert payroll.compute_slip(storage, settings, person, PERIOD)["retention_amount"] == 0.0


def test_retention_analytics_only_counts_finalized_or_paid_runs(storage, settings):
    person = _employee(storage, "Ret Analytics", gross=30000)
    employees_service.update_employee(storage, person["id"], {"retention_enabled": 1, "retention_percent": 20, "retention_months": 3})
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)  # draft run

    # Draft run retention must NOT be counted in currently held pool
    analytics = payroll.retention_fund_analytics(storage)
    assert analytics["total_currently_held"] == 0.0

    # Once finalized, it enters the held pool
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    analytics_final = payroll.retention_fund_analytics(storage)
    assert analytics_final["total_currently_held"] == 6000.0


def test_m3_a_draft_run_cannot_jump_to_paid(storage, settings):
    person = _employee(storage, "Drafty")
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    with pytest.raises(employees_service.ValidationError, match="Finalise"):
        payroll.set_run_status(storage, PERIOD, "paid")


# -------------------------------------------------------------------- H8
def test_h8_fnf_recovers_outstanding_loan_and_counts_only_final_retention(storage, settings):
    person = _employee(storage, "Leaver", gross=30000)
    employees_service.update_employee(storage, person["id"], {"retention_enabled": 1})
    _loan(storage, person["id"], 20000, 5000)
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)  # draft only: nothing withheld yet
    exit_row = exits_service.record_exit(storage, settings, employee_id=person["id"], exit_type="resignation",
                                         resignation_date="2026-10-01", last_working_day="2026-10-31")
    fnf = exits_service.compute_fnf(storage, settings, exit_row)
    labels = {line["label"]: line["amount"] for line in fnf["deductions"] + fnf["earnings"]}
    assert "Refund of Salary Retention Fund" not in labels
    assert labels["Outstanding loan / advance"] == 15000.0  # 20,000 minus the EMI the draft run takes


# -------------------------------------------------------------------- M2
def test_m2_finalising_does_not_announce_held_payslips(hr_client, storage, settings):
    held, normal = _employee(storage, "Held"), _employee(storage, "Normal")
    for person in (held, normal):
        _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    slip = storage.query_one("SELECT id FROM payslips WHERE employee_id = ?", (held["id"],))
    payroll.set_slip_status(storage, slip["id"], "on_hold", actor="hr", reason="check")
    token = re.search(r'name="csrf-token" content="([^"]+)"', hr_client.get("/payroll").text).group(1)
    for _ in range(2):  # a double submit must not notify twice
        hr_client.post("/payroll/status", data={"_csrf": token, "period": PERIOD, "status": "finalized"})
    pushes = storage.query("SELECT recipient FROM notifications WHERE category = 'payslip'")
    recipients = " ".join(str(row["recipient"]) for row in pushes)
    assert len(pushes) <= 1 and str(held["id"]) not in recipients.split()


# -------------------------------------------------------------------- M5
def test_m5_face_login_lockout_is_per_ip(storage):
    for _ in range(auth.LOCKOUT_PER_USER):
        auth.record_login_failure(storage, "face_login:10.0.0.1", "10.0.0.1")
    assert auth.login_locked(storage, "face_login:10.0.0.1", "10.0.0.1")
    assert not auth.login_locked(storage, "face_login:10.0.0.2", "10.0.0.2")


# -------------------------------------------------------------------- M6
def test_m6_a_racing_first_scan_is_handled(storage, settings, staff, monkeypatch):
    from app.services import attendance as svc

    real_get_row = svc.get_row
    calls = {"n": 0}

    def stale_get_row(storage_, employee_id, day):
        calls["n"] += 1
        if calls["n"] == 1:  # first look: another device inserts right after we read
            svc.mark_manual(storage_, employee_id=employee_id, work_date=str(day), status="present",
                            check_in="09:00")
            return None
        return real_get_row(storage_, employee_id, day)

    monkeypatch.setattr(svc, "get_row", stale_get_row)
    outcome = svc.mark_by_face(storage, settings, staff[0])
    assert outcome.action in ("duplicate", "check_out", "too_early")


# ------------------------------------------------------------------- UX
def test_u3_employee_can_withdraw_a_request_awaiting_manager(app, storage, settings, reportee):
    user, _ = auth.ensure_employee_login(storage, reportee, password="portal-pass-1")
    day = _next_working_day(storage, settings, 10)
    leave_service.apply_leave(storage, settings, employee_id=reportee["id"], leave_type="unpaid",
                              from_date=day.isoformat(), status="pending")
    client = TestClient(app)
    client.post("/login", data={"username": user["username"], "password": "portal-pass-1"})
    page = client.get("/me/leave").text
    assert "Awaiting manager" in page and "pending_manager" not in page.split("<script")[0]
    assert "Withdraw" in page


def test_u4_my_team_link_shows_on_every_portal_page(app, storage, reportee):
    manager = employees_service.get_employee(
        storage, employees_service.get_employee(storage, reportee["id"])["manager_id"])
    user, _ = auth.ensure_employee_login(storage, manager, password="portal-pass-1")
    client = TestClient(app)
    client.post("/login", data={"username": user["username"], "password": "portal-pass-1"})
    for path in ("/me", "/me/leave", "/me/attendance", "/me/payslips"):
        assert 'href="/me/team"' in client.get(path).text, path


def test_hr_login_still_works_after_changes(client, hr_user):
    response = client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD},
                           follow_redirects=False)
    assert response.status_code == 303


def test_u9_later_scan_corrects_an_early_check_out(storage, settings, staff):
    from datetime import datetime as dt

    day = _next_working_day(storage, settings, 1)

    def at(hour, minute):
        return dt(day.year, day.month, day.day, hour, minute, tzinfo=settings.tz)

    person = staff[0]
    attendance_service.mark_by_face(storage, settings, person, moment=at(9, 25))
    early = attendance_service.mark_by_face(storage, settings, person, moment=at(13, 0))
    assert early.action == "check_out" and early.status == orgtime.STATUS_HALF_DAY
    # an intermediate scan still before shift end (e.g. 14:00) does not become full day
    mid = attendance_service.mark_by_face(storage, settings, person, moment=at(14, 0))
    assert mid.action == "check_out" and mid.status == orgtime.STATUS_HALF_DAY
    fixed = attendance_service.mark_by_face(storage, settings, person, moment=at(18, 35))
    assert fixed.action == "check_out" and fixed.status == orgtime.STATUS_PRESENT
    row = attendance_service.get_row(storage, person["id"], day)
    assert row["check_out"].startswith("18:35") and "corrected from 13:00" in row["note"]
    # once the check-out is after shift end, another scan is still refused
    assert attendance_service.mark_by_face(storage, settings, person, moment=at(20, 0)).action == "already_done"


def test_historical_import_never_touches_real_runs_or_the_running_month(storage, settings):
    from app.services import employees_bulk

    person = _employee(storage, "History")
    _fill(storage, person["id"], _working_days(storage, PERIOD))
    payroll.generate_run(storage, settings, PERIOD)
    payroll.set_run_status(storage, PERIOD, "finalized", actor="hr")
    before = storage.query_one("SELECT net_pay FROM payslips WHERE employee_id = ?", (person["id"],))
    assert employees_bulk.record_historical_payslip(storage, employee_id=person["id"], period=PERIOD,
                                                    salary_amount=1.0) is False
    assert storage.query_one("SELECT net_pay FROM payslips WHERE employee_id = ?", (person["id"],)) == before
    current = orgtime.period_of(orgtime.today(settings))
    assert employees_bulk.record_historical_payslip(storage, employee_id=person["id"], period=current,
                                                    salary_amount=30000) is False
    assert payroll.get_run(storage, current) is None  # this month's payroll stays free to run
    assert current not in employees_bulk.get_past_months(18, orgtime.today(settings))


def test_csv_exports_neutralise_formulas():
    from app.services.csvsafe import csv_cell

    assert csv_cell('=HYPERLINK("http://evil","x")').startswith("\"'=")
    assert csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert csv_cell("-500") == "-500" and csv_cell(-500.5) == "-500.5"
    assert csv_cell("Ravi, Kumar") == '"Ravi, Kumar"'


# ===================================================== leave buckets & type (A/B)
def _run_of_working_days(storage, start, count):
    days, day = [], start
    while len(days) < count:
        if orgtime.day_kind(storage, day) == "working":
            days.append(day)
        day += timedelta(days=1)
    return days[0], days[-1]


def test_a_reason_templates_never_change_the_leave_type(app, storage, staff):
    user, _ = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    client = TestClient(app)
    client.post("/login", data={"username": user["username"], "password": "portal-pass-1"})
    page = client.get("/me/leave").text
    assert "leaveTypeEl.value = 'casual'" not in page and "leaveTypeEl.value = 'sick'" not in page
    assert 'value="" selected disabled>Choose leave type' in page
    assert "days left" in page or "day left" in page


def test_a_a_leave_type_is_required(storage, settings, staff):
    with pytest.raises(employees_service.ValidationError, match="Choose a leave type"):
        leave_service.apply_leave(storage, settings, employee_id=staff[0]["id"], leave_type="",
                                  from_date="2026-01-07", status="pending")
    with pytest.raises(employees_service.ValidationError, match="Choose the leave type"):
        attendance_service.mark_manual(storage, employee_id=staff[0]["id"], work_date="2026-01-07",
                                       status="leave", actor="hr")


def test_b1_next_leave_year_has_its_own_quota(storage, settings):
    person = _employee(storage, "Next Year")
    year_start, _ = orgtime.leave_cycle_bounds(orgtime.today(settings), "financial_year")
    next_year = year_start.replace(year=year_start.year + 1)
    # this year's quota used up does not block next year's dates ...
    a, z = _run_of_working_days(storage, _next_working_day(storage, settings, 1), 6)
    if a < next_year:
        leave_service.apply_leave(storage, settings, employee_id=person["id"], leave_type="sick",
                                  from_date=a.isoformat(), to_date=z.isoformat(), status="pending")
    a, z = _run_of_working_days(storage, next_year + timedelta(days=10), 6)
    leave_service.apply_leave(storage, settings, employee_id=person["id"], leave_type="sick",
                              from_date=a.isoformat(), to_date=z.isoformat(), status="pending")
    # ... and next year's quota cannot be stacked
    a, z = _run_of_working_days(storage, next_year + timedelta(days=60), 6)
    with pytest.raises(employees_service.ValidationError, match="leave year"):
        leave_service.apply_leave(storage, settings, employee_id=person["id"], leave_type="sick",
                                  from_date=a.isoformat(), to_date=z.isoformat(), status="pending")


def test_b1_leave_across_the_year_end_is_split(storage, settings):
    person = _employee(storage, "Boundary")
    year_start, year_end = orgtime.leave_cycle_bounds(orgtime.today(settings), "financial_year")
    a, z = _run_of_working_days(storage, year_end - timedelta(days=4), 6)
    leave = leave_service.apply_leave(storage, settings, employee_id=person["id"], leave_type="casual",
                                      from_date=a.isoformat(), to_date=z.isoformat(), status="pending")["leave"]
    before = orgtime.working_days(storage, a, year_end)
    this_year = leave_service.balance(storage, settings, person["id"], cycle_on=year_end)
    next_year = leave_service.balance(storage, settings, person["id"], cycle_on=year_end + timedelta(days=1))
    assert this_year["cl_net"] == pytest.approx(this_year["annual_cl"] - before)
    assert next_year["cl_net"] == pytest.approx(next_year["annual_cl"] - (leave["days"] - before))


def test_b3_leave_marked_on_the_board_uses_the_bucket(storage, settings):
    person = _employee(storage, "Board")
    employees_service.update_employee(storage, person["id"], {"custom_cl_quota": 2})
    days = _working_days(storage, PERIOD)
    for day in days[:2]:
        attendance_service.mark_manual(storage, employee_id=person["id"], work_date=day.isoformat(),
                                       status="leave", leave_type="casual", actor="hr")
    assert leave_service.balance(storage, settings, person["id"], cycle_on=days[0])["cl_net"] == 0
    with pytest.raises(employees_service.ValidationError, match="Not enough casual leave"):
        attendance_service.mark_manual(storage, employee_id=person["id"], work_date=days[2].isoformat(),
                                       status="leave", leave_type="casual", actor="hr")
    attendance_service.mark_manual(storage, employee_id=person["id"], work_date=days[2].isoformat(),
                                   status="leave", leave_type="casual", actor="hr", allow_negative=True)
    # re-saving the same day does not charge it twice; changing it gives the day back
    attendance_service.mark_manual(storage, employee_id=person["id"], work_date=days[2].isoformat(),
                                   status="leave", leave_type="casual", actor="hr", note="edited")
    assert leave_service.balance(storage, settings, person["id"], cycle_on=days[0])["cl_net"] == -1
    attendance_service.mark_manual(storage, employee_id=person["id"], work_date=days[2].isoformat(),
                                   status="present", check_in="09:30", check_out="18:30", actor="hr",
                                   release_leave=True)
    assert leave_service.balance(storage, settings, person["id"], cycle_on=days[0])["cl_net"] == 0


def test_b4_balance_import_reads_columns_by_name_in_any_order(storage, staff):
    rows = [["PL Quota", "Employee Name", "Unpaid LOP", "CL Quota", "Employee Code", "SL Quota"],
            ["9", staff[0]["name"], "0", "4", staff[0]["code"], "5"]]
    result = leave_service.import_balances_data(storage, rows, actor="hr")
    assert result["errors"] == []
    person = employees_service.get_employee(storage, staff[0]["id"])
    assert (person["custom_pl_accrual"], person["custom_sl_quota"], person["custom_cl_quota"]) == (9.0, 5.0, 4.0)


# ============================================================ stability (C)
def _face_with(settings, handler, **overrides):
    import httpx
    from dataclasses import replace
    from app.face import FaceService

    cfg = replace(settings, faceapi_url="http://face.test", face_queue_wait=0.2, face_breaker_failures=2,
                  face_breaker_seconds=30, **overrides)
    service = FaceService(cfg)
    service._collection_ready = True
    service._pool = httpx.Client(base_url="http://face.test", transport=httpx.MockTransport(handler))
    return service


def test_c1_hung_face_service_opens_the_breaker_and_fails_fast(settings):
    import httpx
    from app.face import FaceServiceUnavailable

    calls = {"n": 0}

    def hung(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("face api hung", request=request)

    face = _face_with(settings, hung)
    for _ in range(2):
        with pytest.raises(FaceServiceUnavailable):
            face.identify(b"jpeg")
    started = time.monotonic()
    with pytest.raises(FaceServiceUnavailable, match="not responding"):
        face.identify(b"jpeg")
    assert time.monotonic() - started < 0.1 and calls["n"] == 2  # no third call: breaker is open
    assert face.status()["breaker_open"] is True


def test_c1_all_slots_busy_answers_busy_quickly(settings):
    import httpx
    from app.face import FaceServiceBusy

    face = _face_with(settings, lambda r: httpx.Response(200, json={}), face_max_concurrent=1)
    face._slots.acquire()  # one scan already in flight
    try:
        started = time.monotonic()
        with pytest.raises(FaceServiceBusy):
            face.identify(b"jpeg")
        assert time.monotonic() - started < 1.0
    finally:
        face._slots.release()


def test_c1_no_face_answers_do_not_trip_the_breaker(settings):
    import httpx

    face = _face_with(settings, lambda r: httpx.Response(422, json={"error": {"message": "No face"}}))
    for _ in range(5):
        assert face.identify(b"jpeg").matched is False
    assert face.status()["breaker_open"] is False


def test_c2_healthz_is_async(app):
    import inspect
    from app.routes import meta

    assert inspect.iscoroutinefunction(meta.healthz)
