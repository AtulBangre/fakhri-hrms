"""Income tax: declarations, a *projection*, the CA's confirmed TDS, and Form 16.

**Read this before changing a number.** This module deliberately does two
different things and never mixes them up:

``projection``
    An estimate computed from the statutory slabs. It exists so HR can see roughly
    what an employee's tax should be, and so the employee understands the effect of
    declaring 80C/80D/HRA. It is shown with a "verify with your CA" banner and is
    **never** used for Form 16.

``confirmed``
    The actual TDS figure entered by HR or the CA (``tax_profiles.confirmed_tds``).
    Payroll deducts ``monthly_tds`` because of it, and Form 16 Part B reports it.
    If nobody has confirmed a figure, Form 16 refuses to generate — that is the
    whole point of option B: the employer/CA owns the tax number, not this code.

Statutory sources: Income-tax Act slabs for FY 2025-26 (old and new regimes),
standard deduction, section 87A rebate, 4% health & education cess. Surcharge is
applied at the basic rates but **marginal relief is not modelled** — above 50 lakh
the projection warns and asks for a manual figure.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from ..uploads import delete_file, save_upload
from . import employees as employees_service
from . import payroll
from . import orgtime

#: Financial year helpers ------------------------------------------------
def fy_for_period(period: str) -> str:
    """``'2026-03'`` → ``'2025-26'`` (Indian FY runs April → March)."""
    return orgtime.fiscal_year(period)


def fy_for_date(day: date) -> str:
    return fy_for_period(f"{day.year}-{day.month:02d}")


def current_fy(settings: Settings) -> str:
    return fy_for_date(orgtime.today(settings))


def fy_label(fy: str) -> str:
    return f"FY {fy}"


def fy_months(fy: str) -> list[str]:
    return orgtime.fiscal_year_months(fy)


def fy_range(fy: str) -> tuple[str, str]:
    start = int(fy.split("-")[0])
    return f"{start}-04-01", f"{start + 1}-03-31"


def list_fys(storage: Storage, settings: Settings) -> list[str]:
    """Every FY that has salary, payslips or declarations, newest first."""
    found: set[str] = set()
    for row in storage.query("SELECT DISTINCT period FROM payslips"):
        found.add(fy_for_period(row["period"]))
    for row in storage.query("SELECT DISTINCT fiscal_year FROM tax_declarations"):
        found.add(row["fiscal_year"])
    found.add(current_fy(settings))
    return sorted(found, reverse=True)


#: Slabs ---------------------------------------------------------------
# (upper bound of the slab, rate) — the last entry must be (None, rate)
SLABS_NEW = [
    (400_000, 0.00), (800_000, 0.05), (1_200_000, 0.10), (1_600_000, 0.15),
    (2_000_000, 0.20), (2_400_000, 0.25), (None, 0.30),
]
SLABS_OLD = [
    (250_000, 0.00), (500_000, 0.05), (1_000_000, 0.20), (None, 0.30),
]
STD_DEDUCTION_NEW = 75_000.0
STD_DEDUCTION_OLD = 50_000.0
REBATE_NEW = (1_200_000.0, 60_000.0)
REBATE_OLD = (500_000.0, 12_500.0)
CESS_RATE = 0.04

#: caps, in rupees
CAP_80C = 150_000.0
CAP_80CCD1B = 50_000.0
CAP_80D_SELF = 25_000.0
CAP_80D_SENIOR = 50_000.0
CAP_80TTA = 10_000.0
CAP_24B = 200_000.0

SECTIONS = {
    "80C": ("80C — PF, ELSS, LIC, PPF, tuition", CAP_80C),
    "80CCD1B": ("80CCD(1B) — NPS (extra)", CAP_80CCD1B),
    "80D": ("80D — health insurance", CAP_80D_SELF),
    "80TTA": ("80TTA — savings interest", CAP_80TTA),
    "80E": ("80E — education loan interest", None),
    "24B": ("24(b) — home loan interest", CAP_24B),
}


def slab_tax(taxable: float, slabs: list[tuple[float | None, float]]) -> tuple[float, list[dict[str, Any]]]:
    """Tax at slab rates, plus a line-by-line breakdown for the UI."""
    tax, lower, breakdown = 0.0, 0.0, []
    for upper, rate in slabs:
        ceiling = upper if upper is not None else float("inf")
        if taxable <= lower:
            break
        amount = min(taxable, ceiling) - lower
        if amount > 0:
            contribution = amount * rate
            tax += contribution
            breakdown.append({"from": lower, "to": min(taxable, ceiling), "rate": rate,
                              "amount": round(amount, 2), "tax": round(contribution, 2)})
        lower = ceiling
    return round(tax, 2), breakdown


def _surcharge(tax: float, income: float, regime: str) -> tuple[float, float]:
    """Basic surcharge. Marginal relief is not applied — see the module docstring."""
    if income <= 5_000_000:
        return 0.0, 0.0
    if regime == "new":
        rate = 0.10 if income <= 10_000_000 else 0.15 if income <= 20_000_000 else 0.25
    else:
        rate = 0.10 if income <= 10_000_000 else 0.15 if income <= 20_000_000 else 0.25 if income <= 50_000_000 else 0.37
    return rate, round(tax * rate, 2)


# ---------------------------------------------------------------- declarations
def list_declarations(storage: Storage, employee_id: int, fy: str) -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT * FROM tax_declarations WHERE employee_id = ? AND fiscal_year = ? ORDER BY section, id",
        (employee_id, fy),
    )
    for row in rows:
        row["section_label"] = SECTIONS.get(row["section"], (row["section"], None))[0]
    return rows


def add_declaration(
    storage: Storage,
    settings: Settings,
    *,
    employee_id: int,
    fy: str,
    section: str,
    amount: float,
    description: str | None = None,
    proof: tuple[bytes, str] | None = None,
    declared_by: str | None = None,
) -> dict[str, Any]:
    employees_service.require_employee(storage, employee_id)
    if section not in SECTIONS:
        raise ValueError("Unknown section.")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid amount.") from None
    if amount <= 0:
        raise ValueError("The amount must be greater than zero.")
    if amount > 10_000_000:
        raise ValueError("That amount looks wrong — check the declaration.")

    proof_name = proof_mime = proof_size = None
    if proof:
        stored = save_upload(settings, "proofs", proof[0], original_name=proof[1])
        proof_name, proof_mime, proof_size = stored.stored_name, stored.mime, stored.size

    cursor = storage.execute(
        """
        INSERT INTO tax_declarations (employee_id, fiscal_year, section, description, amount,
                                      declared_by, declared_at, proof_name, proof_mime, proof_size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (employee_id, fy, section, description, round(amount, 2), declared_by, utcnow_iso(),
         proof_name, proof_mime, proof_size),
    )
    storage.audit(actor=declared_by, action="tax_declaration", entity="employee",
                  entity_id=employee_id, detail=f"{fy} {section} {amount:.0f}")
    return get_declaration(storage, int(cursor.lastrowid))


