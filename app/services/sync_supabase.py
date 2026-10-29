"""Supabase Cloud Database Sync Engine (Hybrid SQLite + PostgreSQL).

Provides continuous, non-blocking background replication of SQLite data to Supabase.
- Local SQLite handles all reads & writes with microsecond latency (< 0.1ms).
- Supabase Cloud acts as a live, secure replica and web dashboard.
- If internet is temporarily unavailable, local scans continue uninterrupted.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
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


def get_pg_connection(db_url: str):
    """Open an SSL-encrypted PostgreSQL connection to Supabase pooler."""
    if not HAS_PSYCOPG2:
        raise RuntimeError("psycopg2 is required for Supabase sync.")
    return psycopg2.connect(db_url, sslmode="require", connect_timeout=12)


def sync_all(storage: Storage, settings: Settings) -> dict[str, int]:
    """Mirror SQLite tables and upsert all rows to Supabase PostgreSQL."""
    if not settings.supabase_sync_enabled or not settings.supabase_db_url or not HAS_PSYCOPG2:
        return {}

    started = time.perf_counter()
    results: dict[str, int] = {}

    pg_conn = None
    try:
        pg_conn = get_pg_connection(settings.supabase_db_url)
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

                rows = s_cur.execute(f'SELECT * FROM "{tbl}"').fetchall()
                if not rows:
                    results[tbl] = 0
                    continue

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
        logger.info("Supabase sync complete: %d rows across %d tables in %.2fs", total_rows, len(results), elapsed)
        return results

    except Exception as exc:
        if pg_conn:
            try:
                pg_conn.rollback()
            except Exception:
                pass
        logger.warning("Supabase sync paused (will retry): %s", exc)
        return {}
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


class SupabaseSyncWorker:
    """Background daemon thread that periodically replicates SQLite to Supabase."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_synced: float = 0

    def start(self) -> None:
        if not self.settings.supabase_sync_enabled or not HAS_PSYCOPG2:
            logger.info("Supabase background sync is disabled or psycopg2 is missing.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="supabase-sync", daemon=True)
        self._thread.start()
        logger.info("Supabase background sync started (interval=%ds)", self.settings.supabase_sync_interval)

    def trigger(self) -> None:
        """Wake up the sync worker immediately."""
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("Supabase background sync stopped.")

    def _run(self) -> None:
        # Initial sync on startup
        time.sleep(2)
        if not self._stop_event.is_set():
            sync_all(self.storage, self.settings)
            self._last_synced = time.time()

        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self.settings.supabase_sync_interval)
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            sync_all(self.storage, self.settings)
            self._last_synced = time.time()

