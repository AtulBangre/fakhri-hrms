"""Payslip PDF generation.

One A4 page per payslip, drawn with fpdf2 using the built-in Helvetica core font.
That means no font files to ship and identical output on macOS, Linux and inside
the container — at the cost of one substitution: the rupee sign is rendered as
``Rs`` because the core fonts are Latin-1 only. Everything else (amounts, dates,
attendance counts) is exact.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fpdf import FPDF

from .config import BRAND, Settings

#: 0x87 0x08 0x08 → the brand maroon, as an RGB tuple
MAROON = (0x87, 0x08, 0x08)
INK = (0x14, 0x14, 0x14)
MUTED = (0x73, 0x73, 0x73)
BORDER = (0xD8, 0xD3, 0xD0)
TINT = (0xFD, 0xF2, 0xF2)


def _money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"Rs {amount:,.2f}"


def _num(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{amount:g}" if amount % 1 else f"{amount:,.0f}"


def _text(value: Any) -> str:
    """Latin-1 safe text for the core font."""
    text = str(value or "")
    for source, target in (("₹", "Rs "), ("–", "-"), ("—", "-"), ("’", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(source, target)
    return text.encode("latin-1", "replace").decode("latin-1")


class PayslipPDF(FPDF):
    """Thin FPDF subclass that knows the company's colours."""

    def __init__(self, org_name: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.org_name = org_name
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(14, 14, 14)

    # -- building blocks ---------------------------------------------------
    def brand_bar(self) -> None:
        self.set_fill_color(*MAROON)
        self.rect(0, 0, 210, 24, style="F")
        self.set_xy(14, 6)
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(255, 255, 255)
        self.cell(0, 7, _text(self.org_name), new_x="LEFT", new_y="NEXT")
        self.set_x(14)
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, "Salary slip - computer generated", new_x="LEFT", new_y="NEXT")
        self.set_text_color(*INK)
        self.set_y(30)

    def section(self, title: str) -> None:
        self.ln(2)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*MAROON)
        self.cell(0, 6, _text(title.upper()), new_x="LEFT", new_y="NEXT")
        self.set_draw_color(*BORDER)
        y = self.get_y()
        self.line(14, y, 196, y)
        self.ln(2)
        self.set_text_color(*INK)

    def field(self, label: str, value: str, *, width: float = 91) -> None:
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*MUTED)
        self.cell(38, 6, _text(label), new_x="RIGHT")
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*INK)
        self.cell(width - 38, 6, _text(value), new_x="LEFT", new_y="NEXT")

    def amount_row(self, label: str, amount: str, *, bold: bool = False, tint: bool = False) -> None:
        if tint:
            self.set_fill_color(*TINT)
        self.set_font("Helvetica", "B" if bold else "", 9.5)
        self.set_text_color(*INK)
        self.cell(70, 7, _text(label), border="B", fill=tint, new_x="RIGHT")
        self.cell(25, 7, _text(amount), border="B", align="R", fill=tint, new_x="LEFT", new_y="NEXT")


