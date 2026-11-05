"""Employee Data Export Service (Excel .xlsx and CSV formats).

Exports comprehensive employee details with zero omissions, including:
- Basic & profile identity (code, name, email, phone, DOB, anniversary, etc.)
- Organizational hierarchy (department, designation, employment type, manager)
- Statutory & banking (PAN, Aadhaar last 4, bank name, account number, IFSC)
- Shift & attendance rules (shift name, timing, punch modes, face enrolment)
- Current salary structure & statutory breakdown (gross, basic, HRA, PF, ESI, PT)
- Real-time leave balances (PL accrued/taken/balance, SL, CL, custom quotas)
- System & access status (portal username, portal status, last login, notes, timestamps)
"""

from __future__ import annotations

import csv
import io
import re
import xml.sax.saxutils
import zipfile
from datetime import date, datetime
from typing import Any

from ..config import Settings
from ..db import Storage
from ..services import leave as leave_service
from ..services import orgtime


EXPORT_COLUMNS: list[tuple[str, str, str]] = [
    # (Header Title, Field Key, Data Type: 'text' | 'number' | 'date')
    ("Employee ID", "id", "number"),
    ("Employee Code", "code", "text"),
    ("Full Name", "name", "text"),
    ("Email", "email", "text"),
    ("Phone", "phone", "text"),
    ("Date of Birth", "date_of_birth", "date"),
    ("Anniversary", "anniversary", "date"),
    ("Department", "department", "text"),
    ("Designation", "designation", "text"),
    ("Date of Joining", "joined_on", "date"),
    ("Employment Type", "employment_type", "text"),
    ("Status", "status", "text"),
    ("Monthly Gross (₹)", "monthly_gross", "number"),
    ("Basic Salary (₹)", "basic", "number"),
    ("HRA (₹)", "hra", "number"),
    ("Other Allowances (₹)", "other_allowances", "number"),
    ("PF Deduction (₹)", "pf", "number"),
    ("ESI Deduction (₹)", "esi", "number"),
    ("Professional Tax (₹)", "professional_tax", "number"),
    ("Statutory Mode", "statutory_mode", "text"),
    ("Salary Effective Date", "salary_effective_from", "date"),
    ("Address", "address", "text"),
    ("Emergency Contact", "emergency_contact", "text"),
    ("PAN", "pan", "text"),
    ("Aadhaar (Last 4)", "aadhaar_last4", "text"),
    ("Bank Name", "bank_name", "text"),
    ("Account Holder Name", "account_holder", "text"),
    ("Bank Account Number", "account_number", "text"),
    ("IFSC Code", "ifsc", "text"),
    ("Assigned Shift", "shift_name", "text"),
    ("Shift Timing", "shift_timing", "text"),
    ("Self Punch Mode", "self_punch_mode", "text"),
    ("Face Enrolled", "face_enrolled", "text"),
    ("Face Enrolled Date", "face_enrolled_at", "text"),
    ("Reporting Manager Name", "manager_name", "text"),
    ("Reporting Manager Code", "manager_code", "text"),
    ("Months of Service", "months_of_service", "number"),
    ("Paid Leave Accrued", "pl_accrued", "number"),
    ("Paid Leave Taken", "pl_taken", "number"),
    ("Paid Leave Balance", "pl_balance", "number"),
    ("Sick Leave Balance", "sl_balance", "number"),
    ("Casual Leave Balance", "cl_balance", "number"),
    ("Custom PL Accrual / mo", "custom_pl_accrual", "number"),
    ("Custom SL Annual Quota", "custom_sl_quota", "number"),
    ("Custom CL Annual Quota", "custom_cl_quota", "number"),
    ("Income Tax Regime", "tax_regime", "text"),
    ("Portal Username", "portal_username", "text"),
    ("Portal Account Status", "portal_active", "text"),
    ("Last Portal Sign-in", "portal_last_login", "text"),
    ("Notes", "notes", "text"),
    ("Profile Created At", "created_at", "text"),
    ("Profile Updated At", "updated_at", "text"),
]


