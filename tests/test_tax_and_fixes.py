"""Week offs, TDS in payroll, the tax screens, pagination and dashboard status badges."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Storage
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import orgtime, payroll, tax
from tests.conftest import KIOSK_PASSWORD, KIOSK_USERNAME
from tests.helpers import csrf_of

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


# ------------------------------------------------------------------ week offs
def test_two_day_week_off(storage: Storage):
    storage.set_setting("week_off_days", "5,6")
    assert orgtime.is_week_off(storage, date(2026, 1, 3))   # Saturday
    assert orgtime.is_week_off(storage, date(2026, 1, 4))   # Sunday
    assert not orgtime.is_week_off(storage, date(2026, 1, 5))
    assert orgtime.working_days(storage, date(2026, 1, 5), date(2026, 1, 11)) == 5


def test_alternate_saturdays_off(storage: Storage):
    storage.set_setting("week_off_days", "6")
    storage.set_setting("saturday_off_weeks", "2,4")
    # January 2026 Saturdays: 3, 10, 17, 24, 31
    assert not orgtime.is_week_off(storage, date(2026, 1, 3))
    assert orgtime.is_week_off(storage, date(2026, 1, 10))
    assert not orgtime.is_week_off(storage, date(2026, 1, 17))
    assert orgtime.is_week_off(storage, date(2026, 1, 24))
    assert orgtime.week_off_label(storage) == "Sunday + 2nd & 4th Saturday"


def test_no_week_off_is_allowed(storage: Storage):
    storage.set_setting("week_off_days", "")
    assert not any(orgtime.is_week_off(storage, date(2026, 1, d)) for d in range(1, 8))


def test_week_off_checkboxes_save_from_settings(hr_client: TestClient, storage: Storage):
    token = csrf_of(hr_client, "/settings")
    response = hr_client.post(
        "/settings/value",
        data={"_csrf": token, "key": "week_off_days", "value_multi": ["6", "5"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert storage.get_setting("week_off_days") == "5,6"
    page = hr_client.get("/settings").text
    assert "Saturday + Sunday" in page


# ------------------------------------------------------------------- payroll TDS
def _confirm(storage, settings, employee_id: int, fy: str, annual: float) -> None:
    tax.ensure_profile(storage, settings, employee_id=employee_id, fy=fy)
    tax.confirm_tds(storage, employee_id=employee_id, fy=fy, annual_tds=annual, actor="hr")


def test_payroll_deducts_confirmed_tds(storage: Storage, settings, aarav):
    _confirm(storage, settings, aarav["id"], "2025-26", 12_000)
    calc = payroll.compute_slip(storage, settings, aarav, "2026-01")
    # January is month 10 of FY 2025-26: nothing deducted yet, 3 months left
    assert calc["tds"] == pytest.approx(4_000)
    assert calc["total_deductions"] == pytest.approx(calc["pf"] + calc["esi"] + calc["professional_tax"] + 4_000)

    payroll.generate_run(storage, settings, "2026-01")
    slip = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == aarav["id"])
    assert slip["tds"] == pytest.approx(4_000)
    assert slip["net_pay"] == pytest.approx(slip["earned_gross"] - slip["total_deductions"])

    # February spreads what is left over the remaining two months
    assert payroll.compute_slip(storage, settings, aarav, "2026-02")["tds"] == pytest.approx(4_000)
    assert tax.tds_deducted(storage, aarav["id"], "2025-26") == pytest.approx(4_000)


def test_no_tax_profile_means_no_tds(storage: Storage, settings, aarav):
    assert payroll.compute_slip(storage, settings, aarav, "2026-01")["tds"] == 0


def test_tds_can_be_overridden_on_a_draft_slip(storage: Storage, settings, aarav):
    _confirm(storage, settings, aarav["id"], "2025-26", 12_000)
    payroll.generate_run(storage, settings, "2026-01")
    slip = next(s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == aarav["id"])
    updated = payroll.update_slip(storage, slip["id"], tds=1_000, actor="hr")
    assert updated["tds"] == 1_000
    assert updated["total_deductions"] == pytest.approx(slip["total_deductions"] - 3_000)
    # leaving TDS out keeps the stored value
    again = payroll.update_slip(storage, slip["id"], bonus=500, actor="hr")
    assert again["tds"] == 1_000
    assert "TDS" in payroll.payroll_csv(storage, "2026-01").splitlines()[0]


# ------------------------------------------------------------------ tax screens
def test_hr_tax_pages_render(hr_client: TestClient, storage: Storage, settings, staff):
    assert hr_client.get("/tax").status_code == 200
    page = hr_client.get(f"/tax/employee/{staff[0]['id']}")
    assert page.status_code == 200
    assert "Old vs new regime" in page.text


def test_hr_confirms_tds_and_prints_form16(hr_client: TestClient, storage: Storage, settings, staff):
    employee_id = staff[0]["id"]
    fy = "2025-26"
    # Form 16 refuses until TDS is confirmed
    refused = hr_client.get(f"/tax/employee/{employee_id}/form16?fy={fy}", follow_redirects=False)
    assert refused.status_code == 303 and "err=" in refused.headers["location"]

    token = csrf_of(hr_client, "/tax")
    hr_client.post(f"/tax/employee/{employee_id}/confirm",
                   data={"_csrf": token, "fy": fy, "annual_tds": "24000"}, follow_redirects=False)
    assert tax.get_profile(storage, employee_id, fy)["confirmed_tds"] == 24_000

    form16 = hr_client.get(f"/tax/employee/{employee_id}/form16?fy={fy}")
    assert form16.status_code == 200
    assert "Form 16" in form16.text and staff[0]["name"] in form16.text


def test_tds_schedule_import(hr_client: TestClient, storage: Storage, staff):
    token = csrf_of(hr_client, "/tax")
    code = staff[1]["code"]
    response = hr_client.post(
        "/tax/import",
        data={"_csrf": token, "fy": "2025-26", "csv_text": f"code,annual_tds\n{code},18000\nNOPE,1\n"},
        follow_redirects=False,
    )
    assert "applied" in response.headers["location"]
    assert tax.get_profile(storage, staff[1]["id"], "2025-26")["confirmed_tds"] == 18_000


def test_employee_declares_with_proof_and_hr_verifies(
    ess_client: TestClient, storage: Storage, ess_credentials, hr_user
):
    employee_id = ess_credentials["employee"]["id"]
    page = ess_client.get("/me/tax")
    assert page.status_code == 200
    token = csrf_of(ess_client, "/me/tax")
    fy = tax.current_fy(ess_client.app.state.settings)

    response = ess_client.post(
        "/me/tax/declarations",
        data={"_csrf": token, "fy": fy, "section": "80C", "amount": "50000", "description": "PPF"},
        files={"proof": ("ppf.pdf", PDF_BYTES, "application/pdf")},
        follow_redirects=False,
    )
    assert "ok=" in response.headers["location"], response.headers["location"]
    rows = tax.list_declarations(storage, employee_id, fy)
    assert len(rows) == 1 and rows[0]["proof_name"]

    proof = ess_client.get(f"/tax/declarations/{rows[0]['id']}/proof")
    assert proof.status_code == 200 and proof.content.startswith(b"%PDF")

    ess_client.post("/me/tax/profile", data={"_csrf": token, "fy": fy, "regime": "old", "annual_rent": "120000"})
    assert tax.get_profile(storage, employee_id, fy)["regime"] == "old"

    # verified declarations are locked for the employee
    tax.verify_declaration(storage, rows[0]["id"], verified=True, actor="hr")
    blocked = ess_client.post(f"/me/tax/declarations/{rows[0]['id']}/delete",
                              data={"_csrf": token}, follow_redirects=False)
    assert "err=" in blocked.headers["location"]
    assert len(tax.list_declarations(storage, employee_id, fy)) == 1


def test_employee_cannot_touch_someone_elses_declaration(
    ess_client: TestClient, storage: Storage, settings, staff
):
    fy = tax.current_fy(settings)
    other = tax.add_declaration(storage, settings, employee_id=staff[1]["id"], fy=fy,
                                section="80D", amount=10_000, declared_by="hr")
    token = csrf_of(ess_client, "/me/tax")
    ess_client.post(f"/me/tax/declarations/{other['id']}/delete", data={"_csrf": token})
    assert tax.get_declaration(storage, other["id"])  # still there
    page = ess_client.get(f"/tax/declarations/{other['id']}/proof", follow_redirects=False)
    assert page.status_code == 303
    assert ess_client.get("/tax", follow_redirects=False).status_code == 303  # HR-only


# -------------------------------------------------------------------- ESS slip
def test_ess_payslip_pdf_button_uses_the_employee_route(
    ess_client: TestClient, storage: Storage, settings, ess_credentials
):
    payroll.generate_run(storage, settings, "2026-01")
    slip = next(s for s in payroll.list_slips(storage, "2026-01")
                if s["employee_id"] == ess_credentials["employee"]["id"])
    page = ess_client.get(f"/me/payslips/{slip['id']}")
    assert f'href="/me/payslips/{slip["id"]}.pdf"' in page.text
    assert f"/payroll/slip/{slip['id']}.pdf" not in page.text


# ------------------------------------------------------------------ pagination
def test_employee_list_pages(hr_client: TestClient, storage: Storage):
    for n in range(55):
        employees_service.create_employee(storage, {"name": f"Person {n:02d}", "department": "Ops",
                                                    "monthly_gross": 10000, "status": "active"})
    first = hr_client.get("/employees").text
    assert "Showing 1–50 of 55" in first and "page=2" in first
    second = hr_client.get("/employees?page=2").text
    assert "Showing 51–55 of 55" in second
    assert "Person 54" in second and "Person 00" not in second
    assert employees_service.count_employees(storage, department="Ops") == 55
    assert employees_service.count_employees(storage, department="Nope") == 0


def test_audit_log_pages_and_filters(hr_client: TestClient, storage: Storage):
    for n in range(120):
        storage.audit(actor="robot", action=f"bulk_{n}")
    page = hr_client.get("/audit").text
    assert "page=2" in page
    filtered = hr_client.get("/audit?q=bulk_119").text
    assert "bulk_119" in filtered and "bulk_118" not in filtered


# ---------------------------------------------------------- payroll warning
def test_payroll_warns_about_unmarked_days(hr_client: TestClient, storage: Storage, settings, staff):
    rows = attendance_service.unmarked_working_days(storage, settings, "2026-01")
    assert {row["id"] for row in rows} >= {staff[0]["id"]}
    page = hr_client.get("/payroll?period=2026-01").text
    assert "unmarked working day" in page


# ------------------------------------------------------------- kiosk badge
def test_dashboard_shows_kiosk_online(client: TestClient, storage: Storage, hr_user, kiosk_user, fake_face):
    client.post("/login", data={"username": KIOSK_USERNAME, "password": KIOSK_PASSWORD})
    assert client.get("/api/kiosk/status").status_code == 200
    devices = attendance_service.kiosk_devices(storage)
    assert devices and devices[0]["device"] == "Main Gate" and devices[0]["online"]

    client.cookies.clear()
    client.post("/login", data={"username": "hr", "password": "changeme123"})
    page = client.get("/dashboard").text
    assert "Main Gate" in page and "live-dot on" in page
