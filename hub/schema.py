"""Schema and migrations for the hub.

Migrations are numbered, applied in order, never edited once released (add a new one
instead), and recorded in ``schema_migrations``, so every database says exactly which
schema it runs. This is the change record for the data layer (ITIL change enablement).

    python -m hub.schema migrate       # also runs at API / worker start-up
    python -m hub.schema status

Every table carries ``tenant_id`` and every query filters by it: tenant isolation is
enforced in the data layer, not only in the API.
"""
from __future__ import annotations

import sys

from .db import Database, IntegrityError, ts, utcnow

# {dialect: sql}; "*" = same for both
MIGRATIONS: list[tuple[int, str, dict[str, str]]] = [
    (1, "surveys, recordings, locks, jobs, audit anchors", {
        "*": """
CREATE TABLE surveys (
    tenant_id   TEXT NOT NULL,
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE recordings (
    tenant_id    TEXT NOT NULL,
    survey_id    TEXT NOT NULL,
    id           TEXT NOT NULL,
    status       TEXT NOT NULL,
    reviewer     TEXT,
    reviewer_name TEXT,
    log_sha256   TEXT,
    qa_by        TEXT,
    qa_by_name   TEXT,
    qa_decision  TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, survey_id, id),
    FOREIGN KEY (tenant_id, survey_id) REFERENCES surveys (tenant_id, id)
);
CREATE TABLE locks (
    tenant_id     TEXT NOT NULL,
    survey_id     TEXT NOT NULL,
    recording_id  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    holder        TEXT NOT NULL,
    holder_name   TEXT,
    token         TEXT NOT NULL UNIQUE,
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, survey_id, recording_id)
);
CREATE TABLE audit_anchors (
    tenant_id    TEXT NOT NULL,
    survey_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    head_hash    TEXT NOT NULL,
    action       TEXT,
    anchored_by  TEXT NOT NULL,
    anchored_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, survey_id, seq)
);
""",
        "sqlite": """
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    survey_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    params        TEXT NOT NULL,
    status        TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    run_after     TEXT NOT NULL,
    locked_by     TEXT,
    locked_until  TEXT,
    created_by    TEXT NOT NULL,
    created_by_name TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    result        TEXT,
    error         TEXT
);
CREATE INDEX jobs_ready ON jobs (status, run_after, priority);
""",
        "postgresql": """
CREATE TABLE jobs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    survey_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    params        TEXT NOT NULL,
    status        TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    run_after     TEXT NOT NULL,
    locked_by     TEXT,
    locked_until  TEXT,
    created_by    TEXT NOT NULL,
    created_by_name TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    result        TEXT,
    error         TEXT
);
CREATE INDEX jobs_ready ON jobs (status, run_after, priority);
""",
    }),
]

LATEST = MIGRATIONS[-1][0]


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def current(db: Database) -> int:
    with db.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                  "version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
                  "applied_at TEXT NOT NULL)")
        c.execute("SELECT MAX(version) AS v FROM schema_migrations")
        row = c.one()
    return int(row["v"] or 0)


def migrate(db: Database) -> list[int]:
    """Apply pending migrations, each in its own transaction. Safe to run from several
    processes at once: a migration another process applied first is skipped."""
    applied = []
    have = current(db)
    for version, desc, sql in MIGRATIONS:
        if version <= have:
            continue
        try:
            with db.tx() as c:
                c.execute("INSERT INTO schema_migrations (version, description, applied_at) "
                          "VALUES (?, ?, ?)", (version, desc, ts(utcnow())))
                for part in ("*", db.dialect):
                    for stmt in _statements(sql.get(part, "")):
                        c.execute(stmt)
        except IntegrityError:
            continue                      # applied concurrently by another process
        applied.append(version)
    if current(db) > LATEST:
        raise RuntimeError(f"database schema {current(db)} is newer than this hub "
                           f"(knows up to {LATEST}); upgrade the hub, don't run old code")
    return applied


def main(argv: list[str]) -> int:
    db = Database.connect()
    if argv[:1] == ["migrate"]:
        done = migrate(db)
        print(f"applied: {done or 'nothing'}; schema is at {current(db)}")
        return 0
    print(f"schema {current(db)} (this hub knows up to {LATEST})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
