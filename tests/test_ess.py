"""Employee self-service portal tests.

The contract these lock down:

* an employee login sees **only its own** attendance, payslips and leave
* it cannot reach HR pages, the gate scanner, or the scan API
* a leave request from the portal is ``pending`` and touches nothing until HR
  approves it — then attendance is written and payroll becomes correct
* the ``users`` table migration accepts the new role without losing data
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import Settings
from app.db import Storage
from app.services import attendance as attendance_service
from app.services import employees as employees_service
from app.services import leave as leave_service
from app.services import orgtime, payroll
from tests.conftest import HR_PASSWORD, HR_USERNAME
from tests.helpers import csrf_of

# ------------------------------------------------------------------ migration
OLD_SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('hr', 'kiosk')),
    display_name  TEXT,
    device_label  TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
INSERT INTO users (username, password_hash, salt, role, active, created_at)
VALUES ('hr', 'x', 'y', 'hr', 1, 'now');
INSERT INTO users (username, password_hash, salt, role, active, created_at)
VALUES ('gate', 'x', 'y', 'kiosk', 1, 'now');
"""


def test_migration_adds_the_employee_role_without_losing_rows(tmp_path):
    db_path = tmp_path / "hrms.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    storage = Storage(Settings(data_dir=tmp_path))
    sql = storage.query_one("SELECT sql FROM sqlite_master WHERE name = 'users'")["sql"]
    assert "'employee'" in sql
    assert "employee_id" in sql
    assert [row["username"] for row in storage.query("SELECT username FROM users ORDER BY id")] == [
        "hr",
        "gate",
    ]
    # the index the portal queries rely on
    assert storage.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_employee'"
    )
    storage.close()

    # and it is idempotent
    Storage(Settings(data_dir=tmp_path)).close()


# --------------------------------------------------------------- login wiring
def test_ensure_employee_login_uses_the_employee_code(storage, staff):
    user, password = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    assert user["username"] == staff[0]["code"].lower()
    assert user["role"] == auth.ROLE_EMPLOYEE
    assert user["employee_id"] == staff[0]["id"]
    assert auth.authenticate(storage, user["username"], password) is not None


def test_ensure_employee_login_resets_an_existing_password(storage, staff):
    first, _ = auth.ensure_employee_login(storage, staff[0], password="first-password")
    second, password = auth.ensure_employee_login(storage, staff[0], password="second-password")
    assert first["id"] == second["id"]                    # same login, no duplicate
    assert auth.authenticate(storage, second["username"], "first-password") is None
    assert auth.authenticate(storage, second["username"], password) is not None


def test_employee_login_rejects_a_username_clash(storage, staff, hr_user):
    # a HR login already owns "hr"; an employee code of "HR" must not reuse it
    employee_id = employees_service.create_employee(storage, {"name": "Clash Person", "code": "HR"})
    employee = employees_service.get_employee(storage, employee_id)
    with pytest.raises(ValueError):
        auth.ensure_employee_login(storage, employee, password="whatever-123")


