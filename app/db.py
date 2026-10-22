"""SQLite storage: schema, connection handling and small query helpers.

One file (``data/hrms.db``) holds everything — users, employees, attendance,
leave, salary history, payroll runs and payslips — which keeps deployment
portable: copy one file and the whole HR system moved.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import SETTING_DEFS, Settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
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

CREATE TABLE IF NOT EXISTS employees (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    email            TEXT,
    phone            TEXT,
    department       TEXT,
    designation      TEXT,
    joined_on        TEXT,
    employment_type  TEXT NOT NULL DEFAULT 'full_time',
    status           TEXT NOT NULL DEFAULT 'active',
    monthly_gross    REAL NOT NULL DEFAULT 0,
    face_id          TEXT,
    face_enrolled_at TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_employees_status ON employees(status);

CREATE TABLE IF NOT EXISTS attendance (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date      TEXT NOT NULL,
    status         TEXT NOT NULL,
    check_in       TEXT,
    check_out      TEXT,
    late_minutes   INTEGER NOT NULL DEFAULT 0,
    worked_minutes INTEGER,
    leave_type     TEXT,
    source         TEXT NOT NULL DEFAULT 'face',
    device_label   TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(employee_id, work_date)
);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(work_date);
CREATE INDEX IF NOT EXISTS idx_attendance_emp  ON attendance(employee_id, work_date);

CREATE TABLE IF NOT EXISTS leave_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    leave_type  TEXT NOT NULL,
    from_date   TEXT NOT NULL,
    to_date     TEXT NOT NULL,
    days        REAL NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'approved',
    applied_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_leave_employee ON leave_requests(employee_id, from_date);

CREATE TABLE IF NOT EXISTS salary_revisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    monthly_gross     REAL NOT NULL,
    basic             REAL NOT NULL DEFAULT 0,
    hra               REAL NOT NULL DEFAULT 0,
    other_allowances  REAL NOT NULL DEFAULT 0,
    pf                REAL NOT NULL DEFAULT 0,
    esi               REAL NOT NULL DEFAULT 0,
    professional_tax  REAL NOT NULL DEFAULT 0,
    effective_from    TEXT NOT NULL,
    note              TEXT,
    created_by        INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_salary_employee ON salary_revisions(employee_id, effective_from);

CREATE TABLE IF NOT EXISTS payroll_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    period           TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'draft',
    basis_days       INTEGER NOT NULL,
    total_gross      REAL NOT NULL DEFAULT 0,
    total_deductions REAL NOT NULL DEFAULT 0,
    total_net        REAL NOT NULL DEFAULT 0,
    generated_at     TEXT NOT NULL,
    finalized_at     TEXT,
    paid_at          TEXT,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS payslips (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
    employee_id      INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    period           TEXT NOT NULL,
    basis_days       INTEGER NOT NULL,
    present_days     REAL NOT NULL DEFAULT 0,
    paid_leave_days  REAL NOT NULL DEFAULT 0,
    lop_days         REAL NOT NULL DEFAULT 0,
    monthly_gross    REAL NOT NULL DEFAULT 0,
    per_day_rate     REAL NOT NULL DEFAULT 0,
    earned_gross     REAL NOT NULL DEFAULT 0,
    bonus            REAL NOT NULL DEFAULT 0,
    advance          REAL NOT NULL DEFAULT 0,
    other_deduction  REAL NOT NULL DEFAULT 0,
    pf               REAL NOT NULL DEFAULT 0,
    esi              REAL NOT NULL DEFAULT 0,
    professional_tax REAL NOT NULL DEFAULT 0,
    total_deductions REAL NOT NULL DEFAULT 0,
    net_pay          REAL NOT NULL DEFAULT 0,
    note             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(run_id, employee_id)
);

CREATE TABLE IF NOT EXISTS holidays (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    entity    TEXT,
    entity_id TEXT,
    detail    TEXT,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class Storage:
    """Thread-safe SQLite wrapper (FastAPI runs sync routes in a thread pool)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = Path(settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._seed_settings()

    # ------------------------------------------------------------- plumbing
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    # ------------------------------------------------------- app settings
    def _seed_settings(self) -> None:
        with self._lock:
            for key, default, *_ in SETTING_DEFS:
                self._conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, default)
                )
            self._conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if row:
            return row["value"]
        for def_key, def_value, *_ in SETTING_DEFS:
            if def_key == key:
                return def_value
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get_setting(key, str(default))))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def all_settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.query("SELECT key, value FROM app_settings")}

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------- audit
    def audit(self, *, actor: str | None, action: str, entity: str | None = None,
              entity_id: Any = None, detail: str | None = None, ip: str | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log (at, actor, action, entity, entity_id, detail, ip) VALUES (?,?,?,?,?,?,?)",
            (utcnow_iso(), actor, action, entity, None if entity_id is None else str(entity_id), detail, ip),
        )
