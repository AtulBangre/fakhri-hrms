"""Tests for Employee of the Month and Top Performers Recognition feature."""

from __future__ import annotations

import pytest
from app.services import recognition as recognition_service
from app.services import orgtime


def test_recognition_service_calculation(storage, settings, staff):
    today = orgtime.today(settings)
    period = orgtime.period_of(today)

    emp1 = staff[0]
    emp2 = staff[1]

    # Create attendance for emp1: perfect attendance, on-time
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, late_minutes, worked_minutes, source, created_at, updated_at)
        VALUES (?, ?, 'present', 0, 510, 'face', '2026-11-01 10:00:00', '2026-11-01 18:00:00')
        """,
        (emp1["id"], f"{period}-02"),
    )

    # Create attendance for emp2: late punch
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, late_minutes, worked_minutes, source, created_at, updated_at)
        VALUES (?, ?, 'present', 45, 450, 'manual', '2026-11-01 10:45:00', '2026-11-01 18:00:00')
        """,
        (emp2["id"], f"{period}-02"),
    )

    recognition_service.clear_cache()
    data = recognition_service.get_monthly_recognition(storage, settings, period=period, use_cache=False)

    assert data["period"] == period
    assert data["is_live"] is True
    assert len(data["top3"]) >= 2

    top1 = data["top3"][0]
    top2 = data["top3"][1]

    assert top1["rank"] == 1
    assert top1["employee"]["id"] == emp1["id"]
    assert top1["score"] > top2["score"]
    assert "100% on-time" in top1["reasons"][0]


def test_recognition_api_and_dashboard_integration(hr_client, storage, settings, staff):
    today = orgtime.today(settings)
    period = orgtime.period_of(today)

    # 1. API endpoint test
    res = hr_client.get(f"/api/dashboard/recognition?period={period}")
    assert res.status_code == 200
    json_data = res.json()
    assert "top3" in json_data
    assert "available_periods" in json_data
    assert json_data["period"] == period

    # 2. Dashboard page test
    dash_res = hr_client.get("/dashboard")
    assert dash_res.status_code == 200
    assert "recognitionCard" in dash_res.text
    assert "recogPeriodSelect" in dash_res.text
    assert "scorecardModalOverlay" in dash_res.text


def test_employee_portal_recognition_view(ess_client, storage, settings, ess_credentials):
    today = orgtime.today(settings)
    period = orgtime.period_of(today)
    emp = ess_credentials["employee"]

    # Record attendance for employee
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, late_minutes, worked_minutes, source, created_at, updated_at)
        VALUES (?, ?, 'present', 0, 500, 'face', '2026-11-01 10:00:00', '2026-11-01 18:00:00')
        """,
        (emp["id"], f"{period}-02"),
    )

    recognition_service.clear_cache()

    # 1. Test get_employee_performance service helper
    perf = recognition_service.get_employee_performance(storage, settings, emp["id"], period=period, use_cache=False)
    assert perf is not None
    assert perf["candidate"]["employee"]["id"] == emp["id"]
    assert perf["candidate"]["rank"] >= 1
    assert perf["candidate"]["score"] > 0
    assert "attendance" in perf["candidate"]["breakdown"]

    # 2. Test /me employee portal renders score banner & dialog modal
    res = ess_client.get("/me")
    assert res.status_code == 200
    assert "me-score-banner" in res.text
    assert "openScorecardModal" in res.text
    assert "meScorecardModalOverlay" in res.text
    assert "Score Breakdown by Dimension" in res.text


def test_get_employee_performance(storage, settings, staff):
    today = orgtime.today(settings)
    period = orgtime.period_of(today)

    emp1 = staff[0]
    emp2 = staff[1]

    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, late_minutes, worked_minutes, source, created_at, updated_at)
        VALUES (?, ?, 'present', 0, 510, 'face', '2026-11-01 10:00:00', '2026-11-01 18:00:00')
        """,
        (emp1["id"], f"{period}-02"),
    )
    storage.execute(
        """
        INSERT INTO attendance (employee_id, work_date, status, late_minutes, worked_minutes, source, created_at, updated_at)
        VALUES (?, ?, 'present', 30, 450, 'manual', '2026-11-01 10:30:00', '2026-11-01 18:00:00')
        """,
        (emp2["id"], f"{period}-02"),
    )

    recognition_service.clear_cache()
    perf1 = recognition_service.get_employee_performance(storage, settings, emp1["id"], period=period, use_cache=False)
    assert perf1 is not None
    assert perf1["candidate"]["rank"] == 1
    assert perf1["candidate"]["employee"]["id"] == emp1["id"]
    assert perf1["total_candidates"] >= 2

    perf2 = recognition_service.get_employee_performance(storage, settings, emp2["id"], period=period, use_cache=False)
    assert perf2 is not None
    assert perf2["candidate"]["rank"] == 2
    assert perf2["candidate"]["score"] < perf1["candidate"]["score"]
    assert perf2["punctuality_potential"] > 0

