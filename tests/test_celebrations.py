"""Tests for the HR Celebrations dashboard (/celebrations)."""

from __future__ import annotations

from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient

from app.services import celebrations as celebrations_service

TODAY = date(2026, 6, 22)


def _dated(storage, employee: dict, **fields) -> None:
    for column, value in fields.items():
        storage.execute(f"UPDATE employees SET {column} = ? WHERE id = ?", (value, employee["id"]))


def test_list_celebrations_default_nearest_birthdays(storage, staff):
    # staff[0]: birthday tomorrow (June 23)
    # staff[1]: birthday next week (June 29)
    # staff[2]: birthday yesterday (June 21 - wraps to next year)
    _dated(storage, staff[0], date_of_birth="1995-06-23")
    _dated(storage, staff[1], date_of_birth="1992-06-29")
    _dated(storage, staff[2], date_of_birth="1990-06-21")

    items = celebrations_service.list_celebrations(
        storage,
        today=TODAY,
        kind="birthday",
    )

    assert len(items) >= 3
    # Nearest first: June 23 (1 day), June 29 (7 days), June 21 next year (364 days)
    names = [i["employee"]["name"] for i in items[:3]]
    assert names[0] == staff[0]["name"]
    assert items[0]["in_days"] == 1
    assert names[1] == staff[1]["name"]
    assert items[1]["in_days"] == 7
    assert names[2] == staff[2]["name"]
    assert items[2]["in_days"] > 300


def test_list_celebrations_date_range_filter(storage, staff):
    _dated(storage, staff[0], date_of_birth="1995-06-23")
    _dated(storage, staff[1], date_of_birth="1992-07-15")
    _dated(storage, staff[2], date_of_birth="1990-08-01")

    # Filter within June only
    items = celebrations_service.list_celebrations(
        storage,
        today=TODAY,
        from_date=date(2026, 6, 1),
        to_date=date(2026, 6, 30),
        kind="birthday",
    )

    filtered_ids = [i["employee"]["id"] for i in items]
    assert staff[0]["id"] in filtered_ids
    assert staff[1]["id"] not in filtered_ids
    assert staff[2]["id"] not in filtered_ids


def test_list_celebrations_kind_filter(storage, staff):
    _dated(storage, staff[0], date_of_birth="1995-06-23", joined_on="2024-06-23", anniversary=None)
    _dated(storage, staff[1], date_of_birth=None, joined_on=None, anniversary="2018-06-24")
    _dated(storage, staff[2], date_of_birth=None, joined_on=None, anniversary=None)

    # Only work anniversaries
    work_items = celebrations_service.list_celebrations(
        storage,
        today=TODAY,
        kind="work",
    )
    work_emp_ids = [i["employee"]["id"] for i in work_items]
    assert staff[0]["id"] in work_emp_ids
    assert staff[1]["id"] not in work_emp_ids
    assert staff[2]["id"] not in work_emp_ids

    # Only wedding anniversaries
    wedding_items = celebrations_service.list_celebrations(
        storage,
        today=TODAY,
        kind="anniversary",
    )
    wedding_emp_ids = [i["employee"]["id"] for i in wedding_items]
    assert staff[1]["id"] in wedding_emp_ids
    assert staff[0]["id"] not in wedding_emp_ids
    assert staff[2]["id"] not in wedding_emp_ids

    # All celebrations
    all_items = celebrations_service.list_celebrations(
        storage,
        today=TODAY,
        kind="all",
    )
    all_emp_ids = {i["employee"]["id"] for i in all_items}
    assert staff[0]["id"] in all_emp_ids
    assert staff[1]["id"] in all_emp_ids


def test_celebration_stats_and_missing_dob(storage, staff):
    _dated(storage, staff[0], date_of_birth=TODAY.isoformat())  # today
    _dated(storage, staff[1], date_of_birth=(TODAY + timedelta(days=2)).isoformat())  # 2 days
    _dated(storage, staff[2], date_of_birth=None)  # missing DOB

    stats = celebrations_service.celebration_stats(storage, today=TODAY)
    assert stats["today_count"] >= 1
    assert stats["next_7_count"] >= 2
    assert stats["missing_dob"] >= 1

    missing = celebrations_service.missing_dob_employees(storage)
    missing_ids = [m["id"] for m in missing]
    assert staff[2]["id"] in missing_ids


def test_celebrations_page_authenticated_hr(hr_client: TestClient, staff, storage):
    _dated(storage, staff[0], date_of_birth="1995-06-23")
    res = hr_client.get("/celebrations")
    assert res.status_code == 200
    assert "Celebrations" in res.text
    assert "Birthdays" in res.text
    assert staff[0]["name"] in res.text


def test_celebrations_page_access_control(client: TestClient, ess_client: TestClient):
    # Unauthenticated should redirect to login
    res = client.get("/celebrations", follow_redirects=False)
    assert res.status_code in (302, 303, 307)

    # Employee login should redirect to /me
    res_ess = ess_client.get("/celebrations", follow_redirects=False)
    assert res_ess.status_code == 303
    assert res_ess.headers["location"] == "/me"
