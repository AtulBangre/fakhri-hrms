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