def _col_letter(n: int) -> str:
    """Convert zero-based column index to Excel column letter (e.g. 0->A, 25->Z, 26->AA)."""
    res = ""
    while n >= 0:
        res = chr(n % 26 + 65) + res
        n = n // 26 - 1
    return res


def get_employees_dataset(
    storage: Storage,
    settings: Settings,
    *,
    q: str = "",
    department: str = "",
    status: str = "all",
) -> tuple[list[str], list[list[Any]]]:
    """Compile exhaustive dataset of all employees with linked salary, leave, shift and portal data."""
    # 1. Base query with optional filters
    sql = "SELECT * FROM employees WHERE 1=1"
    params: list[Any] = []
    if status == "active":
        sql += " AND status = 'active'"
    elif status == "inactive":
        sql += " AND status = 'inactive'"

    if department:
        sql += " AND department = ?"
        params.append(department)

    if q:
        sql += " AND (name LIKE ? OR code LIKE ? OR phone LIKE ? OR email LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])

    sql += " ORDER BY code ASC"
    employees = storage.query(sql, tuple(params))

    # 2. Batch load related reference datasets for maximum efficiency
    shifts_by_id = {s["id"]: s for s in storage.query("SELECT * FROM shifts")}
    default_shift = storage.query_one("SELECT * FROM shifts WHERE is_default = 1")
    company_shift_start = storage.get_setting("shift_start", "09:30")
    company_shift_end = storage.get_setting("shift_end", "18:30")

    users_by_emp_id = {
        u["employee_id"]: u
        for u in storage.query("SELECT id, username, active, last_login_at, employee_id FROM users WHERE employee_id IS NOT NULL")
    }

    # Latest salary revisions for all employees
    salaries_by_emp_id: dict[int, dict[str, Any]] = {}
    for srev in storage.query("SELECT * FROM salary_revisions ORDER BY effective_from ASC, id ASC"):
        salaries_by_emp_id[srev["employee_id"]] = srev

    # Reporting managers map
    all_emps_by_id = {e["id"]: e for e in storage.query("SELECT id, code, name FROM employees")}

    # Tax profiles
    tax_by_emp_id = {
        t["employee_id"]: t
        for t in storage.query("SELECT employee_id, regime FROM tax_profiles ORDER BY id ASC")
    }

    headers = [col[0] for col in EXPORT_COLUMNS]
    rows: list[list[Any]] = []

    today_date = orgtime.today(settings)

    for emp in employees:
        emp_id = emp["id"]

        # Shift info
        shift = shifts_by_id.get(emp.get("shift_id")) if emp.get("shift_id") else default_shift
        if shift:
            shift_name = shift["name"]
            shift_timing = f"{shift['start_time']} – {shift['end_time']}"
        else:
            shift_name = "Company Default"
            shift_timing = f"{company_shift_start} – {company_shift_end}"

        # Salary revision
        srev = salaries_by_emp_id.get(emp_id, {})
        basic = float(srev.get("basic") or 0)
        hra = float(srev.get("hra") or 0)
        other_allowances = float(srev.get("other_allowances") or 0)
        pf = float(srev.get("pf") or 0)
        esi = float(srev.get("esi") or 0)
        pt = float(srev.get("professional_tax") or 0)
        statutory_mode = (srev.get("statutory_mode") or "auto").capitalize()
        salary_effective_from = srev.get("effective_from") or ""

        # Leave balances
        try:
            bal = leave_service.balance(storage, settings, emp_id, as_of=today_date)
            months_of_service = bal.get("months_of_service", 0)
            pl_accrued = bal.get("accrued", 0.0)
            pl_taken = bal.get("used_paid", 0.0)
            pl_balance = bal.get("balance", 0.0)
            sl_balance = bal.get("sl_balance", 0.0)
            cl_balance = bal.get("cl_balance", 0.0)
        except Exception:
            months_of_service = 0
            pl_accrued = 0.0
            pl_taken = 0.0
            pl_balance = 0.0
            sl_balance = 0.0
            cl_balance = 0.0

        # Reporting manager
        mgr = all_emps_by_id.get(emp.get("manager_id")) if emp.get("manager_id") else None
        manager_name = mgr["name"] if mgr else ""
        manager_code = mgr["code"] if mgr else ""

        # Portal user
        u = users_by_emp_id.get(emp_id)
        portal_username = u["username"] if u else ""
        portal_active = ("Active" if u["active"] else "Disabled") if u else "Not Enrolled"
        portal_last_login = (u.get("last_login_at") or "")[:19].replace("T", " ") if u and u.get("last_login_at") else "Never"

        # Tax regime
        tax_regime = (tax_by_emp_id.get(emp_id, {}).get("regime") or "new").capitalize()

        # Face enrolled
        face_enrolled = "Yes" if emp.get("face_id") else "No"
        face_enrolled_at = (emp.get("face_enrolled_at") or "")[:19].replace("T", " ")

        # Clean timestamps
        created_at = (emp.get("created_at") or "")[:19].replace("T", " ")
        updated_at = (emp.get("updated_at") or "")[:19].replace("T", " ")

        data_dict: dict[str, Any] = {
            "id": emp["id"],
            "code": emp["code"],
            "name": emp["name"],
            "email": emp.get("email") or "",
            "phone": emp.get("phone") or "",
            "date_of_birth": emp.get("date_of_birth") or "",
            "anniversary": emp.get("anniversary") or "",
            "department": emp.get("department") or "",
            "designation": emp.get("designation") or "",
            "joined_on": emp.get("joined_on") or "",
            "employment_type": (emp.get("employment_type") or "full_time").replace("_", " ").title(),
            "status": (emp.get("status") or "active").capitalize(),
            "monthly_gross": float(emp.get("monthly_gross") or 0),
            "basic": basic,
            "hra": hra,
            "other_allowances": other_allowances,
            "pf": pf,
            "esi": esi,
            "professional_tax": pt,
            "statutory_mode": statutory_mode,
            "salary_effective_from": salary_effective_from,
            "address": emp.get("address") or "",
            "emergency_contact": emp.get("emergency_contact") or "",
            "pan": emp.get("pan") or "",
            "aadhaar_last4": f"XXXX-XXXX-{emp['aadhaar_last4']}" if emp.get("aadhaar_last4") else "",
            "bank_name": emp.get("bank_name") or "",
            "account_holder": emp.get("account_holder") or "",
            "account_number": str(emp.get("account_number") or ""),
            "ifsc": emp.get("ifsc") or "",
            "shift_name": shift_name,
            "shift_timing": shift_timing,
            "self_punch_mode": (emp.get("self_punch_mode") or "inherit").replace("_", " ").title(),
            "face_enrolled": face_enrolled,
            "face_enrolled_at": face_enrolled_at,
            "manager_name": manager_name,
            "manager_code": manager_code,
            "months_of_service": months_of_service,
            "pl_accrued": pl_accrued,
            "pl_taken": pl_taken,
            "pl_balance": pl_balance,
            "sl_balance": sl_balance,
            "cl_balance": cl_balance,
            "custom_pl_accrual": float(emp["custom_pl_accrual"]) if emp.get("custom_pl_accrual") is not None else "",
            "custom_sl_quota": float(emp["custom_sl_quota"]) if emp.get("custom_sl_quota") is not None else "",
            "custom_cl_quota": float(emp["custom_cl_quota"]) if emp.get("custom_cl_quota") is not None else "",
            "tax_regime": tax_regime,
            "portal_username": portal_username,
            "portal_active": portal_active,
            "portal_last_login": portal_last_login,
            "notes": emp.get("notes") or "",
            "created_at": created_at,
            "updated_at": updated_at,
        }

        row = [data_dict.get(col[1], "") for col in EXPORT_COLUMNS]
        rows.append(row)

    return headers, rows


