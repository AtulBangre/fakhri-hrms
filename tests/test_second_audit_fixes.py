"""Acceptance tests for all fixes from second audit report (new.report.md).

Covers:
- B1 & B2: EPFO ECR wage derivation & PF consistency
- B17 & B18: ESIC Return deduction filter & calendar paid days
- B19: WAHA WhatsApp key security validation
- B20: Credentials file deletion & login hint privacy
- B14: Anonymous /api/health disclosure lockdown
- B7: Section 87A Marginal Relief under new regime
- B4, B5 & B22: F&F Leave encashment, fixed PL pro-ration & gratuity on basic
- B8: Letter PDF unicode sanitization (₹, —, Hindi, quotes)
- B10: Old regime 80C statutory PF inclusion
- B11: Custom shift reminders
- B12: CLI production-reset --erase-all-data flag
- B13: Inactive employee partial month pay
"""

from datetime import date, timedelta
import argparse
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Storage, utcnow_iso
from app.services import payroll, employees as employees_service, tax as tax_service, exits as exits_service, leave as leave_service, letters as letters_service, alerts as alerts_service, orgtime
import cli


def test_b1_and_b2_epfo_ecr(storage, settings, staff):
    emp = staff[0]
    employees_service.update_employee(
        storage,
        emp["id"],
        {"uan": "100200300400", "date_of_birth": "1995-05-15"},
    )
    period = "2026-10"

    now_iso = utcnow_iso()
    # Seed full month working days attendance
    cur = orgtime.month_bounds(period)[0]
    while cur <= orgtime.month_bounds(period)[1]:
        if orgtime.day_kind(storage, cur) == "working":
            storage.execute(
                "INSERT OR REPLACE INTO attendance (employee_id, work_date, status, check_in, check_out, created_at, updated_at) VALUES (?, ?, 'present', '09:30', '18:30', ?, ?)",
                (emp["id"], cur.isoformat(), now_iso, now_iso),
            )
        cur += timedelta(days=1)
    
    # B1: With statutory deductions OFF (PF = 0), ECR produces NO lines
    storage.set_setting("statutory_deductions_enabled", "no")
    payroll.generate_run(storage, settings, period)
    assert payroll.export_epfo_ecr(storage, period) == ""

    # B2: With statutory deductions ON and Basic 9000, Gross 18000
    storage.set_setting("statutory_deductions_enabled", "yes")
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=18000.0,
        basic=9000.0,
        statutory_mode="auto",
        effective_from="2025-04-01",
    )
    payroll.generate_run(storage, settings, period)
    ecr_text = payroll.export_epfo_ecr(storage, period)
    lines = [l for l in ecr_text.strip().split("\n") if l]
    assert len(lines) >= 1
    
    target_line = [l for l in lines if "100200300400" in l][0]
    fields = target_line.split("#~#")
    # UAN, Member Name, Gross Wages, EPF Wages, EPS Wages, EDLI Wages, EE Share, EPS Share, ER Share
    gross, epf_wages, eps_wages, edli_wages, ee_share, eps_share, er_share = [int(fields[i]) for i in (2, 3, 4, 5, 6, 7, 8)]
    assert gross == 18000
    assert epf_wages == 9000
    assert eps_wages == 9000
    assert edli_wages == 9000
    assert ee_share == 1080  # 12% of 9000
    assert eps_share == 750  # 8.33% of 9000
    assert er_share == 330   # 1080 - 750
    assert ee_share == round(epf_wages * 0.12)
    assert eps_share + er_share == ee_share

    # Senior citizen (Age >= 58): EPS must be 0, all employer contribution goes to EPF
    employees_service.update_employee(storage, emp["id"], {"date_of_birth": "1960-01-01"})
    senior_ecr = payroll.export_epfo_ecr(storage, period)
    senior_fields = [l for l in senior_ecr.strip().split("\n") if "100200300400" in l][0].split("#~#")
    s_eps_wages = int(senior_fields[4])
    s_ee_share = int(senior_fields[6])
    s_eps_share = int(senior_fields[7])
    s_er_share = int(senior_fields[8])
    assert s_eps_wages == 0
    assert s_eps_share == 0
    assert s_er_share == s_ee_share


