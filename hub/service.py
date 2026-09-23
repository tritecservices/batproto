"""Hub business rules: surveys, review locks, separation of duties, jobs, audit anchors.

Kept free of HTTP so the rules can be tested directly and reused by the worker. Every
function takes the signed-in ``Principal`` and works inside that principal's tenant
only. Every change is written to the hub's tamper-evident audit log (brain.audit).

Recording lifecycle::

    pending --review lock--> (reviewing) --signoff--> signed_off --QA lock--> (in QA)
       ^                                                                       |
       +------------------------- rejected <--------- qa reject ---------------+
                                  approved <--------- qa approve --------------+

* Review needs Survey.Review; QA needs Survey.QA **and** must be a different person
  from the reviewer who signed off. Platform.Admin can break a stuck lock and reopen an
  approved recording, always with a reason, always audited.
* One lock per recording. Locks expire (default 30 minutes) unless renewed, so a
  closed laptop never blocks a recording for long.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import Any

from brain import audit as hub_audit
from brain.identity import Principal

from .db import Database, IntegrityError, parse_ts, ts, utcnow

LOCK_TTL_DEFAULT_S = 30 * 60
LOCK_TTL_MAX_S = 4 * 3600
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_KINDS = {
    # kind: allowed params. No free-form paths or locations: the worker decides where
    # files live, from the tenant and survey, so a caller can't point it anywhere else.
    "scan": set(),
    "prep": {"manifest", "only", "hw", "crf", "skip_review"},
    "detect": {"only", "sensitivity", "min_pixels", "min_peak"},
}
MAX_ANCHORS_PER_CALL = 5000


class HubError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _need(p: Principal, *roles: str) -> None:
    if not any(p.has(r) for r in roles):
        raise HubError(403, f"requires the {' or '.join(roles)} role")


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise HubError(422, f"invalid {what}: use letters, digits, '.', '_' or '-' "
                            "(max 128 characters)")
    return value


def _audit(p: Principal, action: str, details: dict) -> None:
    hub_audit.record(p.audit(), "hub." + action, details)


# ------------------------------------------------------------------ surveys
def create_survey(db: Database, p: Principal, survey_id: str, name: str) -> dict:
    _need(p, "Survey.Review")
    _check_id(survey_id, "survey id")
    name = (name or survey_id).strip()[:200]
    with db.tx() as c:
        c.execute("SELECT * FROM surveys WHERE tenant_id = ? AND id = ?", (p.tenant_id, survey_id))
        existing = c.one()
        if existing:
            return existing
        row = {"tenant_id": p.tenant_id, "id": survey_id, "name": name,
               "created_by": p.subject, "created_at": ts(utcnow())}
        c.execute("INSERT INTO surveys (tenant_id, id, name, created_by, created_at) "
                  "VALUES (?, ?, ?, ?, ?)", tuple(row.values()))
    _audit(p, "survey.create", {"survey": survey_id})
    return row


def list_surveys(db: Database, p: Principal) -> list[dict]:
    _need(p, "Survey.Review", "Survey.QA")
    with db.tx() as c:
        return c.execute("SELECT * FROM surveys WHERE tenant_id = ? ORDER BY created_at DESC",
                         (p.tenant_id,)).all()


def get_survey(db: Database, p: Principal, survey_id: str) -> dict:
    _need(p, "Survey.Review", "Survey.QA")
    with db.tx() as c:
        row = c.execute("SELECT * FROM surveys WHERE tenant_id = ? AND id = ?",
                        (p.tenant_id, survey_id)).one()
    if not row:
        # same answer whether it doesn't exist or belongs to another tenant
        raise HubError(404, "survey not found")
    return row


def register_recordings(db: Database, p: Principal, survey_id: str, ids: list[str]) -> list[str]:
    _need(p, "Survey.Review")
    get_survey(db, p, survey_id)
    for rid in ids:
        _check_id(rid, "recording id")
    with db.tx() as c:
        added = insert_recordings(c, p.tenant_id, survey_id, ids)
    if added:
        _audit(p, "recordings.register", {"survey": survey_id, "recordings": added})
    return added


def insert_recordings(c, tenant_id: str, survey_id: str, ids: list[str]) -> list[str]:
    """Add recordings not already known (also used by the worker after a scan job)."""
    added, now = [], ts(utcnow())
    for rid in ids:
        if not ID_RE.match(rid) or c.execute(
                "SELECT 1 AS x FROM recordings WHERE tenant_id = ? AND survey_id = ? AND id = ?",
                (tenant_id, survey_id, rid)).one():
            continue
        c.execute("INSERT INTO recordings (tenant_id, survey_id, id, status, updated_at) "
                  "VALUES (?, ?, ?, 'pending', ?)", (tenant_id, survey_id, rid, now))
        added.append(rid)
    return added


def list_recordings(db: Database, p: Principal, survey_id: str) -> list[dict]:
    get_survey(db, p, survey_id)
    now = ts(utcnow())
    with db.tx() as c:
        rows = c.execute(
            "SELECT r.*, l.kind AS lock_kind, l.holder_name AS lock_holder, "
            "l.expires_at AS lock_expires FROM recordings r LEFT JOIN locks l "
            "ON l.tenant_id = r.tenant_id AND l.survey_id = r.survey_id "
            "AND l.recording_id = r.id AND l.expires_at > ? "
            "WHERE r.tenant_id = ? AND r.survey_id = ? ORDER BY r.id",
            (now, p.tenant_id, survey_id)).all()
    return rows


def _recording(c, p: Principal, survey_id: str, rid: str) -> dict:
    row = c.execute("SELECT * FROM recordings WHERE tenant_id = ? AND survey_id = ? AND id = ?",
                    (p.tenant_id, survey_id, rid)).one()
    if not row:
        raise HubError(404, "recording not found")
    return row


# ------------------------------------------------------------------ locks
def acquire_lock(db: Database, p: Principal, survey_id: str, rid: str, kind: str,
                 ttl_s: int = LOCK_TTL_DEFAULT_S) -> dict:
    if kind not in ("review", "qa"):
        raise HubError(422, "lock kind must be review or qa")
    _need(p, "Survey.Review" if kind == "review" else "Survey.QA")
    ttl_s = max(60, min(int(ttl_s), LOCK_TTL_MAX_S))
    now = utcnow()
    with db.tx() as c:
        rec = _recording(c, p, survey_id, rid)
        if kind == "review" and rec["status"] == "approved":
            raise HubError(409, "recording is approved; an admin must reopen it first")
        if kind == "qa":
            if rec["status"] != "signed_off":
                raise HubError(409, f"recording is {rec['status']}, QA needs a signed-off review")
            if rec["reviewer"] == p.subject:
                raise HubError(403, "QA must be done by someone other than the reviewer")
        key = (p.tenant_id, survey_id, rid)
        c.execute("DELETE FROM locks WHERE tenant_id = ? AND survey_id = ? AND recording_id = ? "
                  "AND expires_at <= ?", (*key, ts(now)))
        held = c.execute("SELECT * FROM locks WHERE tenant_id = ? AND survey_id = ? "
                         "AND recording_id = ?", key).one()
        expires = ts(now + timedelta(seconds=ttl_s))
        if held:
            if held["holder"] == p.subject and held["kind"] == kind:
                c.execute("UPDATE locks SET expires_at = ? WHERE token = ?", (expires, held["token"]))
                return {**held, "expires_at": expires, "renewed": True}
            raise HubError(409, f"recording is locked for {held['kind']} by "
                                f"{held['holder_name'] or 'another user'} until "
                                f"{held['expires_at']}",
                           holder=held["holder_name"], kind=held["kind"],
                           expires_at=held["expires_at"])
        lock = {"tenant_id": p.tenant_id, "survey_id": survey_id, "recording_id": rid,
                "kind": kind, "holder": p.subject, "holder_name": p.name,
                "token": secrets.token_urlsafe(24), "acquired_at": ts(now), "expires_at": expires}
        try:
            c.execute("INSERT INTO locks (tenant_id, survey_id, recording_id, kind, holder, "
                      "holder_name, token, acquired_at, expires_at) VALUES "
                      "(?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(lock.values()))
        except IntegrityError:
            raise HubError(409, "someone else locked this recording a moment ago") from None
    _audit(p, "lock.acquire", {"survey": survey_id, "recording": rid, "kind": kind,
                               "expires_at": expires})
    return lock


def _held_lock(c, p: Principal, survey_id: str, rid: str, token: str, kind: str | None) -> dict:
    lock = c.execute("SELECT * FROM locks WHERE tenant_id = ? AND survey_id = ? "
                     "AND recording_id = ? AND token = ?", (p.tenant_id, survey_id, rid, token)).one()
    if not lock or lock["holder"] != p.subject:
        raise HubError(409, "you don't hold a lock on this recording (expired or taken over)")
    if parse_ts(lock["expires_at"]) <= utcnow():
        raise HubError(409, "your lock expired; lock the recording again")
    if kind and lock["kind"] != kind:
        raise HubError(409, f"this needs a {kind} lock, you hold a {lock['kind']} lock")
    return lock


def renew_lock(db: Database, p: Principal, survey_id: str, rid: str, token: str,
               ttl_s: int = LOCK_TTL_DEFAULT_S) -> dict:
    ttl_s = max(60, min(int(ttl_s), LOCK_TTL_MAX_S))
    with db.tx() as c:
        lock = _held_lock(c, p, survey_id, rid, token, None)
        expires = ts(utcnow() + timedelta(seconds=ttl_s))
        c.execute("UPDATE locks SET expires_at = ? WHERE token = ?", (expires, token))
    return {**lock, "expires_at": expires}


def release_lock(db: Database, p: Principal, survey_id: str, rid: str, token: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM locks WHERE tenant_id = ? AND survey_id = ? AND recording_id = ? "
                  "AND token = ? AND holder = ?", (p.tenant_id, survey_id, rid, token, p.subject))
        gone = c.rowcount
    if gone:
        _audit(p, "lock.release", {"survey": survey_id, "recording": rid})


def break_lock(db: Database, p: Principal, survey_id: str, rid: str, reason: str) -> dict | None:
    _need(p, "Platform.Admin")
    if not reason or len(reason.strip()) < 5:
        raise HubError(422, "give a reason (it goes in the audit log)")
    with db.tx() as c:
        lock = c.execute("SELECT * FROM locks WHERE tenant_id = ? AND survey_id = ? "
                         "AND recording_id = ?", (p.tenant_id, survey_id, rid)).one()
        c.execute("DELETE FROM locks WHERE tenant_id = ? AND survey_id = ? AND recording_id = ?",
                  (p.tenant_id, survey_id, rid))
    _audit(p, "lock.break", {"survey": survey_id, "recording": rid, "reason": reason.strip(),
                             "was_held_by": lock["holder_name"] if lock else None})
    return lock


# ------------------------------------------------------------------ sign-off and QA
def signoff(db: Database, p: Principal, survey_id: str, rid: str, token: str,
            log_sha256: str, rows: int | None = None) -> dict:
    _need(p, "Survey.Review")
    if not HASH_RE.match(log_sha256 or ""):
        raise HubError(422, "log_sha256 must be a SHA-256 hex digest")
    now = ts(utcnow())
    with db.tx() as c:
        _held_lock(c, p, survey_id, rid, token, "review")
        c.execute("UPDATE recordings SET status = 'signed_off', reviewer = ?, reviewer_name = ?, "
                  "log_sha256 = ?, qa_by = NULL, qa_by_name = NULL, qa_decision = NULL, "
                  "updated_at = ? WHERE tenant_id = ? AND survey_id = ? AND id = ?",
                  (p.subject, p.name, log_sha256, now, p.tenant_id, survey_id, rid))
        c.execute("DELETE FROM locks WHERE token = ?", (token,))
        rec = _recording(c, p, survey_id, rid)
    _audit(p, "review.signoff", {"survey": survey_id, "recording": rid,
                                 "log_sha256": log_sha256, "rows": rows})
    return rec


def qa_decision(db: Database, p: Principal, survey_id: str, rid: str, token: str,
                decision: str, log_sha256: str, note: str = "",
                comparison: dict | None = None) -> dict:
    _need(p, "Survey.QA")
    if decision not in ("approve", "reject"):
        raise HubError(422, "decision must be approve or reject")
    now = ts(utcnow())
    with db.tx() as c:
        _held_lock(c, p, survey_id, rid, token, "qa")
        rec = _recording(c, p, survey_id, rid)
        if rec["reviewer"] == p.subject:          # re-checked: roles can change mid-lock
            raise HubError(403, "QA must be done by someone other than the reviewer")
        if rec["status"] != "signed_off":
            raise HubError(409, f"recording is {rec['status']}, not awaiting QA")
        if log_sha256 != rec["log_sha256"]:
            raise HubError(409, "the review log changed after sign-off; the reviewer must "
                                "sign off again before QA")
        status = "approved" if decision == "approve" else "rejected"
        c.execute("UPDATE recordings SET status = ?, qa_by = ?, qa_by_name = ?, qa_decision = ?, "
                  "updated_at = ? WHERE tenant_id = ? AND survey_id = ? AND id = ?",
                  (status, p.subject, p.name, decision, now, p.tenant_id, survey_id, rid))
        c.execute("DELETE FROM locks WHERE token = ?", (token,))
        rec = _recording(c, p, survey_id, rid)
    _audit(p, "review.qa", {"survey": survey_id, "recording": rid, "decision": decision,
                            "note": note[:500], "reviewer": rec["reviewer_name"],
                            "agreement": (comparison or {}).get("agreement")})
    return rec


def reopen(db: Database, p: Principal, survey_id: str, rid: str, reason: str) -> dict:
    _need(p, "Platform.Admin")
    if not reason or len(reason.strip()) < 5:
        raise HubError(422, "give a reason (it goes in the audit log)")
    with db.tx() as c:
        rec = _recording(c, p, survey_id, rid)
        c.execute("UPDATE recordings SET status = 'pending', updated_at = ? WHERE tenant_id = ? "
                  "AND survey_id = ? AND id = ?", (ts(utcnow()), p.tenant_id, survey_id, rid))
    _audit(p, "recording.reopen", {"survey": survey_id, "recording": rid,
                                   "was": rec["status"], "reason": reason.strip()})
    return {**rec, "status": "pending"}


# ------------------------------------------------------------------ jobs
def enqueue(db: Database, p: Principal, survey_id: str, kind: str, params: dict,
            priority: int = 0) -> dict:
    _need(p, "Survey.Review")
    get_survey(db, p, survey_id)
    if kind not in JOB_KINDS:
        raise HubError(422, f"unknown job kind; use one of {sorted(JOB_KINDS)}")
    params = params or {}
    bad = set(params) - JOB_KINDS[kind]
    if bad:
        raise HubError(422, f"parameters not allowed for {kind}: {sorted(bad)}")
    if "manifest" in params:
        m = str(params["manifest"])
        if m.startswith(("/", "\\")) or ".." in m.replace("\\", "/").split("/") or ":" in m:
            raise HubError(422, "manifest must be a path inside the survey's storage folder")
    now = ts(utcnow())
    with db.tx() as c:
        c.execute("INSERT INTO jobs (tenant_id, survey_id, kind, params, status, priority, "
                  "run_after, created_by, created_by_name, created_at) "
                  "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?) RETURNING *",
                  (p.tenant_id, survey_id, kind, json.dumps(params, sort_keys=True),
                   max(-10, min(int(priority), 10)), now, p.subject, p.name, now))
        job = c.one()
    _audit(p, "job.enqueue", {"survey": survey_id, "job": job["id"], "kind": kind})
    return job


def list_jobs(db: Database, p: Principal, survey_id: str | None = None,
              limit: int = 100) -> list[dict]:
    _need(p, "Survey.Review", "Survey.QA")
    sql = "SELECT * FROM jobs WHERE tenant_id = ?"
    args: list = [p.tenant_id]
    if survey_id:
        sql += " AND survey_id = ?"
        args.append(survey_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db.tx() as c:
        return c.execute(sql, args).all()


def get_job(db: Database, p: Principal, job_id: int) -> dict:
    _need(p, "Survey.Review", "Survey.QA")
    with db.tx() as c:
        job = c.execute("SELECT * FROM jobs WHERE tenant_id = ? AND id = ?",
                        (p.tenant_id, int(job_id))).one()
    if not job:
        raise HubError(404, "job not found")
    return job


def cancel_job(db: Database, p: Principal, job_id: int) -> dict:
    job = get_job(db, p, job_id)
    if job["created_by"] != p.subject and not p.has("Platform.Admin"):
        raise HubError(403, "only the person who queued a job (or an admin) can cancel it")
    with db.tx() as c:
        c.execute("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE tenant_id = ? "
                  "AND id = ? AND status = 'queued'", (ts(utcnow()), p.tenant_id, int(job_id)))
        changed = c.rowcount
    if not changed:
        raise HubError(409, f"job is {job['status']}; only queued jobs can be cancelled")
    _audit(p, "job.cancel", {"job": int(job_id)})
    return {**job, "status": "cancelled"}


# ------------------------------------------------------------------ audit anchors
def anchor(db: Database, p: Principal, survey_id: str, entries: list[dict]) -> dict:
    """Store audit-chain heads from a survey folder. Entries already anchored must match
    exactly; a new entry must follow on from the one before it (its ``prev`` equals the
    anchored hash of seq - 1). Anything else means the folder's audit trail was
    rewritten, and is refused and logged."""
    _need(p, "Survey.Review", "Survey.QA")
    get_survey(db, p, survey_id)
    if len(entries) > MAX_ANCHORS_PER_CALL:
        raise HubError(413, f"send at most {MAX_ANCHORS_PER_CALL} entries per call")
    for e in entries:
        if int(e.get("seq", 0)) < 1 or not HASH_RE.match(str(e.get("hash", ""))):
            raise HubError(422, f"entry {e.get('seq')}: needs seq >= 1 and a SHA-256 hash")
    with db.tx() as c:
        accepted, known, conflicts = anchor_rows(c, p.tenant_id, survey_id, entries, p.subject)
    if accepted:
        _audit(p, "anchor", {"survey": survey_id, "from": accepted[0], "to": accepted[-1],
                             "count": len(accepted)})
    if conflicts:
        _audit(p, "anchor.conflict", {"survey": survey_id, "conflicts": conflicts[:20]})
    return {"accepted": len(accepted), "already_anchored": known, "conflicts": conflicts}


def anchor_rows(c, tenant_id: str, survey_id: str, entries: list[dict],
                by: str) -> tuple[list[int], int, list[dict]]:
    """Core of anchor(); also used by the worker for folders in server storage."""
    accepted, known, conflicts = [], 0, []
    now = ts(utcnow())
    have = {r["seq"]: r["head_hash"] for r in c.execute(
        "SELECT seq, head_hash FROM audit_anchors WHERE tenant_id = ? AND survey_id = ?",
        (tenant_id, survey_id)).all()}
    for e in sorted(entries, key=lambda e: int(e.get("seq", 0))):
        seq, h, prev = int(e.get("seq", 0)), e.get("hash", ""), e.get("prev")
        if seq in have:
            if have[seq] == h:
                known += 1
            else:
                conflicts.append({"seq": seq, "problem": "differs from the anchored entry"})
            continue
        if seq - 1 in have and prev is not None and prev != have[seq - 1]:
            conflicts.append({"seq": seq, "problem": f"does not follow the anchored entry {seq - 1}"})
            continue
        c.execute("INSERT INTO audit_anchors (tenant_id, survey_id, seq, head_hash, action, "
                  "anchored_by, anchored_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (tenant_id, survey_id, seq, h, str(e.get("action") or "")[:64], by, now))
        have[seq] = h
        accepted.append(seq)
    return accepted, known, conflicts


def verify_against_anchors(db: Database, p: Principal, survey_id: str,
                           entries: list[dict]) -> dict:
    """Compare a folder's audit trail with what the hub anchored. Detects what a folder
    can't detect on its own: entries removed from the end, or the whole chain rebuilt."""
    _need(p, "Survey.Review", "Survey.QA")
    get_survey(db, p, survey_id)
    local = {int(e["seq"]): e.get("hash") for e in entries}
    with db.tx() as c:
        anchored = c.execute("SELECT seq, head_hash, anchored_at FROM audit_anchors "
                             "WHERE tenant_id = ? AND survey_id = ? ORDER BY seq",
                             (p.tenant_id, survey_id)).all()
    problems = []
    for a in anchored:
        seq = a["seq"]
        if seq not in local:
            problems.append(f"entry {seq} was anchored on {a['anchored_at'][:19]}Z but is "
                            "missing from the folder - the audit trail was cut short")
        elif local[seq] != a["head_hash"]:
            problems.append(f"entry {seq} differs from the copy anchored on "
                            f"{a['anchored_at'][:19]}Z - the audit trail was rewritten")
        if len(problems) >= 20:
            break
    top = anchored[-1]["seq"] if anchored else 0
    return {"ok": not problems, "problems": problems, "anchored_up_to": top,
            "local_up_to": max(local) if local else 0,
            "not_yet_anchored": len([s for s in local if s > top])}
