"""Tests for newly enhanced HRMS features:
1. Dynamic SL & CL settings in /settings
2. Employee leave application confirmation email to both HR and Employee
3. Shift Master: Multiple shifts create, make default, and delete
4. Individual employee custom leave quotas override
"""

from __future__ import annotations

from datetime import date
import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.config import SETTING_DEFS
from app.db import Storage
from app.services import attendance as attendance_service, employees as employees_service, leave as leave_service, orgtime
from tests.conftest import HR_PASSWORD, HR_USERNAME
from tests.helpers import csrf_of


def test_leave_quotas_in_settings_defs(client: TestClient, hr_client: TestClient, storage: Storage):
    """Verify sl_annual_quota and cl_annual_quota exist in SETTING_DEFS and can be updated."""
    keys = [defn[0] for defn in SETTING_DEFS]
    assert "sl_annual_quota" in keys
    assert "cl_annual_quota" in keys

    # Verify updating via /settings/value
    token = csrf_of(hr_client, "/settings")
    resp = hr_client.post("/settings/value", data={"_csrf": token, "key": "sl_annual_quota", "value": "10.0"}, follow_redirects=True)
    assert resp.status_code == 200
    assert storage.get_float("sl_annual_quota", 7.0) == 10.0

    resp2 = hr_client.post("/settings/value", data={"_csrf": token, "key": "cl_annual_quota", "value": "12.0"}, follow_redirects=True)
    assert resp2.status_code == 200
    assert storage.get_float("cl_annual_quota", 8.0) == 12.0