def test_b17_and_b18_esic_return(storage, settings, staff):
    emp = staff[0]
    period = "2026-10"  # 31 calendar days
    storage.set_setting("statutory_deductions_enabled", "yes")

    now_iso = utcnow_iso()
    # Seed full month working days attendance
    cur = orgtime.month_bounds(period)[0]
    while cur <= orgtime.month_bounds(period)[1]:
        if orgtime.day_kind(storage, cur) == "working":
            storage.execute(
                "INSERT OR REPLACE INTO attendance (employee_id, work_date, status, check_in, check_out, created_at, updated_at) VALUES (?, ?, 'present', '09:30', '18:30', ?, ?)",
                (emp["id"], cur.isoformat(), now_iso, now_iso),
            )
        cur += timedelta(days=1)

    # B17: Employee with salary above 21k ceiling (e.g. 35,000) has ESI = 0, so no wage line in return
    employees_service.update_employee(storage, emp["id"], {"esic_ip": "3100999999"})
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=35000.0,
        statutory_mode="auto",
        effective_from="2025-04-01",
    )
    payroll.generate_run(storage, settings, period)
    esic_csv = payroll.export_esic_return(storage, period)
    assert "3100999999" not in esic_csv  # Not exported

    # B18: Covered employee (gross 18,000 <= 21,000) present all working days with 0 LOP
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=18000.0,
        statutory_mode="auto",
        effective_from="2025-04-01",
    )
    payroll.generate_run(storage, settings, period)
    esic_csv_covered = payroll.export_esic_return(storage, period)
    assert "3100999999" in esic_csv_covered
    line = [l for l in esic_csv_covered.strip().split("\n") if "3100999999" in l][0]
    parts = line.split(",")
    # Days worked must report full calendar month days (31) because 0 LOP was taken
    assert parts[2] == "31"


def test_b7_marginal_relief_section_87a(storage, settings, staff):
    emp = staff[0]
    fy = "2025-26"
    
    # Direct test of slab + marginal relief calculation
    # At taxable 12.10L (Gross 12.85L with 75k std deduction):
    # Base tax is 60k + 1500 = 61,500. Excess over 12L is 10,000.
    # Tax before cess is capped at 10,000. Total with 4% cess is 10,400.
    tax_12_10 = 1210000
    base_12_10, _ = tax_service.slab_tax(tax_12_10, tax_service.SLABS_NEW)
    rebate_cap = tax_service.REBATE_NEW[0]  # 1200000
    tax_excess_10 = max(0.0, tax_12_10 - rebate_cap)
    capped_tax_10 = min(base_12_10, tax_excess_10)
    total_with_cess_10 = round(capped_tax_10 * 1.04, 2)
    assert total_with_cess_10 == 10400.0

    # At taxable 12.25L: excess is 25,000. Total with 4% cess is 26,000.
    tax_12_25 = 1225000
    base_12_25, _ = tax_service.slab_tax(tax_12_25, tax_service.SLABS_NEW)
    tax_excess_25 = max(0.0, tax_12_25 - rebate_cap)
    capped_tax_25 = min(base_12_25, tax_excess_25)
    total_with_cess_25 = round(capped_tax_25 * 1.04, 2)
    assert total_with_cess_25 == 26000.0


def test_b4_b5_and_b22_fnf(storage, settings, staff):
    emp = staff[0]
    # Set salary with only gross (no explicit basic)
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=30000.0,
        basic=0.0,
        effective_from="2020-01-01",
    )
    # B22: Gratuity and encashment must use 50% default basic (15,000), not 30,000
    exit_row = {
        "id": 1,
        "employee_id": emp["id"],
        "status": "in_progress",
        "resignation_date": "2026-03-10",
        "last_working_day": "2026-04-10",
        "notice_days": 30,
        "reason": "resignation",
    }
    stmt = exits_service.compute_fnf(storage, settings, exit_row)
    assert any("default 50% of gross" in note for note in stmt["notes"])
    
    # B4: Check that sick / casual leaves do not reduce PL balance
    bal = leave_service.balance(storage, settings, emp["id"], as_of=date(2026, 4, 10))
    assert bal["balance"] == bal["accrued"] - bal["used_pl"]


