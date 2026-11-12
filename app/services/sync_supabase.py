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


def sync_database(
    storage: Storage,
    settings: Settings,
    override_url: str | None = None,
    force: bool = False,
) -> tuple[bool, str, dict[str, int]]:
    """Synchronize all SQLite tables and upsert rows to Supabase PostgreSQL."""
    cfg = get_effective_config(storage, settings)
    target_url = (override_url or cfg["db_url"]).strip()

    if not HAS_PSYCOPG2:
        return False, "psycopg2-binary is not installed in the environment.", {}
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

    try:
        pg_conn = get_pg_connection(target_url)
        pg_cur = pg_conn.cursor()

        # Direct read connection to SQLite in WAL mode
        with sqlite3.connect(storage.db_path, timeout=5) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()

            tables = [
                row[0]
                for row in s_cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]

            for tbl in tables:
                cols_info = s_cur.execute(f'PRAGMA table_info("{tbl}")').fetchall()
                if not cols_info:
                    continue

                col_defs = []
                pks = []
                col_names = []
                for c in cols_info:
                    c_name = c["name"]
                    col_names.append(c_name)
                    c_type = map_type(c["type"])
                    if c["pk"]:
                        pks.append(f'"{c_name}"')
                    col_defs.append(f'"{c_name}" {c_type}')

                pk_clause = f", PRIMARY KEY ({', '.join(pks)})" if pks else ""
                create_sql = f'CREATE TABLE IF NOT EXISTS "{tbl}" ({", ".join(col_defs)}{pk_clause});'
                pg_cur.execute(create_sql)

                # Ensure missing columns in PostgreSQL are added
                pg_cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (tbl,),
                )
                existing_pg_cols = {r[0] for r in pg_cur.fetchall()}
                for c in cols_info:
                    c_name = c["name"]
                    if c_name not in existing_pg_cols:
                        c_type = map_type(c["type"])
                        try:
                            pg_cur.execute(f'ALTER TABLE "{tbl}" ADD COLUMN IF NOT EXISTS "{c_name}" {c_type}')
                        except Exception:
                            pass

                rows = s_cur.execute(f'SELECT * FROM "{tbl}"').fetchall()
                if not rows:
                    try:
                        pg_cur.execute(f'TRUNCATE TABLE "{tbl}" CASCADE')
                    except Exception:
                        pass
                    results[tbl] = 0
                    continue

                if pks and len(pks) == 1:
                    pk_raw = pks[0].strip('"')
                    local_ids = [row[pk_raw] for row in rows if row[pk_raw] is not None]
                    if local_ids:
                        try:
                            pg_cur.execute(f'DELETE FROM "{tbl}" WHERE "{pk_raw}" NOT IN %s', (tuple(local_ids),))
                        except Exception:
                            pass

                placeholders = ", ".join(["%s"] * len(col_names))
                cols_escaped = ", ".join([f'"{c}"' for c in col_names])

                if pks:
                    non_pks = [f'"{c}" = EXCLUDED."{c}"' for c in col_names if f'"{c}"' not in pks]
                    conflict_action = f"DO UPDATE SET {', '.join(non_pks)}" if non_pks else "DO NOTHING"
                    upsert_sql = (
                        f'INSERT INTO "{tbl}" ({cols_escaped}) VALUES ({placeholders}) '
                        f'ON CONFLICT ({", ".join(pks)}) {conflict_action}'
                    )
                else:
                    upsert_sql = f'INSERT INTO "{tbl}" ({cols_escaped}) VALUES ({placeholders})'

                batch_data = [[row[c] for c in col_names] for row in rows]
                execute_batch(pg_cur, upsert_sql, batch_data, page_size=100)
                results[tbl] = len(rows)

        pg_conn.commit()
        elapsed = time.perf_counter() - started
        total_rows = sum(results.values())
        msg = f"Synced {total_rows} rows across {len(results)} tables in {elapsed:.2f}s."
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


def get_data_version(storage: Storage) -> int:
    try:
        with sqlite3.connect(storage.db_path, timeout=5) as conn:
            row = conn.execute("PRAGMA data_version").fetchone()
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
        while not self._stop_event.is_set():
            cfg = get_effective_config(self.storage, self.settings)
            if not cfg["enabled"] or not cfg["db_url"]:
                # Idle poll every 15s to check if admin enabled it
                self._wake_event.wait(timeout=15)
                self._wake_event.clear()
                continue

            curr_ver = get_data_version(self.storage)
            if self._last_data_version is not None and curr_ver == self._last_data_version:
                logger.debug("Database data_version unchanged (%d), skipping Supabase sync", curr_ver)
            else:
                ok, msg, stats = sync_database(self.storage, self.settings)
                if ok:
                    self._last_synced = time.time()
                    self._last_data_version = curr_ver
                    self._last_error = None
                    self._last_stats = stats
                else:
                    self._last_error = msg

            self._wake_event.wait(timeout=cfg["interval"])
            self._wake_event.clear()