def export_employees_xlsx(
    storage: Storage,
    settings: Settings,
    *,
    q: str = "",
    department: str = "",
    status: str = "all",
) -> bytes:
    """Generate fully-featured OpenXML Spreadsheet (.xlsx) bytes with brand styling."""
    headers, rows = get_employees_dataset(storage, settings, q=q, department=department, status=status)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        # Standard OpenXML package metadata
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>""",
        )

        z.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )

        z.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )

        z.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Employees" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )

        # Style Definitions:
        # Font 0: regular, Font 1: bold white (header)
        # Fill 0: none, Fill 1: gray, Fill 2: brand maroon #870808, Fill 3: zebra subtle #F9F9F9
        # Border 1: light border #D0D0D0
        # XF 0: default
        # XF 1: Header (maroon, white bold, center)
        # XF 2: Number cell (right-aligned, bordered)
        # XF 3: Text cell (left-aligned, bordered)
        # XF 4: Center cell (center-aligned, bordered)
        z.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><name val="Calibri"/><sz val="11"/><color rgb="FF212529"/></font>
    <font><b/><color rgb="FFFFFFFF"/><name val="Calibri"/><sz val="11"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF870808"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFDF2F2"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/></border>
    <border>
      <left style="thin"><color rgb="FFD0D0D0"/></left>
      <right style="thin"><color rgb="FFD0D0D0"/></right>
      <top style="thin"><color rgb="FFD0D0D0"/></top>
      <bottom style="thin"><color rgb="FFD0D0D0"/></bottom>
    </border>
  </borders>
  <cellXfs count="5">
    <xf fontId="0" fillId="0" borderId="0"/>
    <xf fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="right" vertical="center"/>
    </xf>
    <xf fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="left" vertical="center"/>
    </xf>
    <xf fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
  </cellXfs>
</styleSheet>""",
        )

        # Dynamic Column Width calculation
        col_widths: list[int] = []
        for c_idx, h in enumerate(headers):
            max_len = len(str(h))
            for r in rows:
                v_str = str(r[c_idx]) if r[c_idx] is not None else ""
                if len(v_str) > max_len:
                    max_len = len(v_str)
            col_widths.append(min(max(max_len + 3, 12), 40))

        # Build sheet1.xml
        buf: list[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            '<cols>',
        ]
        for idx, w in enumerate(col_widths, start=1):
            buf.append(f'<col min="{idx}" max="{idx}" width="{w}" customWidth="1"/>')
        buf.append("</cols>")

        buf.append("<sheetData>")
        # Header row (height 26)
        buf.append('<row r="1" ht="26" customHeight="1">')
        for c_idx, h in enumerate(headers):
            ref = f"{_col_letter(c_idx)}1"
            safe_h = xml.sax.saxutils.escape(str(h))
            buf.append(f'<c r="{ref}" s="1" t="inlineStr"><is><t>{safe_h}</t></is></c>')
        buf.append("</row>")

        # Data rows (height 20)
        for r_idx, row in enumerate(rows, start=2):
            buf.append(f'<row r="{r_idx}" ht="20" customHeight="1">')
            for c_idx, val in enumerate(row):
                ref = f"{_col_letter(c_idx)}{r_idx}"
                col_type = EXPORT_COLUMNS[c_idx][2]

                if val is None or val == "":
                    # Empty bordered cell
                    buf.append(f'<c r="{ref}" s="3"/>')
                elif col_type == "number" and isinstance(val, (int, float)):
                    buf.append(f'<c r="{ref}" s="2"><v>{val}</v></c>')
                elif col_type == "date":
                    safe_v = xml.sax.saxutils.escape(str(val))
                    buf.append(f'<c r="{ref}" s="4" t="inlineStr"><is><t>{safe_v}</t></is></c>')
                else:
                    safe_v = xml.sax.saxutils.escape(str(val))
                    buf.append(f'<c r="{ref}" s="3" t="inlineStr"><is><t>{safe_v}</t></is></c>')
            buf.append("</row>")

        buf.append("</sheetData>")
        buf.append("</worksheet>")

        z.writestr("xl/worksheets/sheet1.xml", "".join(buf))

    return out.getvalue()


def export_employees_csv(
    storage: Storage,
    settings: Settings,
    *,
    q: str = "",
    department: str = "",
    status: str = "all",
) -> str:
    """Generate UTF-8 with BOM CSV for 100% native Microsoft Excel compatibility."""
    headers, rows = get_employees_dataset(storage, settings, q=q, department=department, status=status)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if val is None else val for val in row])
    # Prepend UTF-8 BOM so Excel opens the CSV correctly with proper encoding
    return "\ufeff" + out.getvalue()