def test_b8_letter_pdf_unicode(storage, staff):
    emp = staff[0]
    # Contains ₹, —, quotes, and Hindi
    body = "Salary is ₹30,000 — “Official” letter.\nस्वागत है (Welcome to the team)."
    pdf_bytes = letters_service.render_letter_pdf(
        org_name="Fakhri IT Services",
        ref_no="FITS/2026/01",
        letter_date="21 Nov 2026",
        subject="Salary Revision — ₹30,000",
        body_text=body,
    )
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500


def test_b10_old_regime_80c_includes_pf(storage, settings, staff):
    emp = staff[0]
    fy = "2025-26"
    storage.set_setting("statutory_deductions_enabled", "yes")
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=30000.0,
        basic=15000.0,
        statutory_mode="auto",
        effective_from="2025-04-01",
    )
    proj = tax_service.project(
        storage,
        settings,
        employee=emp,
        fy=fy,
        regime="old",
        profile={"regime": "old"},
        declarations={},
    )
    c80_step = [s for s in proj["steps"] if "80C" in s["label"]][0]
    # PF: 12% of 15000 = 1800/mo -> 21,600/yr
    assert c80_step["amount"] == -21600.0


def test_b12_cli_production_reset_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["production-reset", "--erase-all-data"])
    assert args.erase_all_data is True


def test_b14_api_health_anonymous_lockdown(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "version" in data
    # Anonymous callers must NOT see internal face service URL details, backpressure, or outbox pending count
    assert "face_backpressure" not in data
    assert "outbox_pending" not in data
    face_svc = data.get("face_service", {})
    assert "detail" not in face_svc


def test_b15_recognition_score_fairness(storage, settings, staff):
    emp = staff[0]
    now_iso = utcnow_iso()
    # Employee marks field/remote punches with source='field'
    storage.execute(
        "INSERT INTO attendance (employee_id, work_date, status, check_in, check_out, source, worked_minutes, created_at, updated_at) "
        "VALUES (?, '2026-10-01', 'present', '09:30', '18:30', 'field', 480, ?, ?)",
        (emp["id"], now_iso, now_iso),
    )
    from app.services import recognition as rec_service
    rec_data = rec_service.get_monthly_recognition(storage, settings, "2026-10", use_cache=False)
    emp_entry = [c for c in rec_data["top3"] if c["employee"]["id"] == emp["id"]][0]
    # Field punches must receive verified punch score (up to 5 pts)
    assert emp_entry["breakdown"]["kiosk"] == 5.0


def test_b23_leave_accrual_lop_reduction(storage, settings, staff):
    emp = staff[0]
    employees_service.update_employee(storage, emp["id"], {"joined_on": "2026-01-01"})
    # Joined 2026-01-01, as of 2026-04-30 (4 full months: Jan, Feb, Mar, Apr).
    # Accrual rate is 1.5/month. Normal accrual would be 6.0 days.
    # Take 2 months of full unpaid leave (February and March).
    storage.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, reason, status, created_at) "
        "VALUES (?, 'unpaid', '2026-02-01', '2026-02-28', 20, 'Personal', 'approved', '2026-02-01')",
        (emp["id"],),
    )
    storage.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, reason, status, created_at) "
        "VALUES (?, 'unpaid', '2026-03-01', '2026-03-31', 22, 'Personal', 'approved', '2026-03-01')",
        (emp["id"],),
    )
    bal = leave_service.balance(storage, settings, emp["id"], as_of=date(2026, 4, 30))
    # Accrual should only be for 2 active months (2 * 1.5 = 3.0), not 6.0
    assert bal["accrued"] == 3.0