def test_shift_management_crud(client: TestClient, hr_client: TestClient, storage: Storage):
    """Verify creating, making default, and deleting shifts via /settings/shifts."""
    token = csrf_of(hr_client, "/settings")

    # 1. Create a new shift
    resp = hr_client.post("/settings/shifts", data={
        "_csrf": token,
        "name": "Evening Shift",
        "start_time": "14:00",
        "end_time": "22:30",
        "grace_minutes": 20,
        "half_day_minutes": 240,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Evening Shift" in resp.text

    created = storage.query_one("SELECT * FROM shifts WHERE name = 'Evening Shift'")
    assert created is not None
    assert created["start_time"] == "14:00"
    assert created["grace_minutes"] == 20
    assert created["is_default"] == 0

    # 2. Duplicate name validation
    resp_dup = hr_client.post("/settings/shifts", data={
        "_csrf": token,
        "name": "Evening Shift",
        "start_time": "14:00",
        "end_time": "22:30",
        "grace_minutes": 15,
        "half_day_minutes": 240,
    }, follow_redirects=True)
    assert "already exists" in resp_dup.text

    # 3. Make default
    resp_def = hr_client.post(f"/settings/shifts/{created['id']}/default", data={"_csrf": token}, follow_redirects=True)
    assert resp_def.status_code == 200
    assert storage.query_one("SELECT is_default FROM shifts WHERE id = ?", (created["id"],))["is_default"] == 1

    # 4. Cannot delete default shift
    resp_del_def = hr_client.post(f"/settings/shifts/{created['id']}/delete", data={"_csrf": token}, follow_redirects=True)
    assert "Cannot delete the default shift" in resp_del_def.text

    # 5. Set General Shift back to default
    gen = storage.query_one("SELECT id FROM shifts WHERE name = 'General Shift'")
    hr_client.post(f"/settings/shifts/{gen['id']}/default", data={"_csrf": token}, follow_redirects=True)

    # 6. Delete Evening Shift
    resp_del = hr_client.post(f"/settings/shifts/{created['id']}/delete", data={"_csrf": token}, follow_redirects=True)
    assert resp_del.status_code == 200
    assert storage.query_one("SELECT id FROM shifts WHERE name = 'Evening Shift'") is None


def test_leave_application_emails_both_hr_and_employee(client: TestClient, hr_client: TestClient, storage: Storage, settings):
    """Verify that when an employee applies for leave, emails are queued for BOTH HR and the employee."""
    # Ensure HR user has an email
    storage.execute("UPDATE users SET email = 'hr@fakhriitservices.com' WHERE role = 'hr'")

    emp_id = employees_service.create_employee(storage, {
        "name": "Rahul Verma",
        "code": "RV001",
        "email": "rahul.verma@example.com",
        "joined_on": "2026-01-01",
        "monthly_gross": 40000,
    })
    user_id = auth.create_user(storage, username="rahulv", password="Password123!", role="employee", employee_id=emp_id)

    emp_client = TestClient(client.app)
    login_resp = emp_client.post("/login", data={"username": "rahulv", "password": "Password123!"}, follow_redirects=False)
    assert login_resp.status_code == 303

    # Clear notifications table
    storage.execute("DELETE FROM notifications")

    # Rahul applies for leave
    token = csrf_of(emp_client, "/me/leave")
    apply_resp = emp_client.post("/me/leave", data={
        "_csrf": token,
        "leave_type": "paid",
        "from_date": "2026-11-10",
        "to_date": "2026-11-11",
        "reason": "Family function",
    }, follow_redirects=True)
    assert apply_resp.status_code == 200

    # Verify notifications queued
    notifs = storage.query("SELECT * FROM notifications ORDER BY id ASC")
    recipients = [n["recipient"] for n in notifs]

    # Both HR and Employee must receive email notifications
    assert "hr@fakhriitservices.com" in recipients
    assert "rahul.verma@example.com" in recipients

    emp_notif = next(n for n in notifs if n["recipient"] == "rahul.verma@example.com")
    assert "Leave Request Submitted" in emp_notif["subject"]
    assert "Rahul" in emp_notif["body"]
    assert "Family function" not in emp_notif["body"]  # clean summary for employee


def test_individual_employee_custom_leave_quota_override(storage: Storage, settings):
    """Verify that custom leave quotas override company defaults for specific employees."""
    # Default settings: PL = 1.5/mo, SL = 7.0/yr, CL = 8.0/yr
    storage.set_setting("pl_accrual_per_month", "1.5")
    storage.set_setting("sl_annual_quota", "7.0")
    storage.set_setting("cl_annual_quota", "8.0")

    # Employee 1: Standard employee (follows company defaults)
    emp1_id = employees_service.create_employee(storage, {
        "name": "Standard Emp",
        "code": "STD01",
        "joined_on": "2026-01-01",
        "monthly_gross": 30000,
    })

    # Employee 2: Executive with custom leave quota overrides
    emp2_id = employees_service.create_employee(storage, {
        "name": "Executive Emp",
        "code": "EXEC01",
        "joined_on": "2026-01-01",
        "monthly_gross": 90000,
        "custom_pl_accrual": 2.5,
        "custom_sl_quota": 12.0,
        "custom_cl_quota": 15.0,
    })

    as_of = date(2026, 7, 1) # 6 months of service

    b1 = leave_service.balance(storage, settings, emp1_id, as_of=as_of)
    b2 = leave_service.balance(storage, settings, emp2_id, as_of=as_of)

    # Standard employee: 7 months * 1.5 = 10.5 PL, 7.0 SL, 8.0 CL
    assert b1["accrual_per_month"] == 1.5
    assert b1["accrued"] == 10.5
    q1 = {q["code"]: q["allocated"] for q in b1["quotas"]}
    assert q1["PL"] == 10.5
    assert q1["SL"] == 7.0
    assert q1["CL"] == 8.0

    # Executive employee: 7 months * 2.5 = 17.5 PL, 12.0 SL, 15.0 CL
    assert b2["accrual_per_month"] == 2.5
    assert b2["accrued"] == 17.5
    q2 = {q["code"]: q["allocated"] for q in b2["quotas"]}
    assert q2["PL"] == 17.5
    assert q2["SL"] == 12.0
    assert q2["CL"] == 15.0


def test_email_reply_to_and_bcc_hr(storage: Storage, settings, tmp_path):
    """Verify outgoing emails carry proper Reply-To, Bcc to hr@fakhriitservices.com, and clean From."""
    from email import policy
    from email.parser import BytesParser
    from pathlib import Path

    settings.data_dir = tmp_path
    settings.smtp_host = ""  # console driver writes to outbox
    settings.smtp_from = '"Fakhri HRMS <k6263638053@gmail.com>"'
    settings.org_name = "Fakhri IT Services"

    row_id = notify.queue_email(
        storage, "emp@example.com", "welcome", settings=settings,
        category="welcome", name="Aman", username="aman01", password="SecretPassword1!",
    )
    assert row_id is not None
    notify.dispatch(storage, settings, limit=10)

    outbox = Path(settings.data_dir) / "outbox"
    eml_files = list(outbox.glob("*.eml"))
    assert len(eml_files) > 0

    msg = BytesParser(policy=policy.default).parsebytes(eml_files[-1].read_bytes())
    assert "Fakhri IT Services" in msg["From"]
    assert "k6263638053@gmail.com" in msg["From"]
    assert "Not Provided" not in msg["From"]
    assert "hr@fakhriitservices.com" in msg["Reply-To"]
    assert "hr@fakhriitservices.com" in msg["Bcc"]
    assert "https://hrms.fakhriitservices.com/login" in msg.get_body().get_content()


def test_attendance_clear_get_redirect(hr_client: TestClient):
    """Verify GET /attendance/clear safely redirects to /attendance without 405 error."""
    resp = hr_client.get("/attendance/clear", follow_redirects=False)
    assert resp.status_code == 303
    assert "/attendance" in resp.headers["location"]


def test_manual_time_update_promotes_late_half_day_to_present(storage: Storage):
    """Verify that when an employee was marked half-day due to late arrival,
    and HR edits check-in to an on-time hour, it automatically promotes to Present."""
    emp_id = employees_service.create_employee(storage, {
        "name": "Test Emp",
        "code": "TST001",
        "joined_on": "2026-01-01",
    })
    # Simulate a late scan that resulted in half_day
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, check_in, late_minutes, source, note, created_at, updated_at)
        VALUES (?, '2026-10-29', 'half_day', '17:59:00', 449, 'scan', 'late arrival — half day', '2026-10-29T12:00:00', '2026-10-29T12:00:00')
        """,
        (emp_id,)
    )

    # HR updates check-in time to 10:01 (on time) while form may have still had status='half_day'
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-29",
        status="half_day",
        check_in="10:01",
        note="late arrival — half day",
        actor="hr",
    )

    row = attendance_service.get_row(storage, emp_id, "2026-10-29")
    assert row["status"] == "present"
    assert row["check_in"] == "10:01:00"
    assert row["note"] is None


def test_pagarbook_attendance_rules(storage: Storage):
    """Verify PagarBook-style attendance rules:
    - Present with blank check-out leaves check_out and worked_minutes as None.
    - Absent clears check_in, check_out, worked_minutes, and leave_type.
    - On Leave clears check_in and check_out.
    - Manual override with HR note is not downgraded to half-day.
    """
    emp_id = employees_service.create_employee(storage, {
        "name": "PagarBook Emp",
        "code": "PGB001",
        "joined_on": "2026-01-01",
    })

    # 1. Present with no check-out (today / active shift)
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="present",
        check_in="10:07",
        check_out="",
        actor="hr",
    )
    r1 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r1["status"] == "present"
    assert r1["check_in"] == "10:07:00"
    assert r1["check_out"] is None
    assert r1["worked_minutes"] is None

    # 2. Marking absent clears times
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="absent",
        check_in="10:07",
        check_out="18:30",
        actor="hr",
    )
    r2 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r2["status"] == "absent"
    assert r2["check_in"] is None
    assert r2["check_out"] is None
    assert r2["worked_minutes"] is None
    assert r2["leave_type"] is None

    # 3. Marking on leave clears times and sets leave_type
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-31",
        status="leave",
        leave_type="sick",
        check_in="10:07",
        actor="hr",
    )
    r3 = attendance_service.get_row(storage, emp_id, "2026-10-31")
    assert r3["status"] == "leave"
    assert r3["check_in"] is None
    assert r3["check_out"] is None
    assert r3["leave_type"] == "sick"

    # 4. Manual override note protects Present from late half-day downgrade
    attendance_service.mark_manual(
        storage,
        employee_id=emp_id,
        work_date="2026-10-30",
        status="present",
        check_in="11:45",
        note="approved client visit",
        actor="hr",
    )
    r4 = attendance_service.get_row(storage, emp_id, "2026-10-30")
    assert r4["status"] == "present"
    assert r4["note"] == "approved client visit"


def test_bulk_employee_import_with_18_month_salary(storage: Storage, settings):
    """Verify bulk employee import:
    - Generates template with 18 salary columns.
    - Successfully creates employees and records only filled salary months.
    - Captures failed rows with clear error reasons.
    """
    from app.services import employees_bulk

    # 1. Template generation
    template = employees_bulk.generate_sample_csv(storage, settings)
    assert "name,code,joined_on" in template
    assert "salary_" in template

    # 2. Prepare test CSV
    csv_content = """name,code,joined_on,monthly_gross,department,designation,salary_2026_10,salary_2026_09,salary_2026_08,salary_2026_07
Amit Verma,BLKE001,2026-07-01,60000,Engineering,Lead Architect,60000,60000,60000,60000
Sneha Roy,BLKE002,2026-08-15,45000,Design,UI Designer,45000,45000,,
,BLKE003,2026-01-01,50000,Sales,Rep,50000,,,
"""
    result = employees_bulk.import_employees_csv(storage, settings, csv_content, actor="hr_test")
    assert result["total"] == 3
    assert result["success_count"] == 2
    assert result["failed_count"] == 1

    # Check Amit Verma (all 4 salary months filled)
    emp1 = employees_service.get_employee_by_code(storage, "BLKE001")
    assert emp1 is not None
    assert emp1["name"] == "Amit Verma"
    slips1 = storage.query("SELECT * FROM payslips WHERE employee_id = ? ORDER BY period", (emp1["id"],))
    assert len(slips1) == 4
    assert {s["period"] for s in slips1} == {"2026-07", "2026-08", "2026-09", "2026-10"}
    assert slips1[0]["net_pay"] == 60000.0

    # Check Sneha Roy (only 2 months filled: 2026-10 and 2026-09, empty months skipped)
    emp2 = employees_service.get_employee_by_code(storage, "BLKE002")
    assert emp2 is not None
    assert emp2["name"] == "Sneha Roy"
    slips2 = storage.query("SELECT * FROM payslips WHERE employee_id = ? ORDER BY period", (emp2["id"],))
    assert len(slips2) == 2
    assert {s["period"] for s in slips2} == {"2026-09", "2026-10"}
    assert slips2[0]["net_pay"] == 45000.0

    # Check Failed Row (missing name)
    failed = result["failed_rows"][0]
    assert failed["row"] == 4
    assert "name is required" in failed["error"].lower()


def test_bulk_upload_http_endpoints(hr_client: TestClient):
    """Verify HTTP GET /employees/bulk-template and POST /employees/bulk-upload."""
    # 1. Download template
    res = hr_client.get("/employees/bulk-template")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "salary_" in res.text

    # 2. Upload valid CSV with CSRF
    token = csrf_of(hr_client, "/employees")
    csv_bytes = b"name,code,joined_on,monthly_gross,salary_2026_10\nKiran Rao,BLKE099,2026-05-01,70000,70000\n"
    res_upload = hr_client.post(
        "/employees/bulk-upload",
        data={"_csrf": token},
        files={"file": ("test.csv", csv_bytes, "text/csv")},
    )
    assert res_upload.status_code == 200
    data = res_upload.json()
    assert data["ok"] is True
    assert data["success_count"] == 1
    assert data["failed_count"] == 0


def test_shift_and_settings_synchronization(hr_client: TestClient, storage: Storage):
    """Verify that shifts table and app_settings stay synchronized."""
    # 1. Update settings values (e.g. shift_start=10:00, shift_end=19:00)
    token = csrf_of(hr_client, "/settings")
    res = hr_client.post(
        "/settings/values",
        data={
            "_csrf": token,
            "shift_start": "10:00",
            "shift_end": "19:00",
            "late_grace_minutes": "20",
            "half_day_min_minutes": "250",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert storage.get_setting("shift_start") == "10:00"
    assert storage.get_setting("shift_end") == "19:00"

    # Verify default shift in shifts table was auto-updated!
    def_shift = storage.query_one("SELECT * FROM shifts WHERE is_default = 1")
    assert def_shift is not None
    assert def_shift["start_time"] == "10:00"
    assert def_shift["end_time"] == "19:00"
    assert def_shift["grace_minutes"] == 20
    assert def_shift["half_day_minutes"] == 250

    # 2. Test editing a shift
    res_edit = hr_client.post(
        f"/settings/shifts/{def_shift['id']}/edit",
        data={
            "_csrf": token,
            "name": "General Office Shift",
            "start_time": "10:15",
            "end_time": "18:45",
            "grace_minutes": "15",
            "half_day_minutes": "240",
        },
        follow_redirects=False,
    )
    assert res_edit.status_code == 303
    updated_shift = storage.query_one("SELECT * FROM shifts WHERE id = ?", (def_shift["id"],))
    assert updated_shift["name"] == "General Office Shift"
    assert updated_shift["start_time"] == "10:15"
    assert updated_shift["end_time"] == "18:45"
    # Since it was default, app_settings should also be updated
    assert storage.get_setting("shift_start") == "10:15"
    assert storage.get_setting("shift_end") == "18:45"

    # 3. Test creating a new non-default shift and making it default
    res_create = hr_client.post(
        "/settings/shifts",
        data={
            "_csrf": token,
            "name": "Evening Shift",
            "start_time": "14:00",
            "end_time": "22:00",
            "grace_minutes": "10",
            "half_day_minutes": "200",
        },
        follow_redirects=False,
    )
    assert res_create.status_code == 303
    evening_shift = storage.query_one("SELECT * FROM shifts WHERE name = 'Evening Shift'")
    assert evening_shift is not None

    res_default = hr_client.post(
        f"/settings/shifts/{evening_shift['id']}/default",
        data={"_csrf": token},
        follow_redirects=False,
    )
    assert res_default.status_code == 303
    assert storage.get_setting("shift_start") == "14:00"
    assert storage.get_setting("shift_end") == "22:00"

    # Switch back to General Shift as default
    hr_client.post(
        f"/settings/shifts/{def_shift['id']}/default",
        data={"_csrf": token},
        follow_redirects=False,
    )
    assert storage.get_setting("shift_start") == "10:15"


def test_employee_export_contains_all_details_with_dob_and_anniversary(
    hr_client: TestClient, storage: Storage, settings: Settings
):
    """Verify that export dataset contains all 52 employee fields including DOB and anniversary."""
    import io
    import zipfile
    from app.services import employees_export

    # 1. Create employee with DOB and Anniversary
    emp_id = employees_service.create_employee(
        storage,
        {
            "name": "Manish Verma",
            "code": "MV001",
            "joined_on": "2024-02-01",
            "monthly_gross": 65000,
            "department": "Engineering",
            "designation": "Backend Developer",
            "email": "manish.verma@example.com",
            "phone": "9876543219",
            "date_of_birth": "1994-08-25",
            "anniversary": "2021-11-14",
            "pan": "ABCDE1234F",
            "aadhaar": "234567890124",
            "bank_name": "HDFC Bank",
            "account_number": "50100234567890",
            "ifsc": "HDFC0001234",
            "account_holder": "Manish Verma",
        },
    )

    # 2. Check dataset directly
    headers, rows = employees_export.get_employees_dataset(storage, settings)
    assert "Date of Birth" in headers
    assert "Anniversary" in headers
    assert "Monthly Gross (₹)" in headers
    assert "Basic Salary (₹)" in headers
    assert "Paid Leave Balance" in headers

    # Find the created employee row
    mv_rows = [r for r in rows if r[headers.index("Employee Code")] == "MV001"]
    assert len(mv_rows) == 1
    mv_row = mv_rows[0]

    dob_idx = headers.index("Date of Birth")
    anniv_idx = headers.index("Anniversary")
    gross_idx = headers.index("Monthly Gross (₹)")

    assert mv_row[dob_idx] == "1994-08-25"
    assert mv_row[anniv_idx] == "2021-11-14"
    assert mv_row[gross_idx] == 65000.0

    # 3. Test HTTP GET /employees/export.xlsx
    res_xlsx = hr_client.get("/employees/export.xlsx")
    assert res_xlsx.status_code == 200
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in res_xlsx.headers["content-type"]
    assert 'filename="employees_all_details_' in res_xlsx.headers["content-disposition"]
    assert res_xlsx.content[:2] == b"PK"

    # Verify that the generated zip file is valid OpenXML spreadsheet
    with zipfile.ZipFile(io.BytesIO(res_xlsx.content)) as z:
        sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "Date of Birth" in sheet_xml
        assert "Anniversary" in sheet_xml
        assert "1994-08-25" in sheet_xml
        assert "2021-11-14" in sheet_xml
        assert "Manish Verma" in sheet_xml

    # 4. Test HTTP GET /employees/export.csv
    res_csv = hr_client.get("/employees/export.csv")
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.headers["content-type"]
    assert "Date of Birth" in res_csv.text
    assert "Anniversary" in res_csv.text
    assert "1994-08-25" in res_csv.text
    assert "2021-11-14" in res_csv.text


def test_leave_policy_toggle_fixed_vs_accrual(storage: Storage, settings):
    """Verify pl_policy_mode toggle between accrual and fixed upfront quota."""
    emp_id = employees_service.create_employee(storage, {
        "name": "Quota Test Emp",
        "code": "QT001",
        "joined_on": "2025-04-01",
        "monthly_gross": 40000,
    })

    as_of = date(2026, 6, 1)  # 15 months of service

    # 1. Accrual mode (0.5 per month -> 15 * 0.5 = 7.5 days)
    storage.set_setting("pl_policy_mode", "accrual")
    storage.set_setting("pl_accrual_per_month", "0.5")
    b_accrual = leave_service.balance(storage, settings, emp_id, as_of=as_of)
    assert b_accrual["accrued"] == 7.5
    assert b_accrual["pl_available"] == 7.5

    # 2. Fixed mode (6.0 annual quota upfront for the financial year)
    storage.set_setting("pl_policy_mode", "fixed")
    storage.set_setting("pl_annual_quota", "6.0")
    b_fixed = leave_service.balance(storage, settings, emp_id, as_of=as_of)
    assert b_fixed["accrued"] == 6.0
    assert b_fixed["pl_available"] == 6.0


def test_annual_quotas_financial_year_reset(storage: Storage, settings):
    """Verify sick and casual leave reset each financial year (April 1 - March 31)."""
    emp_id = employees_service.create_employee(storage, {
        "name": "Reset Test Emp",
        "code": "RST001",
        "joined_on": "2024-04-01",
        "monthly_gross": 40000,
    })
    storage.set_setting("sl_annual_quota", "6.0")
    storage.set_setting("cl_annual_quota", "6.0")

    # Record 6 sick leaves in previous FY (November 2024)
    storage.execute(
        """
        INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, status, created_at, decided_at)
        VALUES (?, 'sick', '2024-11-10', '2024-11-15', 6.0, 'approved', '2024-11-10T10:00:00', '2024-11-10T10:00:00')
        """,
        (emp_id,),
    )

    # In current FY (e.g. June 2026), full 6.0 SL should be available again!
    b_current = leave_service.balance(storage, settings, emp_id, as_of=date(2026, 6, 1))
    assert b_current["annual_sl"] == 6.0
    assert b_current["used_sl"] == 0.0
    assert b_current["sl_available"] == 6.0

    # In previous FY (November 2024), used_sl was 6.0 and sl_available was 0.0
    b_past = leave_service.balance(storage, settings, emp_id, as_of=date(2024, 11, 20))
    assert b_past["used_sl"] == 6.0
    assert b_past["sl_available"] == 0.0


def test_hr_backdated_leave_replaces_absent(storage: Storage, settings):
    """HR can apply backdated leave, cleanly replacing an absent record on past dates."""
    from app.services import attendance as attendance_service

    emp_id = employees_service.create_employee(storage, {
        "name": "Backdated Emp",
        "code": "BKD001",
        "joined_on": "2026-01-01",
        "monthly_gross": 30000,
    })

    past_date = date(2026, 5, 12)
    # Mark absent initially
    attendance_service.mark_manual(
        storage, employee_id=emp_id, work_date=past_date.isoformat(),
        status="absent", actor="hr",
    )
    row_before = attendance_service.get_row(storage, emp_id, past_date)
    assert row_before["status"] == "absent"

    # HR applies approved leave for that backdated day
    result = leave_service.apply_leave(
        storage, settings,
        employee_id=emp_id,
        leave_type="casual",
        from_date=past_date.isoformat(),
        to_date=past_date.isoformat(),
        status="approved",
        actor="hr",
        reason="Family emergency",
    )
    assert result["leave"]["status"] == "approved"

    # Attendance should now be 'leave'
    row_after = attendance_service.get_row(storage, emp_id, past_date)
    assert row_after["status"] == "leave"
    assert row_after["leave_type"] == "casual"