def get_declaration(storage: Storage, declaration_id: int) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM tax_declarations WHERE id = ?", (declaration_id,))
    if row is None:
        raise ValueError("Declaration not found.")
    return row


def delete_declaration(storage: Storage, settings: Settings, declaration_id: int, *,
                       actor: str | None = None) -> None:
    row = get_declaration(storage, declaration_id)
    delete_file(settings, "proofs", row["proof_name"])
    storage.execute("DELETE FROM tax_declarations WHERE id = ?", (declaration_id,))
    storage.audit(actor=actor, action="tax_declaration_deleted", entity="employee",
                  entity_id=row["employee_id"], detail=row["section"])


def verify_declaration(storage: Storage, declaration_id: int, *, verified: bool,
                       actor: str | None = None) -> dict[str, Any]:
    get_declaration(storage, declaration_id)
    storage.execute(
        "UPDATE tax_declarations SET status = ?, verified_by = ?, verified_at = ? WHERE id = ?",
        ("verified" if verified else "rejected", actor if verified else None,
         utcnow_iso() if verified else None, declaration_id),
    )
    return get_declaration(storage, declaration_id)


def declared_totals(storage: Storage, employee_id: int, fy: str, *, verified_only: bool = False) -> dict[str, float]:
    clause = " AND status = 'verified'" if verified_only else ""
    rows = storage.query(
        f"SELECT section, SUM(amount) AS total FROM tax_declarations "
        f"WHERE employee_id = ? AND fiscal_year = ?{clause} GROUP BY section",
        (employee_id, fy),
    )
    return {row["section"]: float(row["total"] or 0) for row in rows}