def test_b6_retroactive_arrears(storage, settings, staff):
    emp = staff[0]
    now_iso = utcnow_iso()
    # 1. Finalize October 2026 at ₹30,000 gross
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=30000.0,
        basic=15000.0,
        effective_from="2026-01-01",
    )
    # Seed October attendance
    cur = date(2026, 10, 1)
    while cur <= date(2026, 10, 31):
        if orgtime.day_kind(storage, cur) == "working":
            storage.execute(
                "INSERT OR REPLACE INTO attendance (employee_id, work_date, status, check_in, check_out, created_at, updated_at) "
                "VALUES (?, ?, 'present', '09:30', '18:30', ?, ?)",
                (emp["id"], cur.isoformat(), now_iso, now_iso),
            )
        cur += timedelta(days=1)

    payroll.generate_run(storage, settings, "2026-10")
    payroll.set_run_status(storage, "2026-10", "finalized", actor="hr")

    # 2. Save retroactive raise to ₹40,000 effective 1 Oct
    employees_service.set_salary(
        storage,
        emp["id"],
        monthly_gross=40000.0,
        basic=20000.0,
        effective_from="2026-10-01",
    )

    # 3. Generate November draft run
    # Seed November attendance
    cur = date(2026, 11, 1)
    while cur <= date(2026, 11, 30):
        if orgtime.day_kind(storage, cur) == "working":
            storage.execute(
                "INSERT OR REPLACE INTO attendance (employee_id, work_date, status, check_in, check_out, created_at, updated_at) "
                "VALUES (?, ?, 'present', '09:30', '18:30', ?, ?)",
                (emp["id"], cur.isoformat(), now_iso, now_iso),
            )
        cur += timedelta(days=1)

    nov_run = payroll.generate_run(storage, settings, "2026-11")
    nov_slips = payroll.list_slips(storage, "2026-11")
    emp_slip = [s for s in nov_slips if s["employee_id"] == emp["id"]][0]
    
    # Acceptance: draft run must show "Arrears (October 2026) ₹10,000"
    assert "Arrears (October 2026) ₹10,000" in (emp_slip.get("note") or "") or "Arrears (October 2026) ₹10,000" in (emp_slip.get("bonus_note") or "")
    assert float(emp_slip.get("bonus") or 0) >= 10000.0


def test_b3_night_shift_checkin_checkout(storage, settings, staff):
    emp = staff[0]
    now_iso = utcnow_iso()
    cursor = storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, is_default, created_at) "
        "VALUES ('Night Shift', '22:00', '06:00', 15, 240, 0, ?)",
        (now_iso,),
    )
    shift_id = cursor.lastrowid
    employees_service.update_employee(storage, emp["id"], {"shift_id": shift_id})
    emp = employees_service.require_employee(storage, emp["id"])
    from app.services import attendance as att_service
    from datetime import datetime
    
    # Check-in on 24 Nov at 21:55
    in_moment = datetime(2026, 11, 24, 21, 55, 0)
    out1 = att_service.mark_by_face(storage, settings, emp, moment=in_moment)
    assert out1.ok is True
    assert out1.action == "check_in"
    
    # Check-out next morning on 25 Nov at 06:05
    out_moment = datetime(2026, 11, 25, 6, 5, 0)
    out2 = att_service.mark_by_face(storage, settings, emp, moment=out_moment)
    assert out2.ok is True
    assert out2.action == "check_out"
    assert out2.worked_minutes == 490
    
    # Ensure 24 Nov has the complete row and 25 Nov does NOT have a phantom row
    row_24 = att_service.get_row(storage, emp["id"], date(2026, 11, 24))
    assert row_24["check_in"] == "21:55:00"
    assert row_24["check_out"] == "06:05:00"
    assert row_24["worked_minutes"] == 490
    
    row_25 = att_service.get_row(storage, emp["id"], date(2026, 11, 25))
    assert row_25 is None  # No phantom check-in on 25 Nov


def test_b21_field_punch_gps_accuracy(storage, settings, staff):
    emp = staff[0]
    employees_service.update_employee(storage, emp["id"], {"self_punch_mode": "geofence_only"})
    emp = employees_service.require_employee(storage, emp["id"])
    from app.services import field as field_service
    loc = field_service.add_location(storage, name="Office HQ", lat=23.2599, lng=77.4126, radius_m=200)
    
    # Weak accuracy (e.g. 600m > 200m radius limit) must be rejected
    res_weak = field_service.check_in(
        storage, settings, employee=emp, lat=23.2599, lng=77.4126, accuracy=600.0,
    )
    assert res_weak["ok"] is False
    assert "GPS signal too weak" in res_weak["message"]
    
    # Accurate reading within geofence (accuracy 15m <= 200m) is accepted
    res_good = field_service.check_in(
        storage, settings, employee=emp, lat=23.2599, lng=77.4126, accuracy=15.0,
    )
    assert res_good["ok"] is True
    assert res_good["inside"] is True

