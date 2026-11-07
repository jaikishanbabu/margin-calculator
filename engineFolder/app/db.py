"""Database layer: the same SQL (with `?` placeholders) runs on SQLite (dev/test) and Postgres.

On Postgres the app connects as the restricted role `margin_app` and works inside schema
`margin`. It cannot run DDL, so it only *checks* that migrations were applied.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

MIGRATIONS_DIR = config.BASE_DIR / "migrations"
REQUIRED_PG_MIGRATIONS = {"001_init", "002_app_role", "003_must_change_password", "004_archive_encoding", "005_fetcher"}

_local = threading.local()
_pg_pool = None
_lock = threading.RLock()


def is_pg():
    return config.DATABASE_URL.startswith(("postgres://", "postgresql://"))


def _pool():
    global _pg_pool
    if _pg_pool is None:
        with _lock:
            if _pg_pool is None:
                from psycopg2.pool import ThreadedConnectionPool
                _pg_pool = ThreadedConnectionPool(1, 6, config.DATABASE_URL, connect_timeout=15)
    return _pg_pool


def _sqlite():
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != config.SQLITE_PATH:
        os.makedirs(os.path.dirname(config.SQLITE_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(config.SQLITE_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn, _local.path = conn, config.SQLITE_PATH
    return conn


class Cursor:
    def __init__(self, cur, pg):
        self._cur, self._pg = cur, pg

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("?", "%s") if self._pg else sql, tuple(params))
        return self

    def _rows(self, rows):
        if self._pg:
            cols = [d[0] for d in self._cur.description]
            return [dict(zip(cols, r)) for r in rows]
        return [dict(r) for r in rows]

    def fetchall(self):
        return self._rows(self._cur.fetchall())

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else self._rows([r])[0]

    def insert(self, sql, params=()):
        self.execute(sql + " RETURNING id", params)
        return self._cur.fetchone()[0]

    def lock(self, key: int):
        """Serialise a critical section (used for the audit hash chain)."""
        if self._pg:
            self._cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        # SQLite: BEGIN IMMEDIATE (see tx) already serialises writers.


@contextmanager
def tx():
    if is_pg():
        pool = _pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SET search_path TO margin")
            yield Cursor(cur, True)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    else:
        conn = _sqlite()
        with _lock:  # one writer at a time in-process
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield Cursor(cur, False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def query(sql, params=()):
    with tx() as c:
        return c.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with tx() as c:
        return c.execute(sql, params).fetchone()


def init():
    """SQLite: apply migrations. Postgres: verify they were applied by the schema owner."""
    if is_pg():
        try:
            rows = query("SELECT version FROM schema_migrations")
        except Exception as e:
            raise RuntimeError(f"Database schema not found - apply migrations/postgres/*.sql first ({e})")
        missing = REQUIRED_PG_MIGRATIONS - {r["version"] for r in rows}
        if missing:
            raise RuntimeError(f"Missing database migrations: {sorted(missing)}")
        return
    conn = _sqlite()
    with _lock:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted((MIGRATIONS_DIR / "sqlite").glob("*.sql")):
            if f.stem not in done:
                conn.executescript(f.read_text())
        conn.commit()


def get_setting(key, default=None):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    with tx() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def table_names():
    return ["users", "audit_log", "uploads", "backup_runs", "settings", "fetch_runs", "fetch_items"]
