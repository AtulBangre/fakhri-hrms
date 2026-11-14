"""Tests for Phase 1: Statutory Compliance (EPFO ECR, ESIC) and HR Letter Generator."""

from __future__ import annotations

import io
from datetime import date
import pytest
from fastapi.testclient import TestClient

from app.services import employees as employees_service
from app.services import letters as letters_service
from app.services import payroll as payroll_service


from tests.helpers import csrf_of


def test_uan_and_esic_ip_validation(storage, staff):
    emp = staff[0]
    
    # Valid UAN and ESIC IP
    employees_service.update_employee(
        storage,
        emp["id"],
        {"uan": "100123456789", "esic_ip": "1234567890"},
    )
    refreshed = employees_service.get_employee(storage, emp["id"])
    assert refreshed["uan"] == "100123456789"
    assert refreshed["esic_ip"] == "1234567890"

    # Invalid UAN (not 12 digits)
    with pytest.raises(employees_service.ValidationError, match="UAN must be a 12-digit number"):
        employees_service.update_employee(storage, emp["id"], {"uan": "12345"})

    # Invalid ESIC IP (too short)
    with pytest.raises(employees_service.ValidationError, match="ESIC Insurance Number must be 10"):
        employees_service.update_employee(storage, emp["id"], {"esic_ip": "12345"})


def test_epfo_ecr_and_esic_exports(storage, settings, staff):
    emp = staff[0]
    employees_service.update_employee(
        storage,
        emp["id"],
        {"uan": "100987654321", "esic_ip": "1122334455"},
    )

    period = "2026-06"
    payroll_service.generate_run(storage, settings, period)

    # EPFO ECR Export
    ecr_text = payroll_service.export_epfo_ecr(storage, period)
    assert "#~#" in ecr_text
    lines = [l for l in ecr_text.strip().split("\n") if l]
    assert len(lines) >= 1
    # Check that UAN and employee name appear in the line
    assert "100987654321" in ecr_text or emp["name"] in ecr_text

    # ESIC Return Export
    esic_csv = payroll_service.export_esic_return(storage, period)
    assert "IP Number,IP Name,No of Days Worked,Total Monthly Wages" in esic_csv
    assert "1122334455" in esic_csv or emp["name"] in esic_csv


def test_letters_service_and_pdf_generation(storage, staff):
    storage._seed_letter_templates()
    templates = letters_service.list_templates(storage)
    assert len(templates) >= 4
    slugs = [t["slug"] for t in templates]
    assert "offer" in slugs
    assert "appointment" in slugs
    assert "increment" in slugs
    assert "relieving" in slugs

    emp = staff[0]
    ctx = letters_service.build_employee_context(storage, emp["id"])
    assert ctx["employee_name"] == emp["name"]
    assert ctx["employee_code"] == emp["code"]

    offer_tmpl = letters_service.get_template(storage, "offer")
    rendered_body = letters_service.render_tokens(offer_tmpl["body_text"], ctx)
    assert emp["name"] in rendered_body
    assert "Offer" in offer_tmpl["title"]

    pdf_bytes = letters_service.render_letter_pdf(
        org_name="Test Company Pvt Ltd",
        ref_no="FITS/HR/OFFER/2026/001",
        letter_date="11 Nov 2026",
        subject="Offer of Employment",
        body_text=rendered_body,
    )
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500

    record_id = letters_service.record_generated_letter(
        storage,
        employee_id=emp["id"],
        template_slug="offer",
        title="Offer of Employment",
        pdf_filename="test_offer.pdf",
        created_by="hr_tester",
    )
    assert record_id > 0
    history = letters_service.list_generated_letters(storage, employee_id=emp["id"])
    assert len(history) == 1
    assert history[0]["template_slug"] == "offer"


def test_letters_web_routes(hr_client: TestClient, staff, settings, storage):
    # Test main letters page
    resp = hr_client.get("/letters")
    assert resp.status_code == 200
    assert "HR Letters &amp; Certificates" in resp.text or "HR Letters" in resp.text

    # Test generate page
    emp = staff[0]
    resp = hr_client.get(f"/letters/generate?slug=offer&employee_id={emp['id']}")
    assert resp.status_code == 200
    assert emp["name"] in resp.text

    # Test PDF download endpoint
    token = csrf_of(hr_client, "/letters/generate")
    resp = hr_client.post(
        "/letters/download",
        data={
            "_csrf": token,
            "slug": "offer",
            "employee_id": str(emp["id"]),
            "subject": "Offer of Employment",
            "body_text": f"Dear {emp['name']},\n\nWelcome to the company.\n\nSincerely,\nHR",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")

    # Generate payroll run so exports have data
    payroll_service.generate_run(storage, settings, "2026-06")

    # Test ESIC and EPFO download routes
    resp_ecr = hr_client.get("/payroll/export-ecr.txt?period=2026-06")
    assert resp_ecr.status_code == 200

    resp_esic = hr_client.get("/payroll/export-esic.csv?period=2026-06")
    assert resp_esic.status_code == 200
    assert "IP Number,IP Name" in resp_esic.text


def test_document_and_template_studio(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, "/letters/templates")

    # 1. Test create custom letter template
    res = hr_client.post(
        "/letters/templates/new",
        data={
            "title": "Warning Letter",
            "slug": "warning_letter",
            "subject": "Official Warning - {{employee_code}}",
            "body_text": "Dear {{employee_name}},\n\nThis is an official warning regarding attendance.",
            "_csrf": token,
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    custom_tmpl = letters_service.get_template(storage, "warning_letter")
    assert custom_tmpl is not None
    assert custom_tmpl["title"] == "Warning Letter"

    # 2. Test update custom letter template
    res_up = hr_client.post(
        "/letters/templates/warning_letter",
        data={
            "title": "Severe Warning Letter",
            "subject": "Final Warning - {{employee_code}}",
            "body_text": "Dear {{employee_name}},\n\nFinal disciplinary notice.",
            "_csrf": token,
        },
        follow_redirects=True,
    )
    assert res_up.status_code == 200
    updated = letters_service.get_template(storage, "warning_letter")
    assert updated["title"] == "Severe Warning Letter"

    # 3. Test delete custom letter template (allowed)
    res_del = hr_client.post(
        "/letters/templates/warning_letter/delete",
        data={"_csrf": token},
        follow_redirects=True,
    )
    assert res_del.status_code == 200
    assert letters_service.get_template(storage, "warning_letter") is None

    # 4. Test delete standard template (rejected)
    res_del_std = hr_client.post(
        "/letters/templates/offer/delete",
        data={"_csrf": token},
        follow_redirects=True,
    )
    assert res_del_std.status_code == 200
    assert letters_service.get_template(storage, "offer") is not None

    # 5. Test update payslip configuration
    res_ps = hr_client.post(
        "/letters/templates/payslip/update",
        data={
            "payslip_subtitle": "Confidential Monthly Statement",
            "payslip_brand_color": "#1e40af",
            "payslip_show_bank": "1",
            "payslip_show_attendance": "0",
            "payslip_footer_note": "Custom Advisory: Contact payroll@example.com for discrepancies.",
            "_csrf": token,
        },
        follow_redirects=True,
    )
    assert res_ps.status_code == 200
    assert storage.get_setting("payslip_subtitle") == "Confidential Monthly Statement"
    assert storage.get_setting("payslip_brand_color") == "#1e40af"
    assert storage.get_setting("payslip_show_bank") == "1"
    assert storage.get_setting("payslip_show_attendance") == "0"
    assert "Custom Advisory" in storage.get_setting("payslip_footer_note")

