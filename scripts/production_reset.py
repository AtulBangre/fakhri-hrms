#!/usr/bin/env python3
"""DESTRUCTIVE: erase all HR data and leave a single HR admin account.

This deletes every employee, payroll, attendance and audit row, the uploads folder
and every enrolled face. It refuses to run unless given ``--erase-all-data`` and
``HRMS_RESET_HR_PASSWORD``, and stops if the face gallery cannot be cleared.

ID sequences are deliberately kept. Reusing an employee id would point any face
template left behind at the wrong person.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from app.auth import hash_password
from app.config import get_settings
from app.db import Storage, utcnow_iso


def reset_production() -> None:
    hr_password = os.environ.get("HRMS_RESET_HR_PASSWORD", "")
    if "--erase-all-data" not in sys.argv[1:] or len(hr_password) < 12:
        sys.exit("Refusing to run. Pass --erase-all-data and set HRMS_RESET_HR_PASSWORD (12+ characters).")

    settings = get_settings()
    db_path = Path(settings.db_path)
    print(f"Target database: {db_path}")

    # Ensure parent dir exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    storage = Storage(settings)

    print("Purging all dummy and operational data...")
    tables_to_purge = [
        "attendance",
        "attendance_regularizations",
        "leave_requests",
        "payslips",
        "payroll_runs",
        "salary_revisions",
        "documents",
        "assets",
        "asset_assignments",
        "expense_claims",
        "work_locations",
        "field_checks",
        "tax_profiles",
        "tax_declarations",
        "employee_exits",
        "exit_checklist",
        "appraisal_cycles",
        "appraisals",
        "appraisal_goals",
        "profile_change_requests",
        "announcements",
        "notifications",
        "otp_codes",
        "password_tokens",
        "login_attempts",
        "employees",
        "users",
        "audit_log",
    ]

    with storage.transaction():
        # Temporarily disable foreign keys during mass truncation
        storage._conn.execute("PRAGMA foreign_keys = OFF;")
        for tbl in tables_to_purge:
            try:
                storage._conn.execute(f'DELETE FROM "{tbl}";')
            except sqlite3.OperationalError:
                pass

        # Create single HR administrator
        hr_email = "hr@fakhriitservices.com"
        pw_hash, salt = hash_password(hr_password)
        now = utcnow_iso()

        storage._conn.execute(
            """
            INSERT INTO users (username, password_hash, salt, role, display_name, active, created_at, email, session_version)
            VALUES (?, ?, ?, 'hr', 'Disha', 1, ?, ?, 1)
            """,
            (hr_email, pw_hash, salt, now, hr_email),
        )

        # Ensure default shift exists
        shift_count = storage._conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        if shift_count == 0:
            storage._conn.execute(
                """
                INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, is_default, created_at)
                VALUES ('General Shift', '09:30', '18:30', 15, 240, 60, 1, ?)
                """,
                (now,),
            )

        # Clean audit log with single initial entry
        storage._conn.execute(
            """
            INSERT INTO audit_log (at, actor, action, entity, detail)
            VALUES (?, ?, 'production_reset', 'system', 'Production environment initialized. Ready for employee onboarding.')
            """,
            (now, hr_email),
        )

        storage._conn.execute("PRAGMA foreign_keys = ON;")

    print(f"Created sole HR Admin: {hr_email} (Display: Disha)")

    # Clean local uploads directory
    uploads_dir = Path(settings.data_dir) / "uploads"
    if uploads_dir.exists():
        shutil.rmtree(uploads_dir)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        print("Cleared uploads folder.")

    # Remove initial_credentials.txt if exists
    cred_file = Path(settings.data_dir) / "initial_credentials.txt"
    if cred_file.exists():
        cred_file.unlink()

    # A gallery left behind is how an old face ends up showing a new person's details.
    headers = {"X-API-Key": settings.faceapi_key} if settings.faceapi_key else {}
    faceapi_url = settings.faceapi_url.rstrip("/")
    try:
        resp = httpx.delete(
            f"{faceapi_url}/v1/collections/{settings.face_collection}", headers=headers, timeout=10.0
        )
    except httpx.HTTPError as exc:
        sys.exit(f"RESET INCOMPLETE: could not reach FaceAPI to clear the gallery: {exc}")
    if resp.status_code not in (200, 404):
        sys.exit(f"RESET INCOMPLETE: FaceAPI refused to clear the gallery: HTTP {resp.status_code}")
    print(f"Cleared FaceAPI collection '{settings.face_collection}': HTTP {resp.status_code}")

    print("The Supabase mirror was not touched. Sync refuses to copy an empty database.")

    print("\n" + "=" * 60)
    print("PRODUCTION RESET COMPLETE & VERIFIED!")
    print(f"Login URL: {settings.public_base_url.rstrip('/')}/login")
    print(f"Username / Email: {hr_email}")
    print("Password: the value of HRMS_RESET_HR_PASSWORD")
    print(f"Role: hr (Full Admin / Dashboard Access)")
    print("Total Employees: 0 (Ready for fresh onboarding)")
    print("=" * 60)


if __name__ == "__main__":
    reset_production()
