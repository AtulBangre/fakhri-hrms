"""HR Letter generation and templates: Offer, Appointment, Increment, Relieving."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from fpdf import FPDF

from ..config import Settings
from ..db import Storage, utcnow_iso
from . import employees as employees_service
from . import orgtime

# Official Brand Palette
MAROON = (0x87, 0x08, 0x08)
INK = (0x14, 0x14, 0x14)
MUTED = (0x55, 0x55, 0x55)
LIGHT_BG = (0xFD, 0xF2, 0xF2)
BORDER = (0xD8, 0xD3, 0xD0)


def list_templates(storage: Storage) -> list[dict[str, Any]]:
    return storage.query("SELECT * FROM letter_templates ORDER BY id ASC")


def get_template(storage: Storage, slug: str) -> dict[str, Any] | None:
    return storage.query_one("SELECT * FROM letter_templates WHERE slug = ?", (slug,))


def update_template(storage: Storage, slug: str, title: str, subject: str, body_text: str) -> None:
    storage.execute(
        "UPDATE letter_templates SET title = ?, subject = ?, body_text = ?, updated_at = ? WHERE slug = ?",
        (title.strip(), subject.strip(), body_text.strip(), utcnow_iso(), slug),
    )


def list_generated_letters(storage: Storage, employee_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if employee_id:
        return storage.query(
            "SELECT g.*, e.name AS employee_name, e.code AS employee_code "
            "FROM generated_letters g JOIN employees e ON e.id = g.employee_id "
            "WHERE g.employee_id = ? ORDER BY g.id DESC LIMIT ?",
            (employee_id, limit),
        )
    return storage.query(
        "SELECT g.*, e.name AS employee_name, e.code AS employee_code "
        "FROM generated_letters g JOIN employees e ON e.id = g.employee_id "
        "ORDER BY g.id DESC LIMIT ?",
        (limit,),
    )


def record_generated_letter(
    storage: Storage,
    *,
    employee_id: int,
    template_slug: str,
    title: str,
    pdf_filename: str,
    created_by: str,
) -> int:
    cursor = storage.execute(
        "INSERT INTO generated_letters (employee_id, template_slug, title, pdf_filename, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (employee_id, template_slug, title, pdf_filename, created_by, utcnow_iso()),
    )
    return int(cursor.lastrowid)


def build_employee_context(storage: Storage, employee_id: int, custom_data: dict[str, Any] | None = None) -> dict[str, str]:
    emp = employees_service.require_employee(storage, employee_id)
    settings = storage.settings
    org_name = storage.get_setting("org_name", settings.org_name) or "Fakhri IT Services"
    gross = float(emp.get("monthly_gross") or 0)
    annual = gross * 12
    today_formatted = date.today().strftime("%d %b %Y")

    ctx: dict[str, str] = {
        "company_name": org_name,
        "employee_name": emp.get("name") or "",
        "employee_code": emp.get("code") or "",
        "designation": emp.get("designation") or "Associate",
        "department": emp.get("department") or "General",
        "joined_on": emp.get("joined_on") or today_formatted,
        "monthly_gross": f"{gross:,.2f}",
        "annual_ctc": f"{annual:,.2f}",
        "relieving_date": today_formatted,
        "effective_date": today_formatted,
        "today": today_formatted,
        "probation_months": "3",
        "notice_period": storage.get_setting("notice_period_days", "30"),
    }
    if custom_data:
        for k, v in custom_data.items():
            if v is not None and str(v).strip():
                ctx[k] = str(v).strip()
    return ctx


def render_tokens(text: str, context: dict[str, str]) -> str:
    rendered = text
    for key, val in context.items():
        pattern = re.compile(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", re.IGNORECASE)
        rendered = pattern.sub(val, rendered)
    return rendered


class LetterPDF(FPDF):
    def __init__(self, org_name: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.org_name = org_name
        self.set_auto_page_break(auto=True, margin=20)

    def header(self) -> None:
        # Top Maroon accent bar
        self.set_fill_color(*MAROON)
        self.rect(0, 0, 210, 6, "F")

        # Company Header
        self.set_xy(18, 14)
        self.set_font("Helvetica", "B", 18)
        self.set_text_color(*MAROON)
        self.cell(174, 8, self.org_name, ln=True)

        self.set_xy(18, 22)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*MUTED)
        self.cell(174, 4, "Official Human Resources Communication", ln=True)

        self.set_draw_color(*BORDER)
        self.set_line_width(0.4)
        self.line(18, 29, 192, 29)
        self.ln(8)

    def footer(self) -> None:
        self.set_y(-18)
        self.set_draw_color(*BORDER)
        self.set_line_width(0.3)
        self.line(18, self.get_y(), 192, self.get_y())

        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(100, 6, f"{self.org_name} - Strictly Private & Confidential", align="L")
        self.cell(74, 6, f"Page {self.page_no()}", align="R")


def render_letter_pdf(
    *,
    org_name: str,
    ref_no: str,
    letter_date: str,
    subject: str,
    body_text: str,
) -> bytes:
    pdf = LetterPDF(org_name=org_name)
    pdf.add_page()
    pdf.set_left_margin(18)
    pdf.set_right_margin(18)

    # Date and Ref No Block
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(100, 6, f"Ref: {ref_no}", align="L")
    pdf.cell(74, 6, f"Date: {letter_date}", align="R", ln=True)
    pdf.ln(4)

    # Subject
    if subject:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*MAROON)
        pdf.multi_cell(174, 6, f"Subject: {subject}")
        pdf.ln(3)

    # Body paragraphs
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)

    paragraphs = body_text.replace("\r\n", "\n").split("\n\n")
    for para in paragraphs:
        cleaned = para.strip()
        if not cleaned:
            continue
        # Check if line-broken list
        lines = cleaned.split("\n")
        if len(lines) > 1 and any(l.strip().startswith(("-", "*", "1.", "2.", "3.", "4.", "5.", "6.")) for l in lines):
            for line in lines:
                l_strip = line.strip()
                if l_strip.startswith(("-", "*")):
                    pdf.set_x(24)
                    pdf.multi_cell(168, 5.5, chr(149) + " " + l_strip[1:].strip())
                elif re.match(r"^\d+\.", l_strip):
                    pdf.set_x(24)
                    pdf.multi_cell(168, 5.5, l_strip)
                else:
                    pdf.multi_cell(174, 5.5, line)
            pdf.ln(3)
        else:
            pdf.multi_cell(174, 5.5, cleaned)
            pdf.ln(3)

    return bytes(pdf.output())

