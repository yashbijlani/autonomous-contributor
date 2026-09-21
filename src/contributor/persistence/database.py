"""SQLite persistence with a PostgreSQL-compatible schema.

Schema uses only portable SQL (TEXT/INTEGER/REAL) so it can be migrated to
PostgreSQL by swapping the connection layer. All job state is serialized as
JSON in the `jobs.state_json` column; queryable columns are duplicated for
filtering. Every state transition must call JobRepository.save().
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL DEFAULT '',
    issue_number INTEGER NOT NULL DEFAULT 0,
    current_state TEXT NOT NULL DEFAULT 'created',
    escalated INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    pull_request_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_repo_state ON jobs(repository, current_state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    type TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS model_health (
    model_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    variant TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    latency_s REAL NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    last_checked REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    variant TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    est_tokens INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_job ON model_usage(job_id);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


class Database:
    """Thin SQLite wrapper. For PostgreSQL, replace _connect with psycopg pool
    keeping the same method signatures."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        if database_url.startswith("sqlite:///"):
            raw = database_url[len("sqlite:///") :]
            self._path: Path | None = None if raw == ":memory:" else Path(raw)
            self._memory = raw == ":memory:"
        elif database_url.startswith("sqlite://"):
            self._path = Path(database_url[len("sqlite://") :])
            self._memory = False
        elif database_url.startswith("postgresql"):
            raise RuntimeError(
                "PostgreSQL URL configured but psycopg backend not installed in this build; "
                "schema in database.py is postgres-compatible — install psycopg and wire Database to it."
            )
        else:
            # treat as plain path
            self._path = Path(database_url)
            self._memory = False
        self._lock = threading.Lock()
        if self._memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.executescript(SCHEMA)
        else:
            assert self._path is not None
            conn = _connect(self._path)
            conn.executescript(SCHEMA)
            conn.commit()
            conn.close()
            self._mem_conn = None

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            assert self._mem_conn is not None
            return self._mem_conn
        assert self._path is not None
        return _connect(self._path)

    def execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
                conn.commit()
                return rows
            finally:
                if not self._memory:
                    conn.close()

    def execute_write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount
            finally:
                if not self._memory:
                    conn.close()


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def with_retries(fn, *, tries: int = 5, base_delay: float = 0.1):
    """Exponential backoff for transient DB failures."""
    last: BaseException | None = None
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(base_delay * (2**i))
    assert last is not None
    raise last
