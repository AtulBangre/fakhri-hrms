"""Supabase Cloud Database Sync Engine (Hybrid SQLite + PostgreSQL).

Provides continuous, non-blocking background replication of SQLite data to Supabase.
- Local SQLite handles all reads & writes with microsecond latency (< 0.1ms).
- Supabase Cloud acts as a live, secure replica and web dashboard.
- If internet is temporarily unavailable, local scans continue uninterrupted.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

from ..config import Settings
from ..db import Storage

logger = logging.getLogger("hrms.supabase")

try:
    import psycopg2
    from psycopg2.extras import execute_batch
    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    HAS_PSYCOPG2 = False
    logger.warning("psycopg2-binary not installed — Supabase cloud sync is disabled.")


def map_type(sqlite_type: str) -> str:
    """Map SQLite types to PostgreSQL types."""
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "BIGINT"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "DOUBLE PRECISION"
    if "BLOB" in t:
        return "BYTEA"
    return "TEXT"


def mask_url(url: str) -> str:
    """Safely mask database password in connection string for display."""
    if not url:
        return ""
    return re.sub(r":([^:@]+)@", r":••••••••@", url)


def get_effective_config(storage: Storage, settings: Settings) -> dict[str, Any]:
    """Resolve active sync configuration, preferring database settings over env defaults."""
    db_enabled = storage.get_setting("supabase_sync_enabled", "")
    if db_enabled in ("1", "true", "yes", "on"):
        enabled = True
    elif db_enabled in ("0", "false", "no", "off"):
        enabled = False
    else:
        enabled = settings.supabase_sync_enabled

    db_url = storage.get_setting("supabase_db_url", "").strip() or settings.supabase_db_url.strip()
    interval = storage.get_int("supabase_sync_interval", settings.supabase_sync_interval)

    return {
        "enabled": enabled,
        "db_url": db_url,
        "interval": max(15, interval),
    }


def get_pg_connection(db_url: str):
    """Open an SSL-encrypted PostgreSQL connection to Supabase pooler."""
    if not HAS_PSYCOPG2:
        raise RuntimeError("psycopg2 is required for Supabase sync.")
    return psycopg2.connect(db_url, sslmode="require", connect_timeout=12)


def test_connection(db_url: str) -> tuple[bool, str]:
    """Test connection to a PostgreSQL database endpoint."""
    if not HAS_PSYCOPG2:
        return False, "psycopg2-binary is not installed in the environment."
    clean_url = (db_url or "").strip()
    if not clean_url:
        return False, "Database connection URL cannot be empty."

    pg_conn = None
    try:
        pg_conn = get_pg_connection(clean_url)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT version();")
            row = cur.fetchone()
            ver_text = str(row[0])[:60] if row else "PostgreSQL"
        return True, f"Connection successful! Handshake verified with {ver_text}"
    except Exception as exc:
        return False, f"Connection failed: {exc}"
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


def _source_has_data(storage: Storage) -> bool:
    """Guard: an empty source would otherwise truncate the whole cloud mirror."""
    try:
        with sqlite3.connect(f"file:{storage.db_path}?mode=ro", uri=True, timeout=5) as conn:
            return all(
                conn.execute(f'SELECT EXISTS(SELECT 1 FROM "{table}")').fetchone()[0]
                for table in ("employees", "users")
            )
    except sqlite3.Error as exc:
        logger.warning("Supabase sync aborted: cannot read the local database (%s)", exc)
        return False


#: Never mirrored: the outbox holds whole email bodies (big, and personal), and these
#: tables hold sign-in secrets or short-lived security state.
EXCLUDED_TABLES = {"notifications", "login_attempts", "otp_codes", "password_tokens"}
#: Rows are only ever added, never changed: sync just the new ones.
APPEND_ONLY_TABLES = {"audit_log", "loan_repayments"}
#: Rows sent per round trip / held in memory at once.
BATCH_ROWS = 500
#: Even with incremental sync, reconcile every table in full this often.
FULL_RESYNC_SECONDS = 6 * 3600


def _table_signature(cur: sqlite3.Cursor, table: str, has_updated: bool) -> tuple:
    rowid = "rowid"
    try:
        row = cur.execute(
            f'SELECT COUNT(*), MAX({rowid}){", MAX(updated_at)" if has_updated else ""} FROM "{table}"'
        ).fetchone()
    except sqlite3.OperationalError:  # WITHOUT ROWID table
        row = cur.execute(
            f'SELECT COUNT(*), NULL{", MAX(updated_at)" if has_updated else ""} FROM "{table}"'
        ).fetchone()
    return tuple(row)


def sync_database(
    storage: Storage,
    settings: Settings,
    override_url: str | None = None,
    force: bool = False,
    state: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, int]]:
    """Mirror the SQLite tables to Supabase PostgreSQL.

    * Rows are streamed in batches of ``BATCH_ROWS`` — the old version loaded every row
      of every table into memory twice per run, enough to push the container to its
      memory limit as the database grew.
    * With ``state`` (the background worker), unchanged tables are skipped and tables
      with ``updated_at`` (or append-only logs) send only rows changed since last time.
      Without it (the "Sync now" button), everything is reconciled in full.
    """
    cfg = get_effective_config(storage, settings)
    target_url = (override_url or cfg["db_url"]).strip()

    if not HAS_PSYCOPG2:
        return False, "psycopg2-binary is not installed.", {}
    if not target_url:
        return False, "Supabase PostgreSQL Database URL is not configured.", {}
    if not cfg["enabled"] and not override_url and not force:
        return False, "Supabase Cloud Sync is currently disabled in settings.", {}

    if not _source_has_data(storage):
        logger.warning("Supabase sync aborted: the local employees or users table is empty.")
        return False, "Local database is empty (employees or users table has no records).", {}

    started = time.perf_counter()
    results: dict[str, int] = {}
    pg_conn = None
    marks: dict[str, Any] = state.get("tables", {}) if state is not None else {}
    incremental = state is not None and (time.time() - float(state.get("full_at") or 0)) < FULL_RESYNC_SECONDS
    new_marks: dict[str, Any] = {}

    try:
        pg_conn = get_pg_connection(target_url)
        pg_cur = pg_conn.cursor()

        # Direct read connection to SQLite in WAL mode (readers never block the app)
        with sqlite3.connect(storage.db_path, timeout=5) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()

            tables = [
                row[0]
                for row in s_cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if row[0] not in EXCLUDED_TABLES
            ]

            for tbl in tables:
                cols_info = s_cur.execute(f'PRAGMA table_info("{tbl}")').fetchall()
                if not cols_info:
                    continue
                col_names = [c["name"] for c in cols_info]
                pks = [f'"{c["name"]}"' for c in cols_info if c["pk"]]
                has_updated = "updated_at" in col_names
                signature = _table_signature(s_cur, tbl, has_updated)
                previous = marks.get(tbl)
                if incremental and previous and previous["sig"] == signature:
                    new_marks[tbl] = previous
                    continue  # nothing changed in this table since the last run

                col_defs = [f'"{c["name"]}" {map_type(c["type"])}' for c in cols_info]
                pk_clause = f", PRIMARY KEY ({', '.join(pks)})" if pks else ""
                pg_cur.execute(f'CREATE TABLE IF NOT EXISTS "{tbl}" ({", ".join(col_defs)}{pk_clause});')
                pg_cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (tbl,)
                )
                existing_pg_cols = {r[0] for r in pg_cur.fetchall()}
                for c in cols_info:
                    if c["name"] not in existing_pg_cols:
                        try:
                            pg_cur.execute(
                                f'ALTER TABLE "{tbl}" ADD COLUMN IF NOT EXISTS "{c["name"]}" {map_type(c["type"])}'
                            )
                        except Exception:
                            pass

                count = int(signature[0] or 0)
                if count == 0:
                    try:
                        pg_cur.execute(f'TRUNCATE TABLE "{tbl}" CASCADE')
                    except Exception:
                        pass
                    results[tbl] = 0
                    new_marks[tbl] = {"sig": signature}
                    continue

                # which rows to send: only the changed ones when we know where we left off
                where, params = "", ()
                if incremental and previous:
                    if has_updated and previous["sig"][2]:
                        # >=: a row changed in the same second as the last run is not missed
                        where, params = " WHERE updated_at >= ?", (previous["sig"][2],)
                    elif tbl in APPEND_ONLY_TABLES and previous["sig"][1] is not None:
                        where, params = " WHERE rowid > ?", (previous["sig"][1],)

                # rows deleted locally: remove them remotely (ids only, never whole rows)
                if len(pks) == 1 and (not incremental or not previous or count < int(previous["sig"][0] or 0)
                                      or not where):
                    pk_raw = pks[0].strip('"')
                    local_ids = tuple(r[0] for r in s_cur.execute(f'SELECT "{pk_raw}" FROM "{tbl}"'))
                    if local_ids:
                        try:
                            pg_cur.execute(f'DELETE FROM "{tbl}" WHERE "{pk_raw}" NOT IN %s', (local_ids,))
                        except Exception:
                            pass

                cols_escaped = ", ".join(f'"{c}"' for c in col_names)
                placeholders = ", ".join(["%s"] * len(col_names))
                if pks:
                    non_pks = [f'"{c}" = EXCLUDED."{c}"' for c in col_names if f'"{c}"' not in pks]
                    conflict_action = f"DO UPDATE SET {', '.join(non_pks)}" if non_pks else "DO NOTHING"
                    upsert_sql = (f'INSERT INTO "{tbl}" ({cols_escaped}) VALUES ({placeholders}) '
                                  f'ON CONFLICT ({", ".join(pks)}) {conflict_action}')
                else:
                    pg_cur.execute(f'TRUNCATE TABLE "{tbl}"')  # no key to upsert on: replace
                    where, params = "", ()
                    upsert_sql = f'INSERT INTO "{tbl}" ({cols_escaped}) VALUES ({placeholders})'

                sent = 0
                reader = s_conn.execute(f'SELECT {cols_escaped} FROM "{tbl}"{where}', params)
                while True:
                    chunk = reader.fetchmany(BATCH_ROWS)
                    if not chunk:
                        break
                    execute_batch(pg_cur, upsert_sql, [tuple(row) for row in chunk], page_size=100)
                    sent += len(chunk)
                results[tbl] = sent
                new_marks[tbl] = {"sig": signature}

        pg_conn.commit()
        if state is not None:
            state["tables"] = new_marks
            if not incremental:
                state["full_at"] = time.time()
        elapsed = time.perf_counter() - started
        total_rows = sum(results.values())
        skipped = len([t for t in new_marks if t not in results])
        msg = (f"Synced {total_rows} rows across {len(results)} tables in {elapsed:.2f}s"
               + (f" ({skipped} unchanged tables skipped)." if skipped else "."))
        logger.info("Supabase sync complete: %s", msg)
        return True, msg, results

    except Exception as exc:
        if pg_conn:
            try:
                pg_conn.rollback()
            except Exception:
                pass
        err_msg = f"Supabase sync failed: {exc}"
        logger.warning(err_msg)
        return False, err_msg, {}
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


def sync_all(storage: Storage, settings: Settings) -> dict[str, int]:
    """Mirror SQLite tables and upsert all rows to Supabase PostgreSQL (backward-compatible)."""
    ok, _, results = sync_database(storage, settings)
    return results if ok else {}


def get_data_version(storage: Storage, conn: sqlite3.Connection | None = None) -> int:
    """``PRAGMA data_version`` changes when *another* connection commits — but only as seen
    from the same connection over time. A fresh connection per call (the old code) always
    reads the same number, so changes were never noticed. Pass a long-lived ``conn``.
    """
    try:
        if conn is not None:
            row = conn.execute("PRAGMA data_version").fetchone()
            return int(row[0]) if row else 0
        with sqlite3.connect(storage.db_path, timeout=5) as fresh:
            row = fresh.execute("PRAGMA data_version").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def get_sync_status(
    storage: Storage,
    settings: Settings,
    worker: SupabaseSyncWorker | None = None,
) -> dict[str, Any]:
    """Summarize the live status of Supabase Cloud Sync for the HR console."""
    cfg = get_effective_config(storage, settings)
    last_synced_str = ""
    last_synced_sec = None
    if worker and worker._last_synced:
        last_synced_sec = int(time.time() - worker._last_synced)
        last_synced_str = datetime.fromtimestamp(worker._last_synced).strftime("%d %b %Y, %I:%M %p")

    return {
        "has_psycopg2": HAS_PSYCOPG2,
        "enabled": cfg["enabled"],
        "db_url_configured": bool(cfg["db_url"]),
        "db_url_raw": cfg["db_url"],
        "masked_db_url": mask_url(cfg["db_url"]),
        "interval": cfg["interval"],
        "is_running": (worker._thread is not None and worker._thread.is_alive()) if worker else False,
        "last_synced": last_synced_str,
        "last_synced_seconds": last_synced_sec,
        "last_error": worker._last_error if worker else None,
        "last_stats": worker._last_stats if worker else {},
        "total_rows_synced": sum(worker._last_stats.values()) if (worker and worker._last_stats) else 0,
    }


class SupabaseSyncWorker:
    """Background daemon thread that periodically replicates SQLite to Supabase."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_synced: float = 0
        self._last_data_version: int | None = None
        self._last_error: str | None = None
        self._last_stats: dict[str, int] = {}

    def start(self) -> None:
        cfg = get_effective_config(self.storage, self.settings)
        if not cfg["enabled"] or not HAS_PSYCOPG2:
            logger.info("Supabase background sync is disabled or psycopg2 is missing.")
            return

        if self._thread and self._thread.is_alive():
            self._wake_event.set()
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="supabase-sync", daemon=True)
        self._thread.start()
        logger.info("Supabase background sync engine started.")

    def trigger(self) -> None:
        """Wake up sync worker immediately (and start thread if enabled and stopped)."""
        cfg = get_effective_config(self.storage, self.settings)
        if cfg["enabled"]:
            if not self._thread or not self._thread.is_alive():
                self.start()
            self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("Supabase background sync stopped.")

    def _run(self) -> None:
        time.sleep(2)
        probe: sqlite3.Connection | None = None
        state: dict[str, Any] = {}
        failures = 0
        while not self._stop_event.is_set():
            cfg = get_effective_config(self.storage, self.settings)
            if not cfg["enabled"] or not cfg["db_url"]:
                # Idle poll every 15s to check if admin enabled it
                self._wake_event.wait(timeout=15)
                self._wake_event.clear()
                continue

            if probe is None:
                try:
                    probe = sqlite3.connect(self.storage.db_path, timeout=5, check_same_thread=False)
                except sqlite3.Error:
                    probe = None
            curr_ver = get_data_version(self.storage, probe)
            wait = cfg["interval"]
            if self._last_data_version is not None and curr_ver == self._last_data_version:
                logger.debug("Database data_version unchanged (%d), skipping Supabase sync", curr_ver)
            else:
                ok, msg, stats = sync_database(self.storage, self.settings, state=state)
                if ok:
                    failures = 0
                    self._last_synced = time.time()
                    self._last_data_version = curr_ver
                    self._last_error = None
                    self._last_stats = stats
                else:
                    # back off instead of retrying a full copy every interval
                    failures += 1
                    self._last_error = msg
                    wait = min(cfg["interval"] * (2 ** min(failures, 6)), 1800)

            self._wake_event.wait(timeout=wait)
            self._wake_event.clear()
        if probe is not None:
            probe.close()
