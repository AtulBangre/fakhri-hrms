"""Core service tests: time helpers, auth, employees, attendance, leave, payroll."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest

from app import auth
from app.db import Storage
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import orgtime, payroll
from tests.conftest import FakeFaceService, at

# ------------------------------------------------------------------ time helpers
def test_parse_clock_accepts_both_formats():
    assert orgtime.parse_clock("09:30") == orgtime.parse_clock("09:30:00")
    assert orgtime.parse_clock("24:00") is None
    assert orgtime.parse_clock(None) is None
    assert orgtime.parse_clock("garbage") is None


def test_minutes_between_handles_overnight():
    assert orgtime.minutes_between("09:30:00", "18:30:00") == 540
    assert orgtime.minutes_between("22:00:00", "02:00:00") == 240
    assert orgtime.minutes_between(None, "18:30:00") is None


def test_seconds_between_is_second_precise():
    assert orgtime.seconds_between("09:30:00", "09:30:45") == 45
    assert orgtime.seconds_between("09:30:00", "09:31:00") == 60


def test_month_bounds_and_labels():
    start, end = orgtime.month_bounds("2026-02")
    assert (start, end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert orgtime.month_days("2024-02") == 29
    assert orgtime.month_label("2026-01") == "January 2026"


def test_late_minutes_respects_grace(storage: Storage):
    # shift starts 09:30, grace 15 → late only after 09:45
    assert orgtime.late_minutes_for(storage, at(9, 20)) == 0
    assert orgtime.late_minutes_for(storage, at(9, 45)) == 0
    assert orgtime.late_minutes_for(storage, at(10, 5)) == 20


def test_week_off_and_working_day_count(storage: Storage):
    # default week off is Sunday (weekday 6)
    assert orgtime.is_week_off(storage, date(2026, 1, 4)) is True
    assert orgtime.is_week_off(storage, date(2026, 1, 5)) is False
    # January 2026 has 4 Sundays: 4, 11, 18, 25
    assert orgtime.working_days(storage, date(2026, 1, 1), date(2026, 1, 31)) == 27.0


def test_holidays_are_excluded_from_working_days(storage: Storage):
    storage.execute("INSERT INTO holidays (day, name, created_at) VALUES ('2026-01-26', 'Republic Day', 'now')")
    assert orgtime.day_kind(storage, date(2026, 1, 26)) == orgtime.STATUS_HOLIDAY
    assert orgtime.working_days(storage, date(2026, 1, 1), date(2026, 1, 31)) == 26.0


# ------------------------------------------------------------------------- auth
def test_password_hash_roundtrip():
    digest, salt = auth.hash_password("s3cret-pass")
    assert auth.verify_password("s3cret-pass", digest, salt) is True
    assert auth.verify_password("wrong", digest, salt) is False
    # same password, different salt → different hash
    other, _ = auth.hash_password("s3cret-pass")
    assert other != digest


def test_session_token_roundtrip_and_tamper(settings):
    user = {"id": 7, "username": "hr", "role": "hr", "display_name": "HR Admin", "device_label": None}
    token, session = auth.create_session_token(user, settings, lifetime_seconds=60)
    assert session.role == "hr"
    assert session.csrf

    restored = auth.read_session_token(token, settings)
    assert restored is not None and restored.id == 7 and restored.csrf == session.csrf

    # any change to the payload invalidates the signature
    payload, signature = token.split(".")
    assert auth.read_session_token(f"{payload}x.{signature}", settings) is None
    assert auth.read_session_token(f"{payload}.{signature[:-1]}a", settings) is None
    assert auth.read_session_token("garbage", settings) is None


def test_expired_session_is_rejected(settings):
    user = {"id": 1, "username": "hr", "role": "hr", "display_name": "x", "device_label": None}
    token, _ = auth.create_session_token(user, settings, lifetime_seconds=-5)
    assert auth.read_session_token(token, settings) is None


def test_authenticate_and_role_rules(storage: Storage, settings):
    created = auth.ensure_default_users(storage, settings)
    passwords = {username: password for _role, username, password in created}

    assert auth.authenticate(storage, "hr", "wrong") is None
    user = auth.authenticate(storage, "hr", passwords["hr"])
    assert user is not None and user["role"] == "hr"

    assert auth.authenticate(storage, "gate", "not-the-password") is None
    gate = auth.authenticate(storage, "gate", passwords["gate"])
    assert gate is not None and gate["role"] == "kiosk"


def test_inactive_user_cannot_sign_in(storage: Storage, hr_user):
    from app import auth as auth_module

    auth_module.set_user_active(storage, hr_user["id"], False)
    assert auth_module.authenticate(storage, "hr", "changeme123") is None
    auth_module.set_user_active(storage, hr_user["id"], True)
    assert auth_module.authenticate(storage, "hr", "changeme123") is not None


def test_password_change_takes_effect(storage: Storage, hr_user):
    assert auth.authenticate(storage, "hr", "changeme123") is not None
    auth.change_password(storage, hr_user["id"], "brand-new-pass")
    assert auth.authenticate(storage, "hr", "changeme123") is None
    assert auth.authenticate(storage, "hr", "brand-new-pass") is not None


def test_ensure_default_users_writes_credentials_once(storage: Storage, settings):
    created = auth.ensure_default_users(storage, settings)
    assert {role for role, _, _ in created} == {"hr", "kiosk"}
    assert (settings.data_dir / auth.CREDENTIALS_FILE).exists()

    again = auth.ensure_default_users(storage, settings)
    assert again == []
    assert len(auth.list_users(storage)) == 2


def test_kiosk_user_carries_device_label(storage: Storage, kiosk_user):
    assert kiosk_user["device_label"] == "Main Gate"


# -------------------------------------------------------------------- employees
def test_create_employee_assigns_sequential_codes(storage: Storage, staff):
    assert [person["code"] for person in staff] == ["FITS001", "FITS002", "FITS003"]


def test_create_employee_requires_name(storage: Storage):
    with pytest.raises(employees_service.ValidationError):
        employees_service.create_employee(storage, {"name": "  "})


def test_duplicate_employee_code_is_rejected(storage: Storage, staff):
    with pytest.raises(employees_service.ValidationError) as excinfo:
        employees_service.create_employee(storage, {"name": "Copy Cat", "code": "FITS001"})
    assert "already used" in str(excinfo.value)


def test_invalid_joining_date_is_rejected(storage: Storage):
    with pytest.raises(employees_service.ValidationError):
        employees_service.create_employee(storage, {"name": "Bad Date", "joined_on": "01-04-2025"})


def test_salary_history_is_kept_and_latest_wins(storage: Storage, aarav):
    employees_service.set_salary(
        storage, aarav["id"], monthly_gross=68000, pf=2000, professional_tax=200,
        effective_from="2026-04-01", note="annual raise",
    )
    history = employees_service.salary_history(storage, aarav["id"])
    assert len(history) == 2  # joining salary + raise
    assert history[0]["monthly_gross"] == 68000

    refreshed = employees_service.get_employee(storage, aarav["id"])
    assert refreshed["monthly_gross"] == 68000


def test_negative_salary_is_rejected(storage: Storage, aarav):
    with pytest.raises(employees_service.ValidationError):
        employees_service.set_salary(storage, aarav["id"], monthly_gross=-1)


def test_update_employee_changes_department_and_status(storage: Storage, isha):
    employees_service.update_employee(
        storage, isha["id"], {"department": "Growth", "status": "inactive", "designation": "Marketing Lead"}
    )
    refreshed = employees_service.get_employee(storage, isha["id"])
    assert refreshed["department"] == "Growth"
    assert refreshed["status"] == "inactive"
    assert refreshed["designation"] == "Marketing Lead"
    assert employees_service.departments(storage) == ["Engineering", "Growth", "Operations"]


def test_employee_stats_counts_enrolment(storage: Storage, staff, fake_face):
    employees_service.enrol_face(storage, fake_face, staff[0]["id"], b"fake-image-bytes")
    stats = employees_service.stats(storage)
    assert stats["active"] == 3
    assert stats["face_enrolled"] == 1
    assert stats["missing_face"] == 2
    assert stats["monthly_payroll"] == pytest.approx(133000.0)


def test_enrol_face_replaces_previous_template(storage: Storage, staff, fake_face):
    first = employees_service.enrol_face(storage, fake_face, staff[0]["id"], b"img-1")
    second = employees_service.enrol_face(storage, fake_face, staff[0]["id"], b"img-2")
    assert first.face_id != second.face_id
    assert first.face_id in fake_face.deleted  # old template removed, no double match

    employee = employees_service.get_employee(storage, staff[0]["id"])
    assert employee["face_id"] == second.face_id
    assert employee["face_enrolled_at"]


def test_unenrol_face_clears_the_link(storage: Storage, staff, fake_face):
    employees_service.enrol_face(storage, fake_face, staff[0]["id"], b"img")
    employees_service.unenrol_face(storage, fake_face, staff[0]["id"])
    employee = employees_service.get_employee(storage, staff[0]["id"])
    assert employee["face_id"] is None
    assert employees_service.employees_missing_face(storage)


def test_delete_employee_removes_attendance_and_payslips(storage: Storage, settings, staff):
    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date="2026-01-05", status="present",
        check_in="09:30", check_out="18:30",
    )
    payroll.generate_run(storage, settings, "2026-01")
    employees_service.delete_employee(storage, staff[0]["id"])
    assert storage.scalar("SELECT COUNT(*) AS n FROM attendance WHERE employee_id = ?", (staff[0]["id"],)) == 0
    assert storage.scalar("SELECT COUNT(*) AS n FROM payslips WHERE employee_id = ?", (staff[0]["id"],)) == 0


# ------------------------------------------------------------------ attendance
def test_first_scan_checks_in_on_time(storage: Storage, settings, aarav):
    outcome = attendance_service.mark_by_face(
        storage, settings, aarav, device_label="Main Gate", moment=at(9, 30), similarity=0.91
    )
    assert outcome.ok and outcome.action == "check_in"
    assert outcome.status == "present"
    assert outcome.late_minutes == 0
    assert outcome.check_in == "09:30:00"
    assert outcome.similarity == 0.91

    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["source"] == "face"
    assert row["device_label"] == "Main Gate"


def test_late_arrival_records_late_minutes(storage: Storage, settings, aarav):
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(10, 15))
    assert outcome.ok
    assert outcome.late_minutes == 30  # 09:45 + 30 min
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["late_minutes"] == 30


def test_second_scan_checks_out_and_half_day_rule(storage: Storage, settings, aarav):
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    # leaves after 3 hours → less than the 4 hour half-day threshold...
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(12, 30))
    assert outcome.ok and outcome.action == "check_out"
    assert outcome.worked_minutes == 180
    assert outcome.status == "half_day"


def test_full_day_scan_stays_present(storage: Storage, settings, aarav):
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(18, 30))
    assert outcome.status == "present"
    assert outcome.worked_minutes == 540


def test_repeat_scan_inside_debounce_is_ignored(storage: Storage, settings, aarav):
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    outcome_b = attendance_service.mark_by_face(storage, settings, aarav, moment=datetime(2026, 1, 5, 9, 30, 30))
    for repeat in (outcome, outcome_b):
        assert repeat.ok is False and repeat.action == "duplicate"
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["check_out"] is None  # the day was not closed by the repeat


def test_check_out_is_refused_until_two_hours_after_check_in(storage: Storage, settings, aarav):
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))

    early = attendance_service.mark_by_face(
        storage, settings, aarav, moment=datetime(2026, 1, 5, 11, 29, 59), photo_bytes=b"not-saved"
    )
    assert early.ok is False and early.action == "too_early"
    assert early.checkout_after == "11:30"
    assert early.as_dict()["checkout_after"] == "11:30"
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["check_out"] is None and row["punch_out_photo"] is None and row["status"] == "present"

    allowed = attendance_service.mark_by_face(storage, settings, aarav, moment=at(11, 30))
    assert allowed.ok and allowed.action == "check_out"
    assert allowed.worked_minutes == 120


def test_check_out_opening_time_rounds_a_partial_minute_up(settings):
    assert attendance_service.checkout_opens_at(settings, "09:30:10", "09:31:00") == "11:31"
    assert attendance_service.checkout_opens_at(settings, "09:30:10", "11:30:10") is None


def test_hr_correction_is_not_limited_by_the_check_out_wait(storage: Storage, aarav):
    row = attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-05", status="half_day",
        check_in="09:30", check_out="10:00",
    )
    assert row["check_out"] == "10:00:00"


def _assign_shift(storage: Storage, employee: dict, *, start="14:00", end="22:00", grace=5, half_day=360) -> dict:
    shift_id = storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, "
        "is_default, created_at) VALUES ('Late Shift', ?, ?, ?, ?, 60, 0, '2026-01-01')",
        (start, end, grace, half_day),
    ).lastrowid
    storage.execute("UPDATE employees SET shift_id = ? WHERE id = ?", (shift_id, employee["id"]))
    return employees_service.get_employee(storage, employee["id"])


def test_shift_for_prefers_the_assigned_shift_then_the_company_setting(storage: Storage, aarav):
    storage.set_setting("shift_start", "10:00")
    assert orgtime.shift_for(storage, aarav)["start_time"] == "10:00"
    assert orgtime.shift_for(storage, _assign_shift(storage, aarav))["start_time"] == "14:00"


def test_late_and_half_day_follow_the_employees_own_shift(storage: Storage, settings, aarav):
    person = _assign_shift(storage, aarav)
    checked_in = attendance_service.mark_by_face(storage, settings, person, moment=at(14, 10))
    assert checked_in.late_minutes == 5
    # Five hours passes the company's 4 hour rule, but not this shift's 6 hours.
    assert attendance_service.mark_by_face(storage, settings, person, moment=at(19, 10)).status == "half_day"


def test_recalculate_employee_attendance_adjusts_past_records_for_new_shift(storage: Storage, settings, aarav):
    storage.set_setting("late_policy_mode", "tiered_fine_only")
    day = date(2026, 1, 5)
    row = attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date=day.isoformat(), status="present",
        check_in="10:00", check_out="18:30",
    )
    # Default shift 09:30 + 15m grace = 09:45. Check-in 10:00 is 15m late -> fine 50.0
    assert row["late_minutes"] == 15
    assert row["late_fine"] == 50.0

    shift_id = storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, "
        "is_default, created_at) VALUES ('11am Shift', '11:00', '17:00', 15, 360, 60, 0, '2026-01-01')",
    ).lastrowid
    storage.execute("UPDATE employees SET shift_id = ? WHERE id = ?", (shift_id, aarav["id"]))

    res = attendance_service.recalculate_employee_attendance(
        storage, aarav["id"], start_date=day, end_date=day, actor="test"
    )
    assert res["updated"] == 1
    updated_row = attendance_service.get_row(storage, aarav["id"], day)
    assert updated_row["late_minutes"] == 0
    assert updated_row["late_fine"] == 0.0
    assert updated_row["status"] == "present"


def test_list_employees_shift_filter_and_pagination(storage: Storage, aarav, isha):
    s1 = storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, is_default, created_at) "
        "VALUES ('Morning Shift', '09:00', '17:00', 15, 360, 60, 1, '2026-01-01')"
    ).lastrowid
    s2 = storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, is_default, created_at) "
        "VALUES ('Evening Shift', '14:00', '22:00', 15, 360, 60, 0, '2026-01-01')"
    ).lastrowid

    storage.execute("UPDATE employees SET shift_id = ? WHERE id = ?", (s1, aarav["id"]))
    storage.execute("UPDATE employees SET shift_id = ? WHERE id = ?", (s2, isha["id"]))

    all_emps = employees_service.list_employees(storage, shift_id=None)
    assert len(all_emps) >= 2
    assert any(e["id"] == aarav["id"] for e in all_emps)
    assert any(e["id"] == isha["id"] for e in all_emps)

    s2_emps = employees_service.list_employees(storage, shift_id=s2)
    assert len(s2_emps) == 1
    assert s2_emps[0]["id"] == isha["id"]
    assert s2_emps[0]["shift_name"] == "Evening Shift"
    assert employees_service.count_employees(storage, shift_id=s2) == 1

    s1_emps = employees_service.list_employees(storage, shift_id=s1)
    assert any(e["id"] == aarav["id"] for e in s1_emps)
    assert not any(e["id"] == isha["id"] for e in s1_emps)


def test_changing_the_company_shift_reaches_new_scans_and_registers(storage: Storage, settings, aarav):
    storage.set_setting("shift_start", "11:00")
    assert attendance_service.mark_by_face(storage, settings, aarav, moment=at(11, 5)).late_minutes == 0
    row = next(r for r in attendance_service.daily_rows(storage, settings, date(2026, 1, 5))
               if r["employee_id"] == aarav["id"])
    assert row["shift_start"] == "11:00"
    register = attendance_service.monthly_register(storage, settings, "2026-01")
    assert all(item["shift"]["start_time"] == "11:00" for item in register["rows"])


def test_third_scan_after_checkout_is_refused(storage: Storage, settings, aarav):
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    attendance_service.mark_by_face(storage, settings, aarav, moment=at(18, 30))
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(19, 30))
    assert outcome.ok is False and outcome.action == "already_done"
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["check_out"] == "18:30:00"  # not moved


def test_scan_on_leave_is_blocked(storage: Storage, settings, aarav):
    leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-05", to_date="2026-01-05", reason="family function",
    )
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    assert outcome.ok is False and outcome.action == "blocked"
    assert "leave" in outcome.message.lower()


def test_scan_on_week_off_is_recorded_as_worked(storage: Storage, settings, aarav):
    # 2026-01-04 is a Sunday (weekly off)
    outcome = attendance_service.mark_by_face(
        storage, settings, aarav, moment=datetime(2026, 1, 4, 10, 0)
    )
    assert outcome.ok and outcome.action == "check_in"
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 4))
    assert row["note"] == "worked on a weekly off"


def test_inactive_employee_cannot_mark(storage: Storage, settings, aarav):
    employees_service.set_status(storage, aarav["id"], "inactive")
    aarav = employees_service.get_employee(storage, aarav["id"])
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    assert outcome.ok is False and outcome.action == "inactive"


def test_face_scan_overrides_a_manual_absent(storage: Storage, settings, aarav):
    attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-05", status="absent", note="no show"
    )
    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(10, 0))
    assert outcome.ok and outcome.action == "check_in"
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["status"] == "present"
    assert "overrode" in row["note"]


def test_manual_mark_validates_times(storage: Storage, aarav):
    with pytest.raises(employees_service.ValidationError):
        attendance_service.mark_manual(
            storage, employee_id=aarav["id"], work_date="2026-01-05", status="present", check_in="9am"
        )


def test_manual_mark_computes_worked_minutes(storage: Storage, aarav):
    row = attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-06", status="present",
        check_in="09:15", check_out="18:00",
    )
    assert row["worked_minutes"] == 525
    assert row["late_minutes"] == 0
    assert row["source"] == "manual"


def test_clear_day_removes_the_row(storage: Storage, aarav):
    attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-06", status="present", check_in="09:30"
    )
    attendance_service.clear_day(storage, employee_id=aarav["id"], work_date="2026-01-06")
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 6)) is None


def test_month_summary_tallies_each_bucket(storage: Storage, settings, aarav):
    for day in (5, 6, 7):
        attendance_service.mark_manual(
            storage, employee_id=aarav["id"], work_date=f"2026-01-{day:02d}", status="present",
            check_in="09:30", check_out="18:30",
        )
    attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-08", status="half_day"
    )
    leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-09", to_date="2026-01-09",
    )
    leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="unpaid",
        from_date="2026-01-12", to_date="2026-01-12",
    )
    summary = attendance_service.employee_month_summary(storage, settings, aarav["id"], "2026-01")
    assert summary["present"] == 3
    assert summary["half_days"] == 1
    assert summary["paid_leave"] == 1
    assert summary["unpaid_leave"] == 1
    assert summary["week_off"] == 4
    assert summary["absent"] == 27 - (3 + 1 + 1 + 1)  # all other working days unmarked


def test_register_matrix_and_csv(storage: Storage, settings, staff):
    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date="2026-01-05", status="present",
        check_in="09:30", check_out="18:30",
    )
    register = attendance_service.monthly_register(storage, settings, "2026-01")
    assert len(register["days"]) == 31
    assert len(register["rows"]) == 3

    row = next(r for r in register["rows"] if r["employee"]["id"] == staff[0]["id"])
    assert row["totals"]["present"] == 1
    cell = next(c for c, d in zip(row["cells"], register["days"]) if d["iso"] == "2026-01-05")
    assert cell["code"] == "P"
    week_off_cell = next(c for c, d in zip(row["cells"], register["days"]) if d["iso"] == "2026-01-04")
    assert week_off_cell["code"] == "W"

    csv_text = attendance_service.register_csv(register)
    assert "Employee Code,Employee Name" in csv_text
    assert "Aarav Sharma" in csv_text
    assert len(csv_text.strip().splitlines()) == 3 * 31 + 1

    summary = attendance_service.summary_csv(register)
    assert "Payable Days" in summary
    assert len(summary.strip().splitlines()) == 4


def test_dashboard_counts_and_trend(storage: Storage, settings, staff):
    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date=orgtime.today_str(settings), status="present",
        check_in="09:30", check_out="18:30",
    )
    board = attendance_service.dashboard(storage, settings)
    assert board["counts"]["total"] == 3
    assert board["counts"]["present"] >= 1
    assert 0 <= board["counts"]["attendance_percent"] <= 100
    assert len(board["trend"]) == 7


# ----------------------------------------------------------------------- leave
def test_apply_full_day_leave_marks_attendance(storage: Storage, settings, aarav):
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-05", to_date="2026-01-07", reason="family function",
    )
    assert result["leave"]["days"] == 3
    assert len(result["applied"]) == 3
    assert len(result["skipped"]) == 0
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 6))
    assert row["status"] == "leave"
    assert row["leave_type"] == "paid"
    assert row["source"] == "leave"


def test_leave_skips_week_offs(storage: Storage, settings, aarav):
    # 2026-01-03 (Sat) .. 2026-01-05 (Mon) includes Sunday 4th
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-03", to_date="2026-01-05",
    )
    assert result["leave"]["days"] == 2  # Sunday not charged
    assert result["skipped"] == [{"date": "2026-01-04", "why": "weekly off"}]
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 4)) is None


def test_leave_skips_holidays(storage: Storage, settings, aarav):
    storage.execute("INSERT INTO holidays (day, name, created_at) VALUES ('2026-01-06', 'Company Day', 'now')")
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-05", to_date="2026-01-07",
    )
    assert result["leave"]["days"] == 2
    assert result["skipped"] == [{"date": "2026-01-06", "why": "holiday"}]


def test_half_day_leave(storage: Storage, settings, aarav):
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="casual",
        from_date="2026-01-05", half_day=True, reason="bank work",
    )
    assert result["leave"]["days"] == 0.5
    row = attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))
    assert row["status"] == "half_day"
    assert row["leave_type"] == "casual"


def test_half_day_leave_rejects_a_range(storage: Storage, settings, aarav):
    with pytest.raises(employees_service.ValidationError):
        leave_service.apply_leave(
            storage, settings, employee_id=aarav["id"], leave_type="paid",
            from_date="2026-01-05", to_date="2026-01-06", half_day=True,
        )


def test_leave_over_a_weekly_off_only_is_rejected(storage: Storage, settings, aarav):
    with pytest.raises(employees_service.ValidationError) as excinfo:
        leave_service.apply_leave(
            storage, settings, employee_id=aarav["id"], leave_type="paid",
            from_date="2026-01-04", to_date="2026-01-04",
        )
    assert "no working days" in str(excinfo.value)


def test_leave_does_not_clobber_an_existing_check_in(storage: Storage, settings, aarav):
    attendance_service.mark_manual(
        storage, employee_id=aarav["id"], work_date="2026-01-05", status="present",
        check_in="09:30", check_out="18:30",
    )
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-05", to_date="2026-01-06",
    )
    assert result["skipped"] == [{"date": "2026-01-05", "why": "already has a check-in"}]
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5))["status"] == "present"
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 6))["status"] == "leave"


def test_cancel_leave_removes_attendance_rows(storage: Storage, settings, aarav):
    result = leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="unpaid",
        from_date="2026-01-05", to_date="2026-01-06",
    )
    leave_id = result["leave"]["id"]
    leave_service.cancel_leave(storage, leave_id)
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 5)) is None
    assert attendance_service.get_row(storage, aarav["id"], date(2026, 1, 6)) is None
    assert storage.query_one("SELECT status FROM leave_requests WHERE id = ?", (leave_id,))["status"] == "cancelled"


def test_leave_balance_accrues_and_consumes(storage: Storage, settings, aarav):
    # joined 2025-04-01; as_of 2026-01-31 → 10 months of service
    balance = leave_service.balance(storage, settings, aarav["id"], as_of=date(2026, 1, 31))
    assert balance["months_of_service"] == 10
    assert balance["accrued"] == pytest.approx(15.0)  # 1.5 / month
    assert balance["used_paid"] == 0
    assert balance["balance"] == pytest.approx(15.0)

    leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="paid",
        from_date="2026-01-05", to_date="2026-01-06",
    )
    leave_service.apply_leave(
        storage, settings, employee_id=aarav["id"], leave_type="unpaid",
        from_date="2026-01-07", to_date="2026-01-07",
    )
    after = leave_service.balance(storage, settings, aarav["id"], as_of=date(2026, 1, 31))
    assert after["used_paid"] == 2
    assert after["used_unpaid"] == 1
    assert after["balance"] == pytest.approx(13.0)


def test_leave_list_filters_by_period_and_type(storage: Storage, settings, staff):
    leave_service.apply_leave(
        storage, settings, employee_id=staff[0]["id"], leave_type="sick",
        from_date="2026-01-05", to_date="2026-01-05",
    )
    leave_service.apply_leave(
        storage, settings, employee_id=staff[1]["id"], leave_type="unpaid",
        from_date="2026-02-05", to_date="2026-02-05",
    )
    january = leave_service.list_leave(storage, period="2026-01")
    assert len(january) == 1
    assert january[0]["type_label"] == "Sick leave"
    assert january[0]["is_paid"] is True

    assert len(leave_service.list_leave(storage, leave_type="unpaid")) == 1
    assert len(leave_service.list_leave(storage, employee_id=staff[0]["id"])) == 1

    summary = leave_service.summary_for_period(storage, "2026-01")
    assert summary[0]["sick"] == 1
    assert summary[0]["total"] == 1


# --------------------------------------------------------------------- payroll
def _fill_january(storage: Storage, settings, employee_id: int) -> None:
    """A hand-computable January 2026 for one employee (Sundays 4/11/18/25)."""
    present_days = [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23]
    for day in present_days:
        attendance_service.mark_manual(
            storage, employee_id=employee_id, work_date=f"2026-01-{day:02d}", status="present",
            check_in="09:30", check_out="18:30",
        )
    attendance_service.mark_manual(
        storage, employee_id=employee_id, work_date="2026-01-24", status="half_day"
    )
    leave_service.apply_leave(
        storage, settings, employee_id=employee_id, leave_type="paid",
        from_date="2026-01-26", to_date="2026-01-27",
    )
    leave_service.apply_leave(
        storage, settings, employee_id=employee_id, leave_type="unpaid",
        from_date="2026-01-28", to_date="2026-01-29",
    )
    # 30, 31 stay unmarked → absent


def test_compute_slip_maths(storage: Storage, settings, aarav):
    storage.set_setting("statutory_deductions_enabled", "yes")
    employees_service.set_salary(
        storage, aarav["id"], monthly_gross=31000, pf=1800, professional_tax=200,
        effective_from="2025-04-01", note="structure",
    )
    _fill_january(storage, settings, aarav["id"])
    aarav = employees_service.get_employee(storage, aarav["id"])
    calc = payroll.compute_slip(storage, settings, aarav, "2026-01")

    assert calc["basis_days"] == 31
    assert calc["present_days"] == 20.5
    assert calc["paid_leave_days"] == 2
    assert calc["absent_days"] == 2
    assert calc["unpaid_leave_days"] == 2
    # 2 absent + 2 unpaid leave + 0.5 for the half day without leave (PAY-01)
    assert calc["lop_days"] == 4.5
    assert calc["per_day_rate"] == pytest.approx(1000.0)
    assert calc["earned_gross"] == pytest.approx(26500.0)
    # PF follows earned wages (PAY-08): 1800 × 26500 / 31000; PT stays flat
    assert calc["pf"] == pytest.approx(1538.71)
    assert calc["total_deductions"] == pytest.approx(1738.71)
    assert calc["net_pay"] == 24761  # rounded to the rupee (PAY-09)


def test_payroll_basis_can_be_fixed_26_days(storage: Storage, settings, aarav):
    storage.set_setting("payroll_basis_days", "26")
    employees_service.set_salary(storage, aarav["id"], monthly_gross=26000, effective_from="2025-04-01")
    aarav = employees_service.get_employee(storage, aarav["id"])
    calc = payroll.compute_slip(storage, settings, aarav, "2026-01")
    assert calc["basis_days"] == 26
    assert calc["per_day_rate"] == pytest.approx(1000.0)


def test_late_marks_can_cost_half_a_day(storage: Storage, settings, aarav):
    employees_service.set_salary(storage, aarav["id"], monthly_gross=31000, effective_from="2025-04-01")
    storage.set_setting("late_marks_per_half_lop", "3")
    storage.set_setting("late_policy_mode", "lop_count_only")  # the count rule (plan v4 policies)
    for day in (5, 6, 7):  # three late arrivals
        attendance_service.mark_manual(
            storage, employee_id=aarav["id"], work_date=f"2026-01-{day:02d}", status="present",
            check_in="10:30", check_out="18:30",
        )
    aarav = employees_service.get_employee(storage, aarav["id"])
    calc = payroll.compute_slip(storage, settings, aarav, "2026-01")
    assert calc["late_marks"] == 3
    assert calc["late_lop_days"] == 0.5
    assert calc["lop_days"] >= 0.5


def test_late_rule_can_be_disabled(storage: Storage, settings, aarav):
    storage.set_setting("late_marks_per_half_lop", "0")
    for day in (5, 6, 7):
        attendance_service.mark_manual(
            storage, employee_id=aarav["id"], work_date=f"2026-01-{day:02d}", status="present",
            check_in="10:30", check_out="18:30",
        )
    aarav = employees_service.get_employee(storage, aarav["id"])
    calc = payroll.compute_slip(storage, settings, aarav, "2026-01")
    assert calc["late_lop_days"] == 0


def test_generate_run_creates_a_payslip_per_employee(storage: Storage, settings, staff):
    employees_service.set_salary(storage, staff[0]["id"], monthly_gross=31000, effective_from="2025-04-01")
    run = payroll.generate_run(storage, settings, "2026-01")
    assert run["status"] == "draft"
    slips = payroll.list_slips(storage, "2026-01")
    assert len(slips) == 3
    assert run["total_net"] == pytest.approx(sum(slip["net_pay"] for slip in slips))


def test_regenerating_replaces_previous_slips(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    first_ids = {slip["id"] for slip in payroll.list_slips(storage, "2026-01")}
    payroll.generate_run(storage, settings, "2026-01")
    second_ids = {slip["id"] for slip in payroll.list_slips(storage, "2026-01")}
    assert first_ids.isdisjoint(second_ids)
    assert len(second_ids) == 3


def test_adjustments_change_net_pay_and_totals(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    slip = payroll.list_slips(storage, "2026-01")[0]
    updated = payroll.update_slip(
        storage, slip["id"], bonus=5000, advance=2000, other_deduction=500, note="festival bonus"
    )
    assert updated["net_pay"] == pytest.approx(slip["earned_gross"] + 5000 - slip["pf"] - slip["esi"]
                                              - slip["professional_tax"] - 2500 + updated["rounding"])
    assert updated["note"] == "festival bonus"
    run = payroll.get_run(storage, "2026-01")
    assert run["total_net"] == pytest.approx(sum(s["net_pay"] for s in payroll.list_slips(storage, "2026-01")))


def test_negative_adjustment_is_rejected(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    slip = payroll.list_slips(storage, "2026-01")[0]
    with pytest.raises(employees_service.ValidationError):
        payroll.update_slip(storage, slip["id"], bonus=-100)


def test_finalized_run_is_locked_and_can_be_reopened(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    payroll.set_run_status(storage, "2026-01", "finalized")
    assert payroll.get_run(storage, "2026-01")["status"] == "finalized"

    with pytest.raises(employees_service.ValidationError):
        payroll.generate_run(storage, settings, "2026-01")

    slip = payroll.list_slips(storage, "2026-01")[0]
    with pytest.raises(employees_service.ValidationError):
        payroll.update_slip(storage, slip["id"], bonus=100)

    payroll.reopen_run(storage, "2026-01")
    payroll.generate_run(storage, settings, "2026-01")  # allowed again


def test_paid_run_cannot_change(storage: Storage, settings):
    payroll.generate_run(storage, settings, "2026-01")
    payroll.set_run_status(storage, "2026-01", "finalized")
    payroll.set_run_status(storage, "2026-01", "paid")
    with pytest.raises(employees_service.ValidationError):
        payroll.set_run_status(storage, "2026-01", "draft")
    with pytest.raises(employees_service.ValidationError):
        payroll.reopen_run(storage, "2026-01")


def test_payroll_csv_has_a_row_per_employee(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    csv_text = payroll.payroll_csv(storage, "2026-01")
    lines = csv_text.strip().splitlines()
    assert len(lines) == 4  # header + 3
    assert "Net Pay" in lines[0]
    assert "Aarav Sharma" in csv_text


def test_period_summary_totals(storage: Storage, settings, staff):
    payroll.generate_run(storage, settings, "2026-01")
    summary = payroll.period_summary(storage, "2026-01")
    assert summary["totals"]["employees"] == 3
    assert summary["totals"]["net"] == pytest.approx(sum(s["net_pay"] for s in summary["slips"]))


def test_inactive_employees_are_excluded_by_default(storage: Storage, settings, staff):
    employees_service.set_status(storage, staff[1]["id"], "inactive")
    payroll.generate_run(storage, settings, "2026-01")
    assert len(payroll.list_slips(storage, "2026-01")) == 2

    payroll.generate_run(storage, settings, "2026-01", include_inactive=True)
    assert len(payroll.list_slips(storage, "2026-01")) == 3


def test_salary_retention_fund_payroll_and_fnf(storage: Storage, settings):
    from app.services import exits as exits_service

    # Create new joiner with ₹30,000 Gross CTC and 20% retention for 3 months
    emp_id = employees_service.create_employee(
        storage,
        {
            "name": "Karan Mehra",
            "department": "Engineering",
            "designation": "Software Engineer",
            "joined_on": "2026-01-01",
            "monthly_gross": 30000.0,
            "retention_enabled": 1,
            "retention_percent": 20.0,
            "retention_months": 3,
        },
    )
    karan = employees_service.get_employee(storage, emp_id)
    assert karan["retention_enabled"] == 1
    assert karan["retention_percent"] == 20.0
    assert karan["retention_months"] == 3

    # Mark present for Jan 2026
    for day in range(1, 32):
        try:
            attendance_service.mark_manual(storage, employee_id=emp_id, work_date=f"2026-01-{day:02d}", status="present")
        except Exception:
            pass

    # Month 1: 2026-01 (Gross = ₹30,000, 20% = ₹6,000)
    slip1 = payroll.compute_slip(storage, settings, karan, "2026-01")
    assert slip1["monthly_gross"] == 30000.0
    assert slip1["retention_amount"] == 6000.0
    assert slip1["retention_month_num"] == 1
    assert slip1["total_deductions"] >= 6000.0

    # Generate payroll run for 2026-01
    payroll.generate_run(storage, settings, "2026-01")
    run1_slips = {s["employee_id"]: s for s in payroll.list_slips(storage, "2026-01")}
    assert run1_slips[emp_id]["retention_amount"] == 6000.0
    assert run1_slips[emp_id]["retention_month_num"] == 1

    # Mark present for Feb 2026
    for day in range(1, 29):
        try:
            attendance_service.mark_manual(storage, employee_id=emp_id, work_date=f"2026-02-{day:02d}", status="present")
        except Exception:
            pass

    # Month 2: 2026-02
    slip2 = payroll.compute_slip(storage, settings, karan, "2026-02")
    assert slip2["retention_amount"] == 6000.0
    assert slip2["retention_month_num"] == 2
    payroll.generate_run(storage, settings, "2026-02")

    # Mark present for March 2026, but with 3 days absent (LOP)
    for day in range(1, 32):
        try:
            st = "absent" if day <= 3 else "present"
            attendance_service.mark_manual(storage, employee_id=emp_id, work_date=f"2026-03-{day:02d}", status=st)
        except Exception:
            pass

    # Month 3: 2026-03 (Even with 3 days LOP, deduction remains fixed ₹6,000 based on Monthly Gross!)
    slip3 = payroll.compute_slip(storage, settings, karan, "2026-03")
    assert slip3["lop_days"] > 0
    assert slip3["earned_gross"] < 30000.0
    assert slip3["retention_amount"] == 6000.0  # Still fixed ₹6,000!
    assert slip3["retention_month_num"] == 3
    payroll.generate_run(storage, settings, "2026-03")

    # Month 4: 2026-04 (Retention period completed! Auto-stops to ₹0)
    for day in range(1, 31):
        try:
            attendance_service.mark_manual(storage, employee_id=emp_id, work_date=f"2026-04-{day:02d}", status="present")
        except Exception:
            pass
    slip4 = payroll.compute_slip(storage, settings, karan, "2026-04")
    assert slip4["retention_amount"] == 0.0
    assert slip4["retention_month_num"] == 0

    # Only retention that was really withheld (finalised runs) is refunded.
    for month in ("2026-01", "2026-02", "2026-03"):
        payroll.set_run_status(storage, month, "finalized", actor="hr")

    # Full & Final Settlement (F&F)
    # Karan resigns; verify F&F automatically refunds all 3 months of retention (₹18,000)
    exit_row = exits_service.record_exit(
        storage,
        settings,
        employee_id=emp_id,
        exit_type="resignation",
        resignation_date="2026-04-10",
        last_working_day="2026-04-30",
        reason="Career change",
    )
    fnf = exits_service.compute_fnf(storage, settings, exit_row)
    retention_refund = next((item for item in fnf["earnings"] if "Salary Retention Fund" in item["label"]), None)
    assert retention_refund is not None
    assert retention_refund["amount"] == 18000.0  # ₹6,000 * 3 months
    assert fnf["net"] > 18000.0



# ------------------------------------------------------------------- face glue
def test_face_service_identification_flags():
    from app.face import IdentificationResult

    result = IdentificationResult(
        matched=True, external_id="FITS001", similarity=0.812, face_id="face_1", threshold=0.363
    )
    assert result.matched and result.employee_code == "FITS001"


def test_kiosk_outcome_shape_is_json_friendly(storage: Storage, settings, aarav):
    import json

    outcome = attendance_service.mark_by_face(storage, settings, aarav, moment=at(9, 30))
    payload = json.dumps(outcome.as_dict())
    assert '"action": "check_in"' in payload
    assert "Aarav" in payload


def test_fake_face_service_contract(fake_face: FakeFaceService):
    assert fake_face.health()["ok"] is True
    result = fake_face.enrol_face(b"bytes", external_id="FITS001")
    assert result.face_id
    assert fake_face.list_faces()[0]["external_id"] == "FITS001"
    assert fake_face.delete_face(result.face_id) is True
    assert fake_face.list_faces() == []


# ------------------------------------------------------------------- WFH filters & stats
def test_wfh_employee_filtering_and_stats(storage: Storage, settings, aarav):
    # aarav has remote_work_enabled = 0 by default
    from app.services import employees as employees_service
    from app.services import employees_export

    # create a remote employee
    emp_remote_id = employees_service.create_employee(
        storage,
        {"code": "FITS999", "name": "Remote Worker", "status": "active", "department": "Tech", "monthly_gross": 30000},
    )
    storage.execute("UPDATE employees SET remote_work_enabled = 1 WHERE id = ?", (emp_remote_id,))

    # Check stats
    stats = employees_service.stats(storage)
    assert stats["remote_work"] >= 1

    # Check list filtering
    wfh_list = employees_service.list_employees(storage, work_mode="remote")
    wfh_ids = [e["id"] for e in wfh_list]
    assert emp_remote_id in wfh_ids
    assert aarav["id"] not in wfh_ids

    office_list = employees_service.list_employees(storage, work_mode="office")
    office_ids = [e["id"] for e in office_list]
    assert aarav["id"] in office_ids
    assert emp_remote_id not in office_ids

    # Check counts
    assert employees_service.count_employees(storage, work_mode="remote") == len(wfh_list)
    assert employees_service.count_employees(storage, work_mode="office") == len(office_list)

    # Check export dataset
    headers, rows = employees_export.get_employees_dataset(storage, settings, work_mode="remote")
    assert any("FITS999" in r for r in rows)
    assert not any(aarav["code"] in r for r in rows)

    # Check daily attendance rows flag
    day_rows = attendance_service.daily_rows(storage, settings)
    r_map = {r["employee_id"]: r for r in day_rows}
    assert r_map[emp_remote_id]["remote_work_enabled"] is True
    assert r_map[aarav["id"]]["remote_work_enabled"] is False


# ------------------------------------------------------------------- Fines & Deductions
def test_fine_tracking_and_waiver(storage: Storage, settings, aarav):
    from app.services import attendance as attendance_service

    # Mark attendance with a late fine
    row = attendance_service.mark_manual(
        storage,
        employee_id=aarav["id"],
        work_date="2026-11-10",
        status="present",
        check_in="10:45:00",
        check_out="18:30:00",
        late_fine=50.0,
    )
    assert row["late_fine"] == 50.0

    # Verify monthly register tracks cell.has_fine and totals.fines
    reg = attendance_service.monthly_register(storage, settings, "2026-11")
    aarav_row = next(r for r in reg["rows"] if r["employee"]["id"] == aarav["id"])
    day_10_cell = next(c for c, d in zip(aarav_row["cells"], reg["days"]) if d["iso"] == "2026-11-10")
    assert day_10_cell["has_fine"] is True
    assert day_10_cell["late_fine"] == 50.0
    assert aarav_row["totals"]["fines"] >= 50.0

    # Waive the fine via waive_fine
    waived = attendance_service.waive_fine(storage, employee_id=aarav["id"], work_date="2026-11-10", reason="Bike puncture approved")
    assert waived["late_fine"] == 0.0

    # Verify updated register shows no active fine
    reg_updated = attendance_service.monthly_register(storage, settings, "2026-11")
    aarav_row_updated = next(r for r in reg_updated["rows"] if r["employee"]["id"] == aarav["id"])
    day_10_updated = next(c for c, d in zip(aarav_row_updated["cells"], reg_updated["days"]) if d["iso"] == "2026-11-10")
    assert day_10_updated["has_fine"] is False
    assert day_10_updated["late_fine"] == 0.0


def test_deductions_crud_and_payroll_sync(storage: Storage, settings, aarav):
    from app.services import deductions as deductions_service
    from app.services import payroll as payroll_service

    period = "2026-11"
    # Add a damage recovery deduction
    did = deductions_service.add_deduction(
        storage,
        employee_id=aarav["id"],
        period=period,
        category="damage_recovery",
        amount=300.0,
        reason="Broken mouse & adapter replacement",
        actor="hr_admin",
    )
    assert did > 0

    # List deductions
    dlist = deductions_service.list_deductions(storage, period, employee_id=aarav["id"])
    assert len(dlist) == 1
    assert dlist[0]["amount"] == 300.0
    assert dlist[0]["category"] == "damage_recovery"

    # Stats for period
    stats = deductions_service.stats_for_period(storage, period)
    assert stats["total_active"] >= 300.0

    # Verify payroll integration in compute_slip
    slip = payroll_service.compute_slip(storage, settings, aarav, period)
    assert slip["other_deduction"] >= 300.0
    assert "Broken mouse" in (slip.get("other_deduction_note") or "")

    # Waive deduction
    waived = deductions_service.waive_deduction(storage, did, reason="Waived as first-time courtesy")
    assert waived["status"] == "waived"

    # Re-compute slip after waiver: other_deduction should no longer charge the 300
    slip_after_waiver = payroll_service.compute_slip(storage, settings, aarav, period)
    assert slip_after_waiver["other_deduction"] == 0.0


def test_individual_employee_shift_and_grace_flexibility(storage: Storage, settings):
    storage.set_setting("shift_start", "09:30")
    storage.set_setting("shift_end", "18:30")
    storage.set_setting("late_grace_minutes", "15")  # default grace is 15 mins (up to 09:45)
    storage.set_setting("late_policy_mode", "tiered_fine_only")
    storage.set_setting("late_slab_1_penalty", "50")

    # 1. Create normal employee and employee with custom 30 min grace
    emp1_id = employees_service.create_employee(
        storage,
        {"name": "Standard Employee", "code": "STD001", "monthly_gross": 30000, "joined_on": "2026-01-01"},
    )
    emp2_id = employees_service.create_employee(
        storage,
        {
            "name": "Flexible Employee",
            "code": "FLX001",
            "monthly_gross": 35000,
            "joined_on": "2026-01-01",
            "custom_grace_minutes": 35,  # 35 min grace (up to 10:05 allowed without fine)
            "custom_shift_start": "09:30",
            "custom_shift_end": "18:30",
        },
    )

    std_emp = employees_service.get_employee(storage, emp1_id)
    flx_emp = employees_service.get_employee(storage, emp2_id)

    # Verify shift_for correctly reflects custom settings
    shift_std = orgtime.shift_for(storage, std_emp)
    shift_flx = orgtime.shift_for(storage, flx_emp)

    assert shift_std["grace_minutes"] == 15
    assert not shift_std["is_custom_grace"]

    assert shift_flx["grace_minutes"] == 35
    assert shift_flx["is_custom_grace"]

    # 2. Both punch in at 09:55 (25 minutes after 09:30)
    work_day = date(2026, 11, 2)  # Monday (working day)
    
    # Standard employee: 09:55 is 10 mins past 15m grace -> 10 mins late, ₹50 fine
    outcome_std = attendance_service.mark_manual(
        storage,
        employee_id=emp1_id,
        work_date=work_day,
        status="present",
        check_in="09:55:00",
        check_out="18:30:00",
    )
    assert outcome_std["late_minutes"] == 10
    assert outcome_std["late_fine"] == 50.0

    # Flexible employee: 09:55 is within 35m grace -> 0 late mins, ₹0 fine
    outcome_flx = attendance_service.mark_manual(
        storage,
        employee_id=emp2_id,
        work_date=work_day,
        status="present",
        check_in="09:55:00",
        check_out="18:30:00",
    )
    assert outcome_flx["late_minutes"] == 0
    assert outcome_flx["late_fine"] == 0.0

    # 3. Custom shift timing override: change emp2 to start at 10:30
    employees_service.update_employee(
        storage,
        emp2_id,
        {"custom_shift_start": "10:30", "custom_shift_end": "19:30", "custom_grace_minutes": 10},
    )
    flx_emp_updated = employees_service.get_employee(storage, emp2_id)
    shift_flx_updated = orgtime.shift_for(storage, flx_emp_updated)
    assert shift_flx_updated["start_time"] == "10:30"
    assert shift_flx_updated["end_time"] == "19:30"
    assert shift_flx_updated["grace_minutes"] == 10
    assert shift_flx_updated["is_custom_shift"]
    assert shift_flx_updated["is_custom_grace"]

    # Punching in at 10:35 is now on-time for this employee (within 10m grace of 10:30)
    work_day_2 = date(2026, 11, 3)
    outcome_flx_2 = attendance_service.mark_manual(
        storage,
        employee_id=emp2_id,
        work_date=work_day_2,
        status="present",
        check_in="10:35:00",
        check_out="19:30:00",
    )
    assert outcome_flx_2["late_minutes"] == 0
    assert outcome_flx_2["late_fine"] == 0.0


def test_manager_leave_approval_logic(storage: Storage, settings) -> None:
    """Test manager status and badge logic for leave requests."""
    # Create manager and subordinate
    mgr_id = employees_service.create_employee(
        storage,
        {"name": "Manager Bob", "code": "MGR001", "monthly_gross": 60000, "joined_on": "2026-01-01"},
    )
    sub_id = employees_service.create_employee(
        storage,
        {"name": "Subordinate Alice", "code": "SUB001", "monthly_gross": 40000, "joined_on": "2026-01-01", "manager_id": mgr_id},
    )
    solo_id = employees_service.create_employee(
        storage,
        {"name": "Solo Charlie", "code": "SOLO001", "monthly_gross": 35000, "joined_on": "2026-01-01"},
    )

    # 1. Solo Charlie applies leave -> manager_status is 'na', status is 'pending'
    leave_solo = leave_service.apply_leave(
        storage, settings, employee_id=solo_id, leave_type="unpaid", from_date="2026-11-10", to_date="2026-11-10", status="pending"
    )["leave"]
    assert leave_solo["manager_status"] == "na"
    assert leave_solo["status"] == "pending"

    # 2. Subordinate Alice applies leave -> manager_status is 'pending', status is 'pending_manager'
    leave_sub = leave_service.apply_leave(
        storage, settings, employee_id=sub_id, leave_type="unpaid", from_date="2026-11-11", to_date="2026-11-11", status="pending"
    )["leave"]
    assert leave_sub["manager_status"] == "pending"
    assert leave_sub["status"] == "pending_manager"

    # 3. Manager Bob approves Alice's leave
    decided = leave_service.manager_decide_leave(
        storage, leave_sub["id"], mgr_id, decision="approved", note="Recommended by Bob"
    )
    assert decided["manager_status"] == "approved"
    assert decided["manager_action_by"] == mgr_id
    assert decided["status"] == "pending"

    # HR approves Alice's leave -> manager_status remains 'approved'
    approved_sub = leave_service.approve_leave(storage, leave_id=leave_sub["id"])["leave"]
    assert approved_sub["manager_status"] == "approved"
    assert approved_sub["status"] == "approved"

    # 4. HR directly approves a subordinate's leave without manager decision -> manager_status becomes 'bypassed_by_hr'
    leave_sub_2 = leave_service.apply_leave(
        storage, settings, employee_id=sub_id, leave_type="unpaid", from_date="2026-11-12", to_date="2026-11-12", status="pending"
    )["leave"]
    assert leave_sub_2["status"] == "pending_manager"
    approved_bypassed = leave_service.approve_leave(storage, leave_id=leave_sub_2["id"])["leave"]
    assert approved_bypassed["status"] == "approved"
    assert approved_bypassed["manager_status"] == "bypassed_by_hr"


def test_early_leave_attendance(storage: Storage, settings) -> None:
    """Test early leave waiver keeps full day present, 0 fine, and does not downgrade to half-day."""
    emp_id = employees_service.create_employee(
        storage,
        {"name": "Dev Priya", "code": "DEV001", "monthly_gross": 50000, "joined_on": "2026-01-01"},
    )
    work_day = "2026-11-04"  # Wednesday

    # Mark manual with early_leave=True: checked in at 09:30, checked out at 12:30 (worked 3 hours < 4 hours threshold)
    row = attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date=work_day,
        status="present",
        check_in="09:30:00",
        check_out="12:30:00",
        early_leave=True,
    )
    assert row["status"] == "present"
    assert row["early_leave_approved"] == 1
    assert row["late_fine"] == 0.0
    assert "Early Leave" in row["note"]

    # Verify employee_month_days reflects early_leave_approved
    emp = employees_service.get_employee(storage, emp_id)
    days = attendance_service.employee_month_days(storage, settings, emp, "2026-11")
    day_entry = next(d for d in days if d["iso"] == work_day)
    assert day_entry["early_leave_approved"] is True
    assert day_entry["status"] == "present"

    # Verify monthly register
    register = attendance_service.monthly_register(storage, settings, "2026-11")
    emp_row = next(r for r in register["rows"] if r["employee"]["id"] == emp_id)
    # November has 30 days, day 4 is index 3
    day_cell = emp_row["cells"][3]
    assert day_cell["early_leave_approved"] is True
    assert day_cell["code"] == "P"  # Full day Present!


def test_statutory_deductions_global_setting(storage: Storage, settings) -> None:
    """Test statutory deductions enabled/disabled global setting."""
    emp_id = employees_service.create_employee(
        storage,
        {"name": "Govind Joshi", "code": "GOV001", "monthly_gross": 20000, "joined_on": "2026-01-01"},
    )
    emp = employees_service.get_employee(storage, emp_id)
    # Give employee auto statutory mode
    employees_service.set_salary(
        storage, emp_id, monthly_gross=20000, basic=10000, statutory_mode="auto", professional_tax=200
    )

    # 1. By default, statutory_deductions_enabled is "no" -> 0 PF, 0 ESI, 0 PT
    calc = payroll.compute_slip(storage, settings, emp, "2026-11")
    assert calc["pf"] == 0.0
    assert calc["esi"] == 0.0
    assert calc["professional_tax"] == 0.0
    assert calc["total_deductions"] == 0.0

    # 2. When statutory_deductions_enabled is set to "yes" -> calculated
    storage.set_setting("statutory_deductions_enabled", "yes")
    calc_enabled = payroll.compute_slip(storage, settings, emp, "2026-11")
    assert calc_enabled["pf"] > 0
    assert calc_enabled["professional_tax"] == 200.0


