"""Tests for individual and bulk payslip email dispatch."""

from __future__ import annotations

from app.services import employees as employees_service
from app.services import payroll as payroll_service


def test_epfo_ecr_uan_and_member_filtering(storage, settings, staff):
    emp1 = staff[0]
    emp2 = staff[1]

    # emp1 has valid UAN and PF
    employees_service.update_employee(storage, emp1["id"], {"uan": "100123456789"})

    period = "2026-10"
    run = payroll_service.generate_run(storage, settings, period)
    slips = payroll_service.slips_for_run(storage, run["id"])

    ecr_text = payroll_service.export_epfo_ecr(storage, period)
    lines = [l for l in ecr_text.strip().split("\n") if l]

    # Must only contain valid 12-digit UAN or members, never alphabetical employee codes (FSPL...)
    for line in lines:
        parts = line.split("#~#")
        assert len(parts) == 11
        uan = parts[0]
        # UAN must be numeric or empty, never containing FSPL or letters
        assert not any(c.isalpha() for c in uan), f"Found alphabetic character in UAN: {uan}"

        # Mathematical check: EPS share must never exceed EE share
        ee_share = int(parts[6])
        eps_share = int(parts[7])
        er_share = int(parts[8])
        assert eps_share <= ee_share, f"EPS share ({eps_share}) > EE share ({ee_share})"
        assert eps_share + er_share == ee_share, f"EPS + ER ({eps_share + er_share}) != EE ({ee_share})"
