"""Tests for Salary Revision Applicability (Effective Month/Date) and PT default."""

from __future__ import annotations

from datetime import date
import pytest
from fastapi.testclient import TestClient

from app.services import employees as employees_service
from app.services import payroll as payroll_service
from tests.helpers import csrf_of


def test_salary_revision_future_month_applicability(storage, staff):
    emp = staff[0]
    emp_id = emp["id"]
    current_gross = float(emp["monthly_gross"])
    assert current_gross > 0

    # Schedule a raise effective 2026-12-01 (next month)
    future_gross = current_gross + 10000.0
    employees_service.set_salary(
        storage,
        emp_id,
        monthly_gross=future_gross,
        basic=future_gross * 0.5,
        hra=future_gross * 0.3,
        other_allowances=future_gross * 0.2,
        pf=1800.0,
        esi=0.0,
        professional_tax=0.0,
        effective_from="2026-12-01",
        note="Promotion effective Dec 2026",
    )

    # 1. Active monthly gross in employees table should stay at current_gross (not future_gross)
    refreshed_emp = employees_service.get_employee(storage, emp_id)
    assert refreshed_emp["monthly_gross"] == current_gross

    # 2. Payroll for 2026-11 should use current_gross
    sal_nov = payroll_service.salary_for_period(storage, emp_id, "2026-11")
    assert sal_nov["monthly_gross"] == current_gross

    # 3. Payroll for 2026-12 should use future_gross
    sal_dec = payroll_service.salary_for_period(storage, emp_id, "2026-12")
    assert sal_dec["monthly_gross"] == future_gross


def test_salary_revision_current_and_retroactive_applicability(storage, staff):
    emp = staff[0]
    emp_id = emp["id"]

    # 1. Current month revision (2026-11-01)
    new_gross = 60000.0
    employees_service.set_salary(
        storage,
        emp_id,
        monthly_gross=new_gross,
        effective_from="2026-11-01",
        note="Current cycle increment",
    )
    refreshed_emp = employees_service.get_employee(storage, emp_id)
    assert refreshed_emp["monthly_gross"] == new_gross
    sal_nov = payroll_service.salary_for_period(storage, emp_id, "2026-11")
    assert sal_nov["monthly_gross"] == new_gross

    # 2. Retroactive revision (2026-10-01)
    retro_gross = 65000.0
    employees_service.set_salary(
        storage,
        emp_id,
        monthly_gross=retro_gross,
        effective_from="2026-10-01",
        note="Retroactive adjustment for Oct",
    )
    sal_oct = payroll_service.salary_for_period(storage, emp_id, "2026-10")
    assert sal_oct["monthly_gross"] == retro_gross


def test_professional_tax_default_is_zero(hr_client, storage, staff):
    emp = staff[0]
    emp_id = emp["id"]

    # Fetch edit page and verify professional_tax is not 200
    res = hr_client.get(f"/employees/{emp_id}/edit")
    assert res.status_code == 200
    assert 'name="professional_tax"' in res.text
    # Must have value="0" and not value="200"
    assert 'value="200"' not in res.text

    # Onboard page (/employees/new) should default PT to 0
    res_new = hr_client.get("/employees/new")
    assert res_new.status_code == 200
    assert 'name="professional_tax"' in res_new.text
    assert 'value="200"' not in res_new.text


def test_update_employee_route_salary_revision(hr_client, storage, staff):
    emp = staff[0]
    emp_id = emp["id"]
    csrf = csrf_of(hr_client, f"/employees/{emp_id}/edit")

    # Update employee details along with revised salary effective next month
    res = hr_client.post(
        f"/employees/{emp_id}",
        data={
            "_csrf": csrf,
            "name": emp["name"],
            "code": emp["code"],
            "email": emp["email"] or "test@example.com",
            "phone": emp["phone"] or "9876543210",
            "department": emp["department"] or "Engineering",
            "designation": emp["designation"] or "Developer",
            "monthly_gross": "75000",
            "basic": "37500",
            "hra": "15000",
            "other_allowances": "22500",
            "pf": "1800",
            "esi": "0",
            "professional_tax": "0",
            "effective_from": "2026-12-01",
            "salary_note": "Yearly increment",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200

    # Check history
    history = employees_service.salary_history(storage, emp_id)
    assert history[0]["monthly_gross"] == 75000.0
    assert history[0]["effective_from"] == "2026-12-01"
    assert history[0]["professional_tax"] == 0.0
    assert history[0]["note"] == "Yearly increment"