def render_payslip(
    slip: dict[str, Any],
    *,
    settings: Settings,
    org_name: str,
    period_label: str,
    currency: str = "Rs",
) -> bytes:
    """Return the payslip PDF as bytes."""
    pdf = PayslipPDF(org_name)
    pdf.add_page()
    pdf.brand_bar()

    # -- title row ---------------------------------------------------------
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(120, 8, _text(f"Payslip - {period_label}"), new_x="RIGHT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 8, _text(f"Slip #{slip.get('id', '')}"), align="R", new_x="LEFT", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.ln(3)

    # -- employee ----------------------------------------------------------
    pdf.section("Employee")
    left = [
        ("Name", _text(slip.get("name") or "")),
        ("Employee code", _text(slip.get("code") or "")),
        ("Department", _text(slip.get("department") or "-")),
        ("Designation", _text(slip.get("designation") or "-")),
    ]
    right = [
        ("Date of joining", _text(slip.get("joined_on") or "-")),
        ("Payable days", f"{_num(slip.get('present_days'))} present + {_num(slip.get('paid_leave_days'))} paid leave"),
        ("Loss of pay days", _num(slip.get("lop_days"))),
        ("Per day rate", _money(slip.get("per_day_rate"))),
    ]
    start_y = pdf.get_y()
    pdf.set_x(14)
    for label, value in left:
        pdf.field(label, value)
    bottom_left = pdf.get_y()
    pdf.set_xy(109, start_y)
    for label, value in right:
        pdf.field(label, value, width=87)
    pdf.set_y(max(bottom_left, pdf.get_y()) + 1)

    # -- earnings / deductions --------------------------------------------
    pdf.section("Earnings and deductions")
    start_y = pdf.get_y()

    pdf.set_x(14)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(70, 7, "EARNING", new_x="RIGHT")
    pdf.cell(25, 7, "AMOUNT", align="R", new_x="LEFT", new_y="NEXT")
    pdf.set_text_color(*INK)
    lop_deduction = max(0.0, float(slip.get("monthly_gross") or 0) - float(slip.get("earned_gross") or 0))
    pdf.amount_row("Monthly gross", _money(slip.get("monthly_gross")))
    pdf.amount_row(f"Loss of pay ({_num(slip.get('lop_days'))} d)", "- " + _money(lop_deduction))
    pdf.amount_row("Earned gross", _money(slip.get("earned_gross")), bold=True, tint=True)
    pdf.amount_row("Bonus / incentive", _money(slip.get("bonus")))
    total_earnings = float(slip.get("earned_gross") or 0) + float(slip.get("bonus") or 0)
    pdf.amount_row("Total earnings", _money(total_earnings), bold=True)
    left_end = pdf.get_y()

    pdf.set_xy(109, start_y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*MUTED)
    pdf.cell(62, 7, "DEDUCTION", new_x="RIGHT")
    pdf.cell(25, 7, "AMOUNT", align="R", new_x="LEFT", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.amount_row("Provident fund (PF)", _money(slip.get("pf")))
    pdf.amount_row("ESI", _money(slip.get("esi")))
    pdf.amount_row("Professional tax", _money(slip.get("professional_tax")))
    pdf.amount_row("Salary advance", _money(slip.get("advance")))
    pdf.amount_row("Other deductions", _money(slip.get("other_deduction")))
    pdf.amount_row("Total deductions", _money(slip.get("total_deductions")), bold=True)
    pdf.set_y(max(left_end, pdf.get_y()) + 2)

    # -- net pay -----------------------------------------------------------
    pdf.ln(2)
    pdf.set_fill_color(*MAROON)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(120, 14, "  NET PAY", fill=True, new_x="RIGHT")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 14, _money(slip.get("net_pay")) + "  ", fill=True, align="R", new_x="LEFT", new_y="NEXT")
    pdf.set_text_color(*INK)

    if slip.get("note"):
        pdf.ln(3)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 5, _text(f"Note: {slip['note']}"))
        pdf.set_text_color(*INK)

    # -- footer ------------------------------------------------------------
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(
        0, 4.5,
        _text(
            "Net pay = earned gross + bonus - total deductions. "
            "This is a computer-generated payslip from the HRMS; errors and omissions excepted. "
            f"Generated on {date.today().strftime('%d %b %Y')}."
        ),
    )
    pdf.ln(8)
    pdf.set_draw_color(*BORDER)
    y = pdf.get_y()
    pdf.line(14, y, 80, y)
    pdf.line(130, y, 196, y)
    pdf.set_xy(14, y + 1)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(66, 5, "Employee signature", align="C", new_x="RIGHT")
    pdf.cell(50, 5, "", new_x="RIGHT")
    pdf.cell(66, 5, f"For {_text(org_name)}", align="C")

    return bytes(pdf.output())


def filename_for(slip: dict[str, Any]) -> str:
    code = str(slip.get("code") or "employee").replace(" ", "")
    period = str(slip.get("period") or "").replace("/", "-")
    return f"payslip-{code}-{period}.pdf"
