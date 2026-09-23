"""Database access for the hub: PostgreSQL (production) or SQLite (tests, trials).

Deliberately thin: plain SQL through the DB-API, no ORM. SQL in this package is
written once with ``?`` placeholders and translated for psycopg (``%s``). Keep ``%``
out of SQL text for that reason; pass patterns as parameters.

    db = Database.connect("postgresql://hub@db/hub")   # needs psycopg 3
    db = Database.connect("sqlite:///data/hub.db")
    with db.tx() as c:
        c.execute("SELECT ... WHERE id = ?", (x,))

Timestamps are stored as ISO-8601 UTC text with a fixed format, so they sort and
compare correctly as strings in both databases.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)


class IntegrityError(Exception):
    """A unique or primary key was violated (e.g. someone else took the lock first)."""


class Cursor:
    def __init__(self, raw, dialect: str):
        self._c = raw
        self.dialect = dialect

    def execute(self, sql: str, params: tuple | list = ()) -> "Cursor":
        if self.dialect == "postgresql":
            sql = sql.replace("?", "%s")
        try:
            self._c.execute(sql, tuple(params))
        except Exception as exc:                      # noqa: BLE001 - driver-specific types
            if _is_integrity(exc):
                raise IntegrityError(str(exc)) from None
            raise
        return self

    def _cols(self) -> list[str]:
        return [d[0] for d in self._c.description or []]

    def one(self) -> dict | None:
        row = self._c.fetchone()
        return dict(zip(self._cols(), row)) if row is not None else None

    def all(self) -> list[dict]:
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self._c.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._c.rowcount


def _is_integrity(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    # psycopg: errors.UniqueViolation etc. subclass psycopg.IntegrityError
    return any(k.__name__ == "IntegrityError" for k in type(exc).__mro__)


class Database:
    def __init__(self, url: str):
        self.url = url
        if url.startswith(("postgresql://", "postgres://")):
            self.dialect = "postgresql"
        elif url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            self.path = url[len("sqlite:///"):]
        else:
            raise ValueError("HUB_DATABASE_URL must start with postgresql:// or sqlite:///")
        self._local = threading.local()
        self._sqlite_lock = threading.Lock()

    @classmethod
    def connect(cls, url: str | None = None) -> "Database":
        url = url or os.environ.get("HUB_DATABASE_URL", "")
        if not url:
            raise ValueError("HUB_DATABASE_URL is not set")
        return cls(url)

    # one connection per thread; FastAPI runs sync endpoints on a thread pool
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            if self.dialect == "postgresql" and conn.closed:
                conn = None
            else:
                return conn
        if self.dialect == "postgresql":
            import psycopg
            conn = psycopg.connect(self.url, autocommit=False)
            if conn.info.parameter_status("server_encoding") != "UTF8":
                conn.close()
                raise RuntimeError("the hub database must use UTF8 encoding: CREATE DATABASE "
                                   "hub ENCODING 'UTF8' TEMPLATE template0")
        else:
            if self.path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                   check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
        self._local.conn = conn
        return conn

    @contextlib.contextmanager
    def tx(self):
        """One transaction. Commits on success, rolls back on any exception.

        SQLite: BEGIN IMMEDIATE takes the write lock up front, so two writers queue
        instead of failing half way. PostgreSQL: row locks (FOR UPDATE SKIP LOCKED) in
        the job queue give the same guarantee without blocking readers."""
        conn = self._conn()
        if self.dialect == "sqlite":
            conn.execute("BEGIN IMMEDIATE")
            cur = Cursor(conn.cursor(), "sqlite")
            try:
                yield cur
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        else:
            cur = Cursor(conn.cursor(), "postgresql")
            try:
                yield cur
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def ping(self) -> bool:
        try:
            with self.tx() as c:
                c.execute("SELECT 1 AS ok")
                return c.one()["ok"] == 1
        except Exception:                             # noqa: BLE001
            return False
