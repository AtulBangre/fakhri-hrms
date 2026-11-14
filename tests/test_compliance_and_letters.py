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


def test_candidate_offer_letter_and_onboarding_flow(storage, hr_client):
    storage._seed_letter_templates()
    token = csrf_of(hr_client, "/letters/generate?slug=offer")

    # 1. Generate Offer Letter for prospective candidate (who does not exist in DB)
    res_gen = hr_client.post(
        "/letters/download",
        data={
            "slug": "offer",
            "target_type": "candidate",
            "candidate_name": "Arun Kumar",
            "candidate_email": "arun.kumar@example.com",
            "candidate_phone": "9876543210",
            "candidate_role": "Lead DevOps Engineer",
            "candidate_dept": "Infrastructure",
            "candidate_gross": "85000",
            "candidate_joining": "2026-12-01",
            "probation_months": "6",
            "notice_period": "60",
            "subject": "Offer of Employment - Lead DevOps Engineer",
            "body_text": "Dear Arun Kumar,\n\nWe are pleased to offer you the position of Lead DevOps Engineer at Fakhri IT Services.",
            "_csrf": token,
        },
    )
    assert res_gen.status_code == 200
    assert res_gen.headers["content-type"] == "application/pdf"
    assert res_gen.content.startswith(b"%PDF-")

    # 2. Check generated_letters table has candidate record with employee_id = None
    gen_list = letters_service.list_generated_letters(storage)
    assert len(gen_list) >= 1
    cand_letter = next((l for l in gen_list if l["candidate_name"] == "Arun Kumar"), None)
    assert cand_letter is not None
    assert cand_letter["employee_id"] is None
    assert cand_letter["employee_code"] == "PROSPECT"
    assert cand_letter["employee_name"] == "Arun Kumar"

    # 3. Test direct download route by letter id
    res_pdf = hr_client.get(f"/letters/{cand_letter['id']}/pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.headers["content-type"] == "application/pdf"
    assert res_pdf.content.startswith(b"%PDF-")

    # 4. Test pre-fill onboarding form
    res_form = hr_client.get(f"/employees/new?from_letter_id={cand_letter['id']}")
    assert res_form.status_code == 200
    html = res_form.text
    assert "Arun Kumar" in html
    assert "Lead DevOps Engineer" in html
    assert "Infrastructure" in html
    assert "85000" in html

    # 5. Onboard Candidate as Employee
    emp_token = csrf_of(hr_client, f"/employees/new?from_letter_id={cand_letter['id']}")
    res_create = hr_client.post(
        "/employees",
        data={
            "name": "Arun Kumar",
            "code": "FSPL0099",
            "email": "arun.kumar@example.com",
            "phone": "+91 98765 43210",
            "designation": "Lead DevOps Engineer",
            "department": "Infrastructure",
            "joined_on": "2026-12-01",
            "monthly_gross": "85000",
            "employment_type": "full_time",
            "from_letter_id": str(cand_letter["id"]),
            "create_login": "0",
            "skip_documents": "1",
            "_csrf": emp_token,
        },
        follow_redirects=True,
    )
    assert res_create.status_code == 200

    # 6. Verify generated letter is now linked to the new employee
    updated_letter = letters_service.get_generated_letter(storage, cand_letter["id"])
    assert updated_letter["employee_id"] is not None

    # 7. Verify the Offer Letter is now registered in the Document Vault (`documents` table)
    from app.services import records
    docs = records.list_documents(storage, employee_id=updated_letter["employee_id"])
    offer_doc = next((d for d in docs if d["category"] == "offer_letter"), None)
    assert offer_doc is not None
    assert "Offer Letter" in offer_doc["title"]


