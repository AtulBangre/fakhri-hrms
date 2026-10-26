"""Tests for enterprise features and modern UX additions:
- Shift master table & seeding
- Live biometric gate feed API
- Visual Org Chart (/employees/chart)
- Bulk Employee Update (/employees/bulk-update)
- In-app Ask HR Assistant (/api/ai-hr/query)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Settings
from app.db import Storage


@pytest.fixture
def client_and_storage(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        secret_key="test-secret-key-for-enterprise-features",
        default_hr_password="TestAdminPassword123!",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.storage


def _login_hr(client: TestClient) -> None:
    res = client.post(
        "/login",
        data={"username": "hr", "password": "TestAdminPassword123!"},
        follow_redirects=False,
    )
    assert res.status_code == 303


def test_shifts_table_and_seed(client_and_storage):
    _, storage = client_and_storage
    shifts = storage.query("SELECT * FROM shifts")
    assert len(shifts) >= 1
    default_shift = next((s for s in shifts if s["is_default"] == 1), None)
    assert default_shift is not None
    assert default_shift["name"] == "General Shift"
    assert default_shift["start_time"] == "09:30"
    assert default_shift["end_time"] == "18:30"
    assert default_shift["grace_minutes"] == 15


def test_live_scans_api(client_and_storage):
    client, storage = client_and_storage
    _login_hr(client)

    res = client.get("/api/dashboard/live-scans")
    assert res.status_code == 200
    data = res.json()
    assert "recent" in data
    assert "counts" in data


def test_org_chart_page(client_and_storage):
    client, storage = client_and_storage
    _login_hr(client)

    res = client.get("/employees/chart")
    assert res.status_code == 200
    assert "Organization Hierarchy" in res.text


def test_bulk_update_flow(client_and_storage):
    client, storage = client_and_storage
    _login_hr(client)

    # 1. Fetch form
    res = client.get("/employees/bulk-update")
    assert res.status_code == 200
    assert "Bulk Employee Update" in res.text

    # 2. Get CSRF token
    csrf = client.cookies.get("hrms_session")
    session = client.app.state.storage.query_one(
        "SELECT id, username FROM users WHERE username = 'hr'"
    )
    assert session is not None

    # Create dummy employees
    e1_id = storage.scalar(
        "INSERT INTO employees (code, name, department, monthly_gross, created_at, updated_at) "
        "VALUES ('EMP101', 'Test Person 1', 'Engineering', 50000, '2026-01-01', '2026-01-01') RETURNING id"
    )
    e2_id = storage.scalar(
        "INSERT INTO employees (code, name, department, monthly_gross, created_at, updated_at) "
        "VALUES ('EMP102', 'Test Person 2', 'Sales', 45000, '2026-01-01', '2026-01-01') RETURNING id"
    )

    # Get CSRF from page
    page_res = client.get("/employees/bulk-update")
    import re
    csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', page_res.text)
    token = csrf_match.group(1) if csrf_match else ""

    # Apply bulk update to e1 and e2 -> new department 'Product'
    post_res = client.post(
        "/employees/bulk-update",
        data={
            "_csrf": token,
            "employee_ids": [e1_id, e2_id],
            "new_department": "Product",
            "new_shift_id": "",
        },
        follow_redirects=False,
    )
    assert post_res.status_code == 303

    # Check updated departments
    row1 = storage.query_one("SELECT department FROM employees WHERE id = ?", (e1_id,))
    row2 = storage.query_one("SELECT department FROM employees WHERE id = ?", (e2_id,))
    assert row1["department"] == "Product"
    assert row2["department"] == "Product"


def test_ai_hr_assistant_queries(client_and_storage):
    client, storage = client_and_storage
    _login_hr(client)

    # Query holidays
    res = client.post("/api/ai-hr/query", json={"query": "What are the upcoming holidays?"})
    assert res.status_code == 200
    data = res.json()
    assert "answer" in data
    assert "holiday" in data["answer"].lower() or "period" in data["answer"].lower()

    # Query shift
    res = client.post("/api/ai-hr/query", json={"query": "What is the shift timing and grace?"})
    assert res.status_code == 200
    data = res.json()
    assert "09:30" in data["answer"]
    assert "15" in data["answer"]

    # Query today attendance
    res = client.post("/api/ai-hr/query", json={"query": "Who is absent today?"})
    assert res.status_code == 200
    data = res.json()
    assert "today's stats" in data["answer"].lower()