# -------------------------------------------------------------------- profile
def get_profile(storage: Storage, employee_id: int, fy: str) -> dict[str, Any]:
    row = storage.query_one(
        "SELECT * FROM tax_profiles WHERE employee_id = ? AND fiscal_year = ?", (employee_id, fy)
    )
    return dict(row) if row else {}


def ensure_profile(
    storage: Storage, settings: Settings, *, employee_id: int, fy: str
) -> dict[str, Any]:
    """Create the profile on first use, pre-filled from the salary records."""
    existing = get_profile(storage, employee_id, fy)
    if existing:
        return existing

    annual_gross = annual_gross_for(storage, settings, employee_id, fy)
    basic = round(annual_gross * 0.50, 2)
    hra = round(annual_gross * 0.20, 2)
    storage.execute(
        """
        INSERT INTO tax_profiles (employee_id, fiscal_year, regime, metro, annual_rent,
                                  basic_annual, hra_annual, monthly_tds, updated_at)
        VALUES (?,?, 'new', 0, 0, ?, ?, 0, ?)
        """,
        (employee_id, fy, basic, hra, utcnow_iso()),
    )
    return get_profile(storage, employee_id, fy)


def update_profile(storage: Storage, *, employee_id: int, fy: str, **fields: Any) -> dict[str, Any]:
    profile = get_profile(storage, employee_id, fy)
    if not profile:
        raise ValueError("No tax profile for this employee yet.")

    allowed = {"regime", "metro", "annual_rent", "basic_annual", "hra_annual", "monthly_tds", "note"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "regime" in updates and updates["regime"] not in ("new", "old"):
        raise ValueError("Unknown regime.")
    if not updates:
        return profile

    assignments = ", ".join(f"{key} = ?" for key in updates)
    storage.execute(
        f"UPDATE tax_profiles SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), utcnow_iso(), profile["id"]),
    )
    return get_profile(storage, employee_id, fy)


def confirm_tds(
    storage: Storage,
    *,
    employee_id: int,
    fy: str,
    annual_tds: float,
    taxable_income: float | None = None,
    actor: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record the figure HR/the CA stands behind. This is what Form 16 reports."""
    profile = get_profile(storage, employee_id, fy)
    if not profile:
        raise ValueError("No tax profile for this employee yet.")
    try:
        annual_tds = float(annual_tds)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid TDS amount.") from None
    if annual_tds < 0:
        raise ValueError("TDS cannot be negative.")

    months = max(1, len(fy_months(fy)))
    storage.execute(
        """
        UPDATE tax_profiles SET confirmed_tds = ?, confirmed_taxable = ?, monthly_tds = ?,
                                confirmed_by = ?, confirmed_at = ?, note = COALESCE(?, note),
                                updated_at = ?
        WHERE id = ?
        """,
        (round(annual_tds, 2), taxable_income, round(annual_tds / months, 2),
         actor, utcnow_iso(), note, utcnow_iso(), profile["id"]),
    )
    storage.audit(actor=actor, action="tds_confirmed", entity="employee", entity_id=employee_id,
                  detail=f"{fy} annual TDS {annual_tds:.2f}")
    return get_profile(storage, employee_id, fy)


# ------------------------------------------------------------------ gross pay
def annual_gross_for(storage: Storage, settings: Settings, employee_id: int, fy: str) -> float:
    """Earned gross across the FY's payslips; falls back to the salary record × 12."""
    months = fy_months(fy)
    row = storage.query_one(
        "SELECT COALESCE(SUM(earned_gross), 0) AS total, COUNT(*) AS n FROM payslips "
        "WHERE employee_id = ? AND period BETWEEN ? AND ?",
        (employee_id, months[0], months[-1]),
    ) or {}
    if row.get("n"):
        return round(float(row["total"]), 2)

    employee = employees_service.get_employee(storage, employee_id)
    if not employee:
        return 0.0
    sample = payroll.salary_for_period(storage, employee_id, fy_months(fy)[0])
    return round(float(sample.get("monthly_gross") or 0) * 12, 2)


def tds_deducted(storage: Storage, employee_id: int, fy: str) -> float:
    """What payroll has actually deducted this FY (sum of payslip TDS)."""
    months = fy_months(fy)
    return round(float(storage.scalar(
        "SELECT COALESCE(SUM(tds), 0) AS t FROM payslips WHERE employee_id = ? AND period BETWEEN ? AND ?",
        (employee_id, months[0], months[-1]),
    )), 2)


# ----------------------------------------------------------------- projection
def project(
    storage: Storage,
    settings: Settings,
    *,
    employee: dict[str, Any],
    fy: str,
    regime: str | None = None,
    profile: dict[str, Any] | None = None,
    declarations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Estimate the year's tax. **A projection, not a filing number.**"""
    profile = profile or get_profile(storage, employee["id"], fy)
    regime = regime or profile.get("regime") or "new"
    declared = declarations if declarations is not None else declared_totals(storage, employee["id"], fy)

    gross = annual_gross_for(storage, settings, employee["id"], fy)
    basic = float(profile.get("basic_annual") or gross * 0.50)
    hra_received = float(profile.get("hra_annual") or gross * 0.20)
    rent = float(profile.get("annual_rent") or 0)
    metro = bool(profile.get("metro"))

    steps: list[dict[str, Any]] = []
    warnings: list[str] = []

    def step(label: str, amount: float, note: str = "") -> None:
        steps.append({"label": label, "amount": round(amount, 2), "note": note})

    step("Annual gross salary", gross)

    if regime == "old":
        hra_exempt = 0.0
        if rent > 0 and basic > 0:
            hra_exempt = min(hra_received, max(0.0, rent - 0.10 * basic), (0.50 if metro else 0.40) * basic)
            step("Less: HRA exemption u/s 10(13A)", -hra_exempt,
                 f"rent {rent:,.0f}, {'metro' if metro else 'non-metro'}")
        std = min(STD_DEDUCTION_OLD, max(0.0, gross - hra_exempt))
        step("Less: standard deduction u/s 16(ia)", -std)

        c80 = min(declared.get("80C", 0.0), CAP_80C)
        nps = min(declared.get("80CCD1B", 0.0), CAP_80CCD1B)
        d80 = min(declared.get("80D", 0.0), CAP_80D_SELF)
        tta = min(declared.get("80TTA", 0.0), CAP_80TTA)
        edu = declared.get("80E", 0.0)
        home = min(declared.get("24B", 0.0), CAP_24B)
        chapter = c80 + nps + d80 + tta + edu
        if c80:
            step("Less: 80C", -c80, f"capped at {CAP_80C:,.0f}")
        if nps:
            step("Less: 80CCD(1B) NPS", -nps)
        if d80:
            step("Less: 80D health insurance", -d80)
        if tta:
            step("Less: 80TTA savings interest", -tta)
        if edu:
            step("Less: 80E education loan", -edu)
        if home:
            step("Less: 24(b) home loan interest", -home, f"capped at {CAP_24B:,.0f}")

        taxable = max(0.0, gross - hra_exempt - std - chapter - home)
        slabs, rebate_cap, rebate_amount, std_note = SLABS_OLD, *REBATE_OLD, "old regime"
    else:
        std = min(STD_DEDUCTION_NEW, gross)
        step("Less: standard deduction u/s 16(ia)", -std)
        taxable = max(0.0, gross - std)
        slabs, rebate_cap, rebate_amount, std_note = SLABS_NEW, *REBATE_NEW, "new regime"
        if declared:
            warnings.append(
                "The new regime does not allow 80C/80D/HRA. Declarations are still recorded, "
                "but they are ignored in this projection."
            )

    step("Taxable income", taxable, std_note)
    base_tax, breakdown = slab_tax(taxable, slabs)
    step("Tax on slabs", base_tax)

    rebate = rebate_amount if taxable <= rebate_cap else 0.0
    if rebate:
        step("Less: rebate u/s 87A", -rebate)
    after_rebate = max(0.0, base_tax - rebate)

    surcharge_rate, surcharge = _surcharge(after_rebate, taxable, regime)
    if surcharge:
        step(f"Surcharge @ {surcharge_rate:.0%}", surcharge,
             "marginal relief not modelled — confirm with your CA")
        warnings.append("Income is above 50 lakh: surcharge marginal relief is not modelled.")

    cess = round((after_rebate + surcharge) * CESS_RATE, 2)
    step("Health & education cess @ 4%", cess)

    total_tax = round(after_rebate + surcharge + cess, 2)
    step("Total tax (projected)", total_tax)

    monthly = round(total_tax / 12, 2)
    confirmed = profile.get("confirmed_tds")

    return {
        "employee_id": employee["id"],
        "employee_name": employee["name"],
        "fiscal_year": fy,
        "regime": regime,
        "gross": gross,
        "taxable": round(taxable, 2),
        "total_tax": total_tax,
        "monthly_tds": monthly,
        "confirmed_tds": float(confirmed) if confirmed is not None else None,
        "tds_deducted": tds_deducted(storage, employee["id"], fy),
        "steps": steps,
        "breakdown": breakdown,
        "warnings": warnings,
        "hra_exempt": round(hra_exempt, 2) if regime == "old" else 0.0,
        "standard_deduction": std,
        "declared": declared,
    }


def compare_regimes(storage: Storage, settings: Settings, *, employee: dict[str, Any], fy: str) -> dict[str, Any]:
    profile = ensure_profile(storage, settings, employee_id=employee["id"], fy=fy)
    old = project(storage, settings, employee=employee, fy=fy, regime="old", profile=profile)
    new = project(storage, settings, employee=employee, fy=fy, regime="new", profile=profile)
    better = "old" if old["total_tax"] < new["total_tax"] else "new"
    return {"old": old, "new": new, "better": better,
            "saving": round(abs(old["total_tax"] - new["total_tax"]), 2)}


# ------------------------------------------------------------------ Form 16
def form16_data(storage: Storage, settings: Settings, *, employee_id: int, fy: str) -> dict[str, Any]:
    """Assemble everything Form 16 Part B needs. Raises if TDS is unconfirmed."""
    employee = employees_service.require_employee(storage, employee_id)
    profile = get_profile(storage, employee_id, fy)
    if not profile:
        raise ValueError("Create the tax profile for this financial year first.")
    if profile.get("confirmed_tds") is None:
        raise ValueError(
            "TDS has not been confirmed for this employee yet. Enter the figure from your CA "
            "in the TDS box, then generate Form 16."
        )

    projection = project(storage, settings, employee=employee, fy=fy, profile=profile)
    start, end = fy_range(fy)
    slips = [
        slip
        for period in fy_months(fy)
        for slip in payroll.list_slips(storage, period)
        if slip["employee_id"] == employee_id
    ]
    earned = round(sum(float(slip["earned_gross"] or 0) for slip in slips), 2)
    bonus = round(sum(float(slip["bonus"] or 0) for slip in slips), 2)
    professional_tax = round(sum(float(slip["professional_tax"] or 0) for slip in slips), 2)
    pf = round(sum(float(slip["pf"] or 0) for slip in slips), 2)
    actual_tds = round(sum(float(slip.get("tds") or 0) for slip in slips), 2)

    confirmed_tds = float(profile["confirmed_tds"])
    taxable = profile.get("confirmed_taxable")
    taxable = float(taxable) if taxable is not None else projection["taxable"]
    total_tax = projection["total_tax"]

    return {
        "employee": employee,
        "fiscal_year": fy,
        "period_from": start,
        "period_to": end,
        "months": len(slips),
        "gross_salary": round(earned + bonus, 2),
        "salary_17_1": earned,
        "bonus_17_3": bonus,
        "exempt_allowances": projection.get("hra_exempt", 0.0),
        "standard_deduction": projection["standard_deduction"],
        "professional_tax": professional_tax,
        "income_from_salary": round(max(0.0, earned + bonus - projection["standard_deduction"]
                                        - professional_tax - projection.get("hra_exempt", 0.0)), 2),
        "declared": projection["declared"],
        "taxable_income": round(taxable, 2),
        "tax_on_income": projection["total_tax"],
        "rebate": 0.0,
        "cess_included": True,
        "total_tax": total_tax,
        "tds_confirmed": confirmed_tds,
        "tds_deducted_by_payroll": actual_tds,
        "balance": round(total_tax - confirmed_tds, 2),
        "regime": profile["regime"],
        "pf": pf,
        "confirmed_by": profile.get("confirmed_by"),
        "confirmed_at": profile.get("confirmed_at"),
        "projection": projection,
    }


# ---------------------------------------------------------------- HR overview
def register(storage: Storage, settings: Settings, fy: str) -> list[dict[str, Any]]:
    """One row per active employee for the HR tax screen."""
    rows: list[dict[str, Any]] = []
    for employee in employees_service.list_employees(storage, status="active", limit=1000):
        profile = get_profile(storage, employee["id"], fy)
        declared = declared_totals(storage, employee["id"], fy)
        projection = project(
            storage, settings, employee=employee, fy=fy,
            regime=profile.get("regime") or "new", profile=profile, declarations=declared,
        )
        rows.append({
            "employee": employee,
            "profile": profile,
            "declared_total": round(sum(declared.values()), 2),
            "gross": projection["gross"],
            "taxable": projection["taxable"],
            "projected_tax": projection["total_tax"],
            "confirmed_tds": projection["confirmed_tds"],
            "deducted": projection["tds_deducted"],
            "regime": profile.get("regime") or "new",
            "confirmed": profile.get("confirmed_tds") is not None,
        })
    return rows


def summary(storage: Storage, settings: Settings, fy: str) -> dict[str, Any]:
    rows = register(storage, settings, fy)
    return {
        "employees": len(rows),
        "confirmed": sum(1 for row in rows if row["confirmed"]),
        "pending": sum(1 for row in rows if not row["confirmed"]),
        "projected": round(sum(row["projected_tax"] for row in rows), 2),
        "confirmed_total": round(sum(row["confirmed_tds"] or 0 for row in rows), 2),
        "deducted": round(sum(row["deducted"] for row in rows), 2),
        "gross": round(sum(row["gross"] for row in rows), 2),
    }


def import_tds_schedule(storage: Storage, settings: Settings, *, csv_text: str, fy: str,
                        actor: str | None = None) -> dict[str, Any]:
    """Apply a CA-supplied sheet: ``code,annual_tds[,taxable_income]`` per line."""
    applied, skipped, errors = 0, 0, []
    for line_no, raw in enumerate(csv_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line_no == 1 and line.lower().startswith(("code", "employee")):
            continue
        parts = [part.strip() for part in line.replace("\t", ",").split(",")]
        if len(parts) < 2:
            errors.append(f"line {line_no}: expected code,tds")
            skipped += 1
            continue
        code, tds_raw = parts[0], parts[1]
        taxable_raw = parts[2] if len(parts) > 2 else ""
        employee = storage.query_one(
            "SELECT * FROM employees WHERE UPPER(code) = ?", (code.upper(),)
        )
        if employee is None:
            errors.append(f"line {line_no}: no employee with code {code}")
            skipped += 1
            continue
        try:
            annual = float(tds_raw.replace(",", ""))
            taxable = float(taxable_raw.replace(",", "")) if taxable_raw else None
        except ValueError:
            errors.append(f"line {line_no}: '{tds_raw}' is not a number")
            skipped += 1
            continue
        ensure_profile(storage, settings, employee_id=employee["id"], fy=fy)
        confirm_tds(storage, employee_id=employee["id"], fy=fy, annual_tds=annual,
                    taxable_income=taxable, actor=actor, note="imported from CA schedule")
        applied += 1
    storage.audit(actor=actor, action="tds_import", entity="tax",
                  detail=f"{fy}: {applied} applied, {skipped} skipped")
    return {"applied": applied, "skipped": skipped, "errors": errors[:10]}
