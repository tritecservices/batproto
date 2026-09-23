"""Job queue operations used by workers (not by people).

At-least-once delivery with leases:

* ``claim``   takes the highest-priority ready job and leases it to one worker. On
              PostgreSQL, ``FOR UPDATE SKIP LOCKED`` lets many workers claim at once
              without ever getting the same job; SQLite serialises writers instead.
* ``heartbeat`` extends the lease while the job runs.
* ``reap``    returns jobs whose lease ran out (worker crashed, server rebooted) to the
              queue, or marks them ``dead`` once they've used all their attempts.
* ``fail``    retries with exponential back-off (30 s, 60 s, 120 s ...), then ``dead``.

Dead jobs stay in the table for the service desk: they're incidents, not noise.
Jobs must therefore be safe to run twice. prep and detect are: prep skips files it
already copied with matching checksums, and detect overwrites its own outputs.
"""
from __future__ import annotations

import json
from datetime import timedelta

from .db import Database, ts, utcnow

LEASE_S = 300
BACKOFF_BASE_S = 30


def claim(db: Database, worker: str, kinds: list[str] | None = None,
          lease_s: int = LEASE_S) -> dict | None:
    now = utcnow()
    sql = ("SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ?")
    args: list = [ts(now)]
    if kinds:
        sql += " AND kind IN (" + ", ".join("?" for _ in kinds) + ")"
        args += list(kinds)
    sql += " ORDER BY priority DESC, id LIMIT 1"
    if db.dialect == "postgresql":
        sql += " FOR UPDATE SKIP LOCKED"
    with db.tx() as c:
        row = c.execute(sql, args).one()
        if not row:
            return None
        c.execute("UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?, "
                  "attempts = attempts + 1, started_at = ?, error = NULL WHERE id = ?",
                  (worker, ts(now + timedelta(seconds=lease_s)), ts(now), row["id"]))
        job = c.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).one()
    job["params"] = json.loads(job["params"] or "{}")
    return job


def heartbeat(db: Database, job_id: int, worker: str, lease_s: int = LEASE_S) -> bool:
    """False if the job is no longer ours (reaped and re-claimed): stop working on it."""
    with db.tx() as c:
        c.execute("UPDATE jobs SET locked_until = ? WHERE id = ? AND locked_by = ? "
                  "AND status = 'running'",
                  (ts(utcnow() + timedelta(seconds=lease_s)), job_id, worker))
        return c.rowcount == 1


def complete(db: Database, job_id: int, worker: str, result: dict) -> bool:
    with db.tx() as c:
        c.execute("UPDATE jobs SET status = 'done', finished_at = ?, result = ?, "
                  "locked_until = NULL WHERE id = ? AND locked_by = ? AND status = 'running'",
                  (ts(utcnow()), json.dumps(result, default=str)[:100_000], job_id, worker))
        return c.rowcount == 1


def fail(db: Database, job_id: int, worker: str, error: str, retry: bool = True) -> str:
    """Returns the job's new status: queued (will retry) or dead."""
    now = utcnow()
    with db.tx() as c:
        job = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ? AND locked_by = ? "
                        "AND status = 'running'", (job_id, worker)).one()
        if not job:
            return "not-ours"
        if retry and job["attempts"] < job["max_attempts"]:
            delay = BACKOFF_BASE_S * 2 ** (job["attempts"] - 1)
            c.execute("UPDATE jobs SET status = 'queued', run_after = ?, locked_by = NULL, "
                      "locked_until = NULL, error = ? WHERE id = ?",
                      (ts(now + timedelta(seconds=delay)), error[:4000], job_id))
            return "queued"
        c.execute("UPDATE jobs SET status = 'dead', finished_at = ?, locked_until = NULL, "
                  "error = ? WHERE id = ?", (ts(now), error[:4000], job_id))
        return "dead"


def reap(db: Database) -> dict:
    """Recover jobs whose worker stopped heartbeating."""
    now = ts(utcnow())
    with db.tx() as c:
        c.execute("UPDATE jobs SET status = 'dead', finished_at = ?, locked_until = NULL, "
                  "error = 'worker stopped responding; no attempts left' "
                  "WHERE status = 'running' AND locked_until < ? AND attempts >= max_attempts",
                  (now, now))
        dead = c.rowcount
        c.execute("UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL, "
                  "error = 'worker stopped responding; retrying' "
                  "WHERE status = 'running' AND locked_until < ?", (now,))
        requeued = c.rowcount
    return {"requeued": requeued, "dead": dead}


def stats(db: Database) -> dict:
    """Queue depth by status, and the oldest queued job's age: capacity management."""
    with db.tx() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").all()
        oldest = c.execute("SELECT MIN(created_at) AS t FROM jobs WHERE status = 'queued'").one()
    return {"by_status": {r["status"]: r["n"] for r in rows},
            "oldest_queued": oldest["t"] if oldest else None}