def test_employee_login_redirects_to_the_portal(client, ess_credentials, hr_user, kiosk_user):
    response = client.post(
        "/login",
        data={"username": ess_credentials["username"], "password": ess_credentials["password"], "next": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/me"


def test_root_sends_an_employee_to_their_portal(ess_client):
    response = ess_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/me"


def test_disabled_portal_login_cannot_sign_in(client, storage, staff, hr_user, kiosk_user):
    user, password = auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    auth.disable_employee_login(storage, staff[0]["id"])
    response = client.post(
        "/login", data={"username": user["username"], "password": password}, follow_redirects=False
    )
    assert response.status_code == 401


# ---------------------------------------------------------------- access rules
def test_employee_cannot_open_hr_pages(ess_client: TestClient, ess_credentials):
    for path in ("/dashboard", "/employees", "/payroll", "/leave", "/settings", "/audit", "/attendance"):
        response = ess_client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/me", path


def test_employee_cannot_open_the_gate_scanner(ess_client: TestClient):
    response = ess_client.get("/kiosk", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/me"


def test_employee_cannot_call_the_scan_api(ess_client: TestClient, assets):
    token = csrf_of(ess_client, "/me")
    response = ess_client.post(
        "/api/kiosk/scan",
        data={"_csrf": token},
        files={"image": ("x.jpg", assets["person_a_1"], "image/jpeg")},
    )
    assert response.status_code == 403


def test_hr_cannot_open_the_employee_portal(hr_client: TestClient):
    response = hr_client.get("/me", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# ------------------------------------------------------------- own data only
def test_portal_dashboard_shows_only_my_name(
    ess_client: TestClient, ess_credentials, staff, storage, settings
):
    from app.services import attendance as attendance_service

    attendance_service.mark_manual(
        storage, employee_id=staff[1]["id"], work_date=orgtime.today_str(settings),
        status="present", check_in="09:30", check_out="18:30",
    )
    page = ess_client.get("/me")
    assert page.status_code == 200
    assert ess_credentials["employee"]["name"] in page.text
    # the colleague who also has attendance today must not appear
    assert staff[1]["name"] not in page.text
    assert staff[2]["name"] not in page.text


def test_portal_attendance_is_scoped_to_me(ess_client: TestClient, ess_credentials, staff, storage, settings):
    today = orgtime.today(settings)
    attendance_service.mark_manual(
        storage, employee_id=staff[0]["id"], work_date=today.isoformat(), status="present",
        check_in="09:31", check_out="18:32",
    )
    attendance_service.mark_manual(
        storage, employee_id=staff[1]["id"], work_date=today.isoformat(), status="present",
        check_in="09:47", check_out="18:01",
    )
    page = ess_client.get("/me/attendance")
    assert page.status_code == 200
    assert "09:31" in page.text            # mine
    assert "09:47" not in page.text        # colleague's
    assert staff[1]["name"] not in page.text


def test_portal_payslips_are_scoped_to_me(ess_client: TestClient, ess_credentials, staff, storage, settings):
    payroll.generate_run(storage, settings, "2026-01")
    mine = [s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == staff[0]["id"]][0]
    page = ess_client.get("/me/payslips")
    assert page.status_code == 200
    assert mine["code"] in page.text
    # someone else's net pay must not be on the page
    for other in payroll.list_slips(storage, "2026-01"):
        if other["employee_id"] != staff[0]["id"]:
            assert other["code"] not in page.text


def test_employee_cannot_open_someone_elses_payslip(
    ess_client: TestClient, ess_credentials, staff, storage, settings
):
    payroll.generate_run(storage, settings, "2026-01")
    other = [s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == staff[1]["id"]][0]

    response = ess_client.get(f"/me/payslips/{other['id']}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/me/payslips?err=")

    page = ess_client.get(f"/me/payslips/{other['id']}")
    assert "Salary slip" not in page.text
    assert f"{other['net_pay']:.2f}" not in page.text


def test_employee_can_open_their_own_payslip(ess_client: TestClient, ess_credentials, staff, storage, settings):
    payroll.generate_run(storage, settings, "2026-01")
    mine = [s for s in payroll.list_slips(storage, "2026-01") if s["employee_id"] == staff[0]["id"]][0]
    page = ess_client.get(f"/me/payslips/{mine['id']}")
    assert page.status_code == 200
    assert "Salary slip" in page.text
    assert ess_credentials["employee"]["name"] in page.text
    # the employee must not get a link back into the HR payroll area
    assert "/payroll?" not in page.text


# ------------------------------------------------------- leave request workflow
def test_portal_leave_request_is_pending_and_touches_no_attendance(
    ess_client: TestClient, ess_credentials, storage, settings
):
    token = csrf_of(ess_client, "/me/leave")
    response = ess_client.post(
        "/me/leave",
        data={
            "_csrf": token,
            "leave_type": "paid",
            "from_date": "2026-01-05",
            "to_date": "2026-01-06",
            "reason": "family function",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    leaves = leave_service.list_leave(storage, employee_id=ess_credentials["employee"]["id"])
    assert len(leaves) == 1
    assert leaves[0]["status"] == "pending"
    assert leaves[0]["days"] == 2

    # crucially: no attendance rows yet
    assert attendance_service.get_row(storage, ess_credentials["employee"]["id"], date(2026, 1, 5)) is None
    assert attendance_service.get_row(storage, ess_credentials["employee"]["id"], date(2026, 1, 6)) is None


def test_hr_sees_the_pending_request_and_approves_it(
    ess_client: TestClient, ess_credentials, storage, settings
):
    """One browser = one session, so: request as the employee, then sign in as HR."""
    employee_id = ess_credentials["employee"]["id"]

    token = csrf_of(ess_client, "/me/leave")
    ess_client.post(
        "/me/leave",
        data={"_csrf": token, "leave_type": "paid", "from_date": "2026-01-05", "to_date": "2026-01-06"},
        follow_redirects=False,
    )
    leave_id = leave_service.list_leave(storage, employee_id=employee_id)[0]["id"]

    # HR signs in on the same client (the login replaces the session cookie)
    assert ess_client.post(
        "/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False
    ).status_code == 303

    queue_page = ess_client.get("/leave")
    assert "Waiting for your approval" in queue_page.text
    assert ess_credentials["employee"]["name"] in queue_page.text

    hr_token = csrf_of(ess_client, "/leave")
    response = ess_client.post(
        f"/leave/{leave_id}/approve",
        data={"_csrf": hr_token, "period": "2026-01"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    leave = leave_service.list_leave(storage, employee_id=employee_id)[0]
    assert leave["status"] == "approved"
    row = attendance_service.get_row(storage, employee_id, date(2026, 1, 5))
    assert row is not None and row["status"] == "leave" and row["leave_type"] == "paid"


def test_hr_rejects_a_request_without_touching_attendance(
    ess_client: TestClient, ess_credentials, storage
):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/leave")
    ess_client.post(
        "/me/leave",
        data={"_csrf": token, "leave_type": "casual", "from_date": "2026-01-07"},
        follow_redirects=False,
    )
    leave_id = leave_service.list_leave(storage, employee_id=employee_id)[0]["id"]

    ess_client.post("/login", data={"username": HR_USERNAME, "password": HR_PASSWORD}, follow_redirects=False)
    hr_token = csrf_of(ess_client, "/leave")
    ess_client.post(
        f"/leave/{leave_id}/reject",
        data={"_csrf": hr_token, "reason": "busy week", "period": "2026-01"},
        follow_redirects=False,
    )
    leave = leave_service.list_leave(storage, employee_id=employee_id)[0]
    assert leave["status"] == "rejected"
    assert attendance_service.get_row(storage, employee_id, date(2026, 1, 7)) is None

    # and the employee sees the rejection on their own page
    ess_client.post(
        "/login",
        data={"username": ess_credentials["username"], "password": ess_credentials["password"]},
        follow_redirects=False,
    )
    page = ess_client.get("/me/leave")
    assert "rejected" in page.text


def test_employee_can_withdraw_a_pending_request(ess_client: TestClient, ess_credentials, storage):
    employee_id = ess_credentials["employee"]["id"]
    token = csrf_of(ess_client, "/me/leave")
    ess_client.post(
        "/me/leave",
        data={"_csrf": token, "leave_type": "paid", "from_date": "2026-01-08"},
        follow_redirects=False,
    )
    leave_id = leave_service.list_leave(storage, employee_id=employee_id)[0]["id"]

    response = ess_client.post(
        f"/me/leave/{leave_id}/cancel", data={"_csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert leave_service.list_leave(storage, employee_id=employee_id)[0]["status"] == "cancelled"


def test_employee_cannot_withdraw_someone_elses_request(
    ess_client: TestClient, ess_credentials, staff, storage, settings
):
    # HR books leave for a colleague
    result = leave_service.apply_leave(
        storage, settings, employee_id=staff[1]["id"], leave_type="paid", from_date="2026-01-09"
    )
    other_id = result["leave"]["id"]

    token = csrf_of(ess_client, "/me/leave")
    response = ess_client.post(
        f"/me/leave/{other_id}/cancel", data={"_csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert storage.query_one("SELECT status FROM leave_requests WHERE id = ?", (other_id,))["status"] == "approved"


def test_approved_leave_cannot_be_withdrawn_by_the_employee(ess_client: TestClient, ess_credentials, storage, settings):
    employee_id = ess_credentials["employee"]["id"]
    leave_service.apply_leave(
        storage, settings, employee_id=employee_id, leave_type="paid", from_date="2026-01-12"
    )
    leave_id = leave_service.list_leave(storage, employee_id=employee_id)[0]["id"]

    token = csrf_of(ess_client, "/me/leave")
    response = ess_client.post(
        f"/me/leave/{leave_id}/cancel", data={"_csrf": token}, follow_redirects=False
    )
    assert "err=" in response.headers["location"]
    assert storage.query_one("SELECT status FROM leave_requests WHERE id = ?", (leave_id,))["status"] == "approved"


def test_ess_leave_over_a_week_off_is_refused(ess_client: TestClient, ess_credentials):
    """2026-01-04 is a Sunday — the request must be rejected, not silently accepted."""
    token = csrf_of(ess_client, "/me/leave")
    response = ess_client.post(
        "/me/leave",
        data={"_csrf": token, "leave_type": "paid", "from_date": "2026-01-04", "to_date": "2026-01-04"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]


# ------------------------------------------------------------- HR side screens
def test_hr_creates_a_portal_login_from_the_employee_page(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, f"/employees/{staff[2]['id']}")
    response = hr_client.post(
        f"/employees/{staff[2]['id']}/portal",
        data={"_csrf": token, "password": "chosen-password-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    # the password must never travel in the URL
    assert "chosen-password-1" not in response.headers["location"]

    user = auth.get_user_for_employee(storage, staff[2]["id"])
    assert user is not None
    assert auth.authenticate(storage, user["username"], "chosen-password-1") is not None


def test_hr_portal_password_must_be_long_enough(hr_client: TestClient, storage, staff):
    token = csrf_of(hr_client, f"/employees/{staff[2]['id']}")
    response = hr_client.post(
        f"/employees/{staff[2]['id']}/portal",
        data={"_csrf": token, "password": "short"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]
    assert auth.get_user_for_employee(storage, staff[2]["id"]) is None


def test_hr_can_disable_a_portal_login(hr_client: TestClient, storage, staff):
    auth.ensure_employee_login(storage, staff[2], password="portal-pass-1")
    token = csrf_of(hr_client, f"/employees/{staff[2]['id']}")
    response = hr_client.post(
        f"/employees/{staff[2]['id']}/portal/disable", data={"_csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    user = auth.get_user_for_employee(storage, staff[2]["id"])
    assert user["active"] == 0


def test_employee_page_shows_the_portal_card(hr_client: TestClient, storage, staff):
    auth.ensure_employee_login(storage, staff[0], password="portal-pass-1")
    page = hr_client.get(f"/employees/{staff[0]['id']}")
    assert "Employee portal" in page.text
    assert staff[0]["code"].lower() in page.text


def test_cli_ess_creates_logins_for_everyone(tmp_path, capsys):
    """`cli.py ess --missing-only` is how HR rolls the portal out in bulk."""
    from cli import main

    settings = Settings(data_dir=tmp_path, default_hr_password="x", default_kiosk_password="y")
    storage = Storage(settings)
    for name, code in (("One Person", "P001"), ("Two Person", "P002")):
        employees_service.create_employee(storage, {"name": name, "code": code, "monthly_gross": 1000})
    storage.close()

    import cli as cli_module

    original = cli_module.get_settings
    cli_module.get_settings = lambda refresh=False: settings   # cli holds its own reference
    try:
        code = main(["ess", "--missing-only"])
    finally:
        cli_module.get_settings = original
    assert code == 0

    storage = Storage(settings)
    for code in ("P001", "P002"):
        employee = employees_service.get_employee_by_code(storage, code)
        assert auth.get_user_for_employee(storage, employee["id"]) is not None
    storage.close()


def test_migration_keeps_other_tables_foreign_keys_valid(tmp_path):
    """Regression: renaming `users` used to rewrite leave_requests' FK to users_old.

    That left a dangling reference, and every insert into leave_requests failed
    with "no such table: main.users_old" — the portal's whole point.
    """
    db_path = tmp_path / "hrms.db"
    seed = Storage(Settings(data_dir=tmp_path))
    seed.execute(
        "INSERT INTO employees (code, name, status, created_at, updated_at) "
        "VALUES ('T001','Test Person','active','now','now')"
    )
    employee_id = seed.scalar("SELECT id FROM employees WHERE code='T001'")
    seed.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, status, created_at) "
        "VALUES (?,'paid','2026-12-05','2026-12-05',1,'pending','now')",
        (employee_id,),
    )
    seed.close()

    # put the OLD users schema back, as a pre-ESS installation would have
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        DROP TABLE users;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, salt TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('hr','kiosk')),
            display_name TEXT, device_label TEXT, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_login_at TEXT);
        INSERT INTO users (username, password_hash, salt, role, active, created_at)
        VALUES ('hr','x','y','hr',1,'now');
        """
    )
    raw.commit()
    raw.close()

    storage = Storage(Settings(data_dir=tmp_path))
    # no table may point at the temporary table name
    assert storage.query("SELECT name FROM sqlite_master WHERE sql LIKE '%users_old%'") == []
    # and the dependent table still accepts writes
    storage.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, status, created_at) "
        "VALUES (?,'paid','2026-12-06','2026-12-06',1,'pending','now')",
        (employee_id,),
    )
    assert storage.scalar("SELECT COUNT(*) AS n FROM leave_requests") == 2
    storage.close()


def test_repair_heals_a_database_already_hit_by_the_bad_migration(tmp_path):
    """A database left with `... REFERENCES users_old` must repair itself on boot."""
    storage = Storage(Settings(data_dir=tmp_path))
    storage.execute(
        "INSERT INTO employees (code, name, status, created_at, updated_at) "
        "VALUES ('T001','Test Person','active','now','now')"
    )
    employee_id = storage.scalar("SELECT id FROM employees WHERE code='T001'")
    storage.close()

    raw = sqlite3.connect(tmp_path / "hrms.db")
    raw.executescript(
        """
        DROP TABLE leave_requests;
        CREATE TABLE leave_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            leave_type  TEXT NOT NULL, from_date TEXT NOT NULL, to_date TEXT NOT NULL,
            days        REAL NOT NULL, reason TEXT,
            status      TEXT NOT NULL DEFAULT 'approved',
            applied_by  INTEGER REFERENCES users_old(id),
            created_at  TEXT NOT NULL, decided_at TEXT);
        CREATE INDEX idx_leave_employee ON leave_requests(employee_id, from_date);
        """
    )
    raw.commit()
    raw.close()

    storage = Storage(Settings(data_dir=tmp_path))
    assert storage.query("SELECT name FROM sqlite_master WHERE sql LIKE '%users_old%'") == []
    # the index was rebuilt alongside the table
    assert storage.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_leave_employee'"
    )
    storage.execute(
        "INSERT INTO leave_requests (employee_id, leave_type, from_date, to_date, days, status, created_at) "
        "VALUES (?,'paid','2026-12-07','2026-12-07',1,'pending','now')",
        (employee_id,),
    )
    storage.close()
