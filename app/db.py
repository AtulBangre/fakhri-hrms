"""SQLite storage: schema, connection handling and small query helpers.

One file (``data/hrms.db``) holds everything — users, employees, attendance,
leave, salary history, payroll runs and payslips — which keeps deployment
portable: copy one file and the whole HR system moved.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
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
    role          TEXT NOT NULL CHECK (role IN ('hr', 'kiosk', 'employee')),
    display_name  TEXT,
    device_label  TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    employee_id   INTEGER REFERENCES employees(id) ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    channel    TEXT NOT NULL,              -- email | whatsapp | sms
    recipient  TEXT NOT NULL,              -- address / number
    subject    TEXT,
    body       TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'system',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT,
    error      TEXT,
    meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, id);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at);

CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',      -- info | success | warning | urgent
    audience   TEXT NOT NULL DEFAULT 'all',       -- all | employees | hr
    pinned     INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    expires_on TEXT
);
CREATE INDEX IF NOT EXISTS idx_announcements_active ON announcements(active, pinned);

CREATE TABLE IF NOT EXISTS otp_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_otp_user ON otp_codes(user_id, created_at);

CREATE TABLE IF NOT EXISTS profile_change_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    requested_at TEXT NOT NULL,
    requested_by INTEGER REFERENCES users(id),
    decided_at  TEXT,
    decided_by  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_req_status ON profile_change_requests(status, id);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    category    TEXT NOT NULL DEFAULT 'other',   -- aadhaar | pan | offer_letter | contract | resume | other
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,                   -- original name, shown to the user
    stored_name TEXT NOT NULL,                   -- name on disk (random)
    mime        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    notes       TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    verified_by TEXT,
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_employee ON documents(employee_id, category);

CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tag           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'laptop', -- laptop | sim | id_card | phone | peripheral | other
    serial        TEXT,
    purchase_date TEXT,
    cost          REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'in_stock', -- in_stock | assigned | repair | retired
    notes         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);

CREATE TABLE IF NOT EXISTS asset_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    assigned_on TEXT NOT NULL,
    returned_on TEXT,
    condition_note TEXT,
    assigned_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_assign_active ON asset_assignments(asset_id, returned_on);

CREATE TABLE IF NOT EXISTS expense_claims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    category      TEXT NOT NULL DEFAULT 'travel',  -- travel | food | accommodation | internet | other
    amount        REAL NOT NULL,
    expense_date  TEXT NOT NULL,
    description   TEXT,
    receipt_name  TEXT,
    receipt_mime  TEXT,
    receipt_size  INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | paid
    submitted_at  TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_status ON expense_claims(status, id);
CREATE INDEX IF NOT EXISTS idx_expenses_employee ON expense_claims(employee_id, expense_date);

CREATE TABLE IF NOT EXISTS work_locations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    address    TEXT,
    lat        REAL NOT NULL,
    lng        REAL NOT NULL,
    radius_m   INTEGER NOT NULL DEFAULT 200,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date   TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'check_in',   -- check_in | check_out
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    accuracy_m  REAL,
    distance_m  REAL,
    location_id INTEGER REFERENCES work_locations(id),
    location_name TEXT,
    inside      INTEGER NOT NULL DEFAULT 0,
    selfie_name TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_checks ON field_checks(employee_id, work_date);

CREATE TABLE IF NOT EXISTS tax_profiles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    fiscal_year      TEXT NOT NULL,             -- '2025-26'
    regime           TEXT NOT NULL DEFAULT 'new',  -- new | old
    metro            INTEGER NOT NULL DEFAULT 0,
    annual_rent      REAL NOT NULL DEFAULT 0,
    basic_annual     REAL NOT NULL DEFAULT 0,
    hra_annual       REAL NOT NULL DEFAULT 0,
    -- confirmed by HR / the CA; Form 16 reports these, not our projection
    confirmed_tds    REAL,
    confirmed_taxable REAL,
    monthly_tds      REAL NOT NULL DEFAULT 0,
    note             TEXT,
    confirmed_by     TEXT,
    confirmed_at     TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE (employee_id, fiscal_year)
);

CREATE TABLE IF NOT EXISTS tax_declarations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    fiscal_year TEXT NOT NULL,
    section     TEXT NOT NULL,                  -- 80C | 80CCD1B | 80D | 80TTA | 80E | 24B
    description TEXT,
    amount      REAL NOT NULL DEFAULT 0,
    declared_by TEXT,
    declared_at TEXT NOT NULL,
    proof_name  TEXT,
    proof_mime  TEXT,
    proof_size  INTEGER,
    status      TEXT NOT NULL DEFAULT 'declared',  -- declared | verified | rejected
    verified_by TEXT,
    verified_at TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tax_decl ON tax_declarations(employee_id, fiscal_year);

CREATE TABLE IF NOT EXISTS attendance_regularizations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date        TEXT NOT NULL,
    requested_status TEXT NOT NULL DEFAULT 'present',   -- present | half_day
    check_in         TEXT,
    check_out        TEXT,
    reason           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | withdrawn
    previous_status  TEXT,
    requested_by     TEXT,
    requested_at     TEXT NOT NULL,
    decided_by       TEXT,
    decided_at       TEXT,
    decision_note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_regularization_status ON attendance_regularizations(status, id);
CREATE INDEX IF NOT EXISTS idx_regularization_emp ON attendance_regularizations(employee_id, work_date);

CREATE TABLE IF NOT EXISTS employee_exits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    exit_type          TEXT NOT NULL DEFAULT 'resignation', -- resignation | termination | retirement | absconding | other
    initiated_by       TEXT NOT NULL DEFAULT 'employee',    -- employee | hr
    reason             TEXT,
    resignation_date   TEXT NOT NULL,
    notice_days        INTEGER NOT NULL DEFAULT 30,
    notice_waived      INTEGER NOT NULL DEFAULT 0,
    last_working_day   TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'requested',   -- requested | approved | settled | withdrawn | rejected
    extra_earnings     REAL NOT NULL DEFAULT 0,
    extra_deductions   REAL NOT NULL DEFAULT 0,
    recover_assets     INTEGER NOT NULL DEFAULT 1,
    fnf_note           TEXT,
    fnf_snapshot       TEXT,
    fnf_net            REAL,
    decided_by         TEXT,
    decided_at         TEXT,
    settled_by         TEXT,
    settled_at         TEXT,
    created_by         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exits_status ON employee_exits(status, id);
CREATE INDEX IF NOT EXISTS idx_exits_employee ON employee_exits(employee_id);

CREATE TABLE IF NOT EXISTS exit_checklist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    exit_id   INTEGER NOT NULL REFERENCES employee_exits(id) ON DELETE CASCADE,
    item      TEXT NOT NULL,
    done      INTEGER NOT NULL DEFAULT 0,
    done_by   TEXT,
    done_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_exit_checklist ON exit_checklist(exit_id);

CREATE TABLE IF NOT EXISTS appraisal_cycles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    self_due     TEXT,
    status       TEXT NOT NULL DEFAULT 'draft',   -- draft | open | closed
    goals_template TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    launched_at  TEXT,
    closed_at    TEXT
);

CREATE TABLE IF NOT EXISTS appraisals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id          INTEGER NOT NULL REFERENCES appraisal_cycles(id) ON DELETE CASCADE,
    employee_id       INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'self_review',  -- self_review | manager_review | completed
    self_comment      TEXT,
    self_submitted_at TEXT,
    self_score        REAL,
    manager_score     REAL,
    manager_comment   TEXT,
    final_rating      REAL,
    increment_pct     REAL,
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (cycle_id, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_appraisals_employee ON appraisals(employee_id);

CREATE TABLE IF NOT EXISTS appraisal_goals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    appraisal_id  INTEGER NOT NULL REFERENCES appraisals(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1,
    target        TEXT,
    self_score    REAL,
    self_note     TEXT,
    manager_score REAL,
    manager_note  TEXT,
    position      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_appraisal_goals ON appraisal_goals(appraisal_id);

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
        #: Read-mostly lookups that calendar code hits once per *day*. Cleared by
        #: any write that touches the table (see ``_invalidate``).
        self._settings_cache: dict[str, str] | None = None
        self._holidays_cache: dict[str, str] | None = None
        self._data_version: int | None = None
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()
        self._seed_settings()

    def _migrate(self) -> None:
        """Bring an older database up to date, in place.

        The ``users`` table was first created with
        ``CHECK (role IN ('hr','kiosk'))``, and SQLite cannot alter a CHECK
        constraint — so adding the ``employee`` role needs a table rebuild. Every
        existing row is copied across; the whole method is idempotent.
        """
        with self._lock:
            self._repair_dangling_user_refs()
            self._rebuild_users_if_needed()
            self._add_missing_columns()
            self._backfill_leave_ids()
            # Must exist on both paths (fresh database and migrated one).
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_employee ON users(employee_id)"
            )
            self._conn.commit()

    def _rebuild_users_if_needed(self) -> None:
        """Recreate ``users`` with the ``employee`` role, without breaking FKs.

        Two details matter here and both bit once already:

        * ``ALTER TABLE users RENAME TO users_old`` rewrites the foreign keys of
          *other* tables (``leave_requests.applied_by``) to point at ``users_old``,
          which then dangles as soon as the old table is dropped. So we build a new
          table, copy into it, drop the old one and rename the new one into place —
          and we keep ``legacy_alter_table`` on so the final rename does not touch
          anything else.
        * ``CREATE INDEX ... ON users(employee_id)`` cannot live in the base schema:
          on an un-migrated database that column does not exist yet.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if row is None:
            return

        create_sql = (row["sql"] or "").replace('"', "'")
        columns = {c["name"] for c in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        if "'employee'" in create_sql and "employee_id" in columns:
            return  # nothing to do

        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("PRAGMA legacy_alter_table=ON")
        self._conn.executescript(
            """
            CREATE TABLE users__new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                role          TEXT NOT NULL CHECK (role IN ('hr', 'kiosk', 'employee')),
                display_name  TEXT,
                device_label  TEXT,
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login_at TEXT,
                employee_id   INTEGER REFERENCES employees(id) ON DELETE CASCADE
            );
            """
        )
        employee_expr = "employee_id" if "employee_id" in columns else "NULL"
        self._conn.execute(
            f"""
            INSERT INTO users__new (id, username, password_hash, salt, role, display_name,
                                    device_label, active, created_at, last_login_at, employee_id)
            SELECT id, username, password_hash, salt, role, display_name,
                   device_label, active, created_at, last_login_at, {employee_expr}
            FROM users
            """
        )
        self._conn.execute("DROP TABLE users")
        self._conn.execute("ALTER TABLE users__new RENAME TO users")
        self._conn.execute("PRAGMA legacy_alter_table=OFF")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    def _repair_dangling_user_refs(self) -> None:
        """Undo damage from the first (buggy) migration.

        Databases migrated by the initial version of this code have other tables
        pointing at a ``users_old`` table that no longer exists, which makes every
        insert into them fail with ``no such table: main.users_old``. Rebuild each
        affected table in the safe order. Idempotent: a healthy database has no
        table whose definition mentions ``users_old``.
        """
        broken = self._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%users_old%'"
        ).fetchall()
        if not broken:
            return

        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("PRAGMA legacy_alter_table=ON")
        for row in broken:
            name, sql = row["name"], row["sql"]
            fixed_sql = re.sub(
                rf'CREATE\s+TABLE\s+"?{re.escape(name)}"?',
                f'CREATE TABLE "{name}__new"',
                sql.replace("users_old", "users"),
                count=1,
                flags=re.IGNORECASE,
            )
            index_sql = [
                r["sql"]
                for r in self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (name,),
                ).fetchall()
            ]
            columns = [c["name"] for c in self._conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
            column_list = ", ".join(f'"{c}"' for c in columns)

            self._conn.execute(fixed_sql)
            self._conn.execute(
                f'INSERT INTO "{name}__new" ({column_list}) SELECT {column_list} FROM "{name}"'
            )
            self._conn.execute(f'DROP TABLE "{name}"')
            self._conn.execute(f'ALTER TABLE "{name}__new" RENAME TO "{name}"')
            for statement in index_sql:
                self._conn.execute(statement)

        self._conn.execute("PRAGMA legacy_alter_table=OFF")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    #: Columns added after the first release: table -> [(column, declaration)]
    ADDITIVE_COLUMNS = {
        "users": [
            ("email", "TEXT"),
            ("phone", "TEXT"),
            ("session_version", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "payslips": [
            ("tds", "REAL NOT NULL DEFAULT 0"),
            ("tds_override", "REAL"),
        ],
        "attendance": [
            ("leave_id", "INTEGER"),
        ],
        "employees": [
            ("address", "TEXT"),
            ("bank_name", "TEXT"),
            ("account_number", "TEXT"),
            ("ifsc", "TEXT"),
            ("account_holder", "TEXT"),
            ("pan", "TEXT"),
            ("aadhaar_last4", "TEXT"),
            ("emergency_contact", "TEXT"),
        ],
    }

    def _backfill_leave_ids(self) -> None:
        """Link old leave-stamped attendance rows to their leave (they only had a note)."""
        rows = self._conn.execute(
            "SELECT id, note FROM attendance WHERE source = 'leave' AND leave_id IS NULL AND note LIKE 'leave #%'"
        ).fetchall()
        for row in rows:
            match = re.match(r"leave #(\d+)\b", row["note"] or "")
            if match:
                self._conn.execute("UPDATE attendance SET leave_id = ? WHERE id = ?", (int(match.group(1)), row["id"]))
        if rows:
            self._conn.commit()

    def _add_missing_columns(self) -> None:
        """``ALTER TABLE ... ADD COLUMN`` for anything added since the table was made.

        Adding a column is safe and cheap in SQLite; it never rewrites FKs, so this
        is the right tool for purely additive changes (bank details, user email).
        """
        for table, columns in self.ADDITIVE_COLUMNS.items():
            existing = {c["name"] for c in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not existing:
                continue
            for name, declaration in columns:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self._conn.commit()

    # ------------------------------------------------------------- plumbing
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """Context manager for atomic multi-statement transactions.

        Usage:
            with storage.transaction():
                storage.execute_tx("...")
        """
        with self._lock:
            try:
                yield self
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _invalidate(self, sql: str) -> None:
        lowered = sql.lower()
        if "app_settings" in lowered:
            self._settings_cache = None
        if "holidays" in lowered:
            self._holidays_cache = None

    def execute_tx(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Execute a statement without committing (used within a transaction block)."""
        with self._lock:
            self._invalidate(sql)
            return self._conn.execute(sql, tuple(params))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            self._invalidate(sql)
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._invalidate(sql)
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def _drop_stale_caches(self) -> None:
        """Another process (the CLI, a second worker) may have written the file."""
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self._data_version:
            self._data_version = version
            self._settings_cache = None
            self._holidays_cache = None

    def holidays(self) -> dict[str, str]:
        """``{'2026-01-26': 'Republic Day', ...}`` — cached until a holiday changes."""
        with self._lock:
            self._drop_stale_caches()
            if self._holidays_cache is None:
                self._holidays_cache = {
                    row["day"]: row["name"] for row in self._conn.execute("SELECT day, name FROM holidays")
                }
            return self._holidays_cache

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
        with self._lock:
            self._drop_stale_caches()
            if self._settings_cache is None:
                self._settings_cache = {
                    row["key"]: row["value"] for row in self._conn.execute("SELECT key, value FROM app_settings")
                }
            cached = self._settings_cache.get(key)
        if cached is not None:
            return cached
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
        self.get_setting("org_name")  # warm the cache
        with self._lock:
            return dict(self._settings_cache or {})

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
