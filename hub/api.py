"""HTTP API for the survey hub.

    uvicorn hub.api:app --host 127.0.0.1 --port 8100

Environment:
  HUB_DATABASE_URL        postgresql://... (production) or sqlite:///path (trials)
  ENTRA_AUDIENCE          the API's Application ID URI (api://<client id>)
  ENTRA_ALLOWED_TENANTS   tenant ids allowed in
  BRAIN_AUDIT_DIR         where the hub's own audit log goes (default data/audit)
  AZURE_KEYVAULT_URL      optional: load secrets from Key Vault at start-up

Sign-in is Microsoft Entra ID only. A shared key identifies no person, and every hub
action (lock, sign-off, QA, queue a job) has to be attributable to one.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from brain import identity
from brain import secrets as _secrets

from . import __version__, queue, service
from .db import Database
from .schema import LATEST, current, migrate

_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database.connect()
    return _db


@asynccontextmanager
async def lifespan(_app):
    _secrets.ensure_loaded()
    migrate(get_db())
    yield


app = FastAPI(title="Ecomsp survey hub", version=__version__, lifespan=lifespan,
              description="Shared review, locking, QA, jobs and audit anchoring for the "
                          "Emergence Review Kit.")


@app.exception_handler(service.HubError)
async def _hub_error(_req: Request, exc: service.HubError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, **exc.extra})


def principal(authorization: str | None = Header(default=None)) -> identity.Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "sign in with Microsoft Entra ID (Authorization: Bearer ...)",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return identity.verify_entra(authorization[7:].strip(), identity.EntraConfig.from_env())
    except identity.AuthError as exc:
        raise HTTPException(exc.status, exc.message) from None


P = Depends(principal)
DB = Depends(get_db)


# ------------------------------------------------------------------ models
class SurveyIn(BaseModel):
    id: str = Field(..., max_length=128)
    name: str = Field("", max_length=200)


class RecordingsIn(BaseModel):
    ids: list[str] = Field(..., max_length=500)


class LockIn(BaseModel):
    kind: Literal["review", "qa"]
    ttl_s: int = Field(service.LOCK_TTL_DEFAULT_S, ge=60, le=service.LOCK_TTL_MAX_S)


class TokenIn(BaseModel):
    token: str = Field(..., max_length=64)
    ttl_s: int = Field(service.LOCK_TTL_DEFAULT_S, ge=60, le=service.LOCK_TTL_MAX_S)


class ReasonIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class SignoffIn(BaseModel):
    token: str = Field(..., max_length=64)
    log_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    rows: int | None = Field(None, ge=0)


class QaIn(BaseModel):
    token: str = Field(..., max_length=64)
    decision: Literal["approve", "reject"]
    log_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    note: str = Field("", max_length=500)
    comparison: dict | None = None


class JobIn(BaseModel):
    kind: Literal["scan", "prep", "detect"]
    params: dict = Field(default_factory=dict)
    priority: int = Field(0, ge=-10, le=10)


class AnchorEntry(BaseModel):
    seq: int = Field(..., ge=1)
    hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    prev: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    action: str | None = Field(None, max_length=64)


class AnchorsIn(BaseModel):
    entries: list[AnchorEntry] = Field(..., max_length=service.MAX_ANCHORS_PER_CALL)


def _dump(entries: list[AnchorEntry]) -> list[dict]:
    return [e.model_dump() for e in entries]


# ------------------------------------------------------------------ health
@app.get("/health", tags=["service"])
def health(db: Database = DB):
    """For monitoring (ITIL monitoring and event management): no sign-in, no data."""
    ok = db.ping()
    body = {"status": "ok" if ok else "degraded", "version": __version__,
            "schema": current(db) if ok else None, "schema_expected": LATEST}
    if ok:
        body["jobs"] = queue.stats(db)
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/whoami", tags=["service"])
def whoami(p: identity.Principal = P):
    return p.audit()


# ------------------------------------------------------------------ surveys
@app.post("/surveys", status_code=201, tags=["surveys"])
def create_survey(body: SurveyIn, p=P, db=DB):
    return service.create_survey(db, p, body.id, body.name)


@app.get("/surveys", tags=["surveys"])
def list_surveys(p=P, db=DB):
    return service.list_surveys(db, p)


@app.get("/surveys/{sid}", tags=["surveys"])
def get_survey(sid: str, p=P, db=DB):
    return service.get_survey(db, p, sid)


@app.post("/surveys/{sid}/recordings", tags=["recordings"])
def register(sid: str, body: RecordingsIn, p=P, db=DB):
    return {"added": service.register_recordings(db, p, sid, body.ids)}


@app.get("/surveys/{sid}/recordings", tags=["recordings"])
def recordings(sid: str, p=P, db=DB):
    return service.list_recordings(db, p, sid)


# ------------------------------------------------------------------ locks, sign-off, QA
@app.post("/surveys/{sid}/recordings/{rid}/lock", tags=["review"])
def lock(sid: str, rid: str, body: LockIn, p=P, db=DB):
    return service.acquire_lock(db, p, sid, rid, body.kind, body.ttl_s)


@app.post("/surveys/{sid}/recordings/{rid}/lock/renew", tags=["review"])
def renew(sid: str, rid: str, body: TokenIn, p=P, db=DB):
    return service.renew_lock(db, p, sid, rid, body.token, body.ttl_s)


@app.post("/surveys/{sid}/recordings/{rid}/lock/release", status_code=204, tags=["review"])
def release(sid: str, rid: str, body: TokenIn, p=P, db=DB):
    service.release_lock(db, p, sid, rid, body.token)


@app.post("/surveys/{sid}/recordings/{rid}/lock/break", tags=["admin"])
def break_lock(sid: str, rid: str, body: ReasonIn, p=P, db=DB):
    return {"broken": service.break_lock(db, p, sid, rid, body.reason)}


@app.post("/surveys/{sid}/recordings/{rid}/signoff", tags=["review"])
def signoff(sid: str, rid: str, body: SignoffIn, p=P, db=DB):
    return service.signoff(db, p, sid, rid, body.token, body.log_sha256, body.rows)


@app.post("/surveys/{sid}/recordings/{rid}/qa", tags=["review"])
def qa(sid: str, rid: str, body: QaIn, p=P, db=DB):
    return service.qa_decision(db, p, sid, rid, body.token, body.decision, body.log_sha256,
                               body.note, body.comparison)


@app.post("/surveys/{sid}/recordings/{rid}/reopen", tags=["admin"])
def reopen(sid: str, rid: str, body: ReasonIn, p=P, db=DB):
    return service.reopen(db, p, sid, rid, body.reason)


# ------------------------------------------------------------------ jobs
@app.post("/surveys/{sid}/jobs", status_code=201, tags=["jobs"])
def enqueue(sid: str, body: JobIn, p=P, db=DB):
    return service.enqueue(db, p, sid, body.kind, body.params, body.priority)


@app.get("/surveys/{sid}/jobs", tags=["jobs"])
def survey_jobs(sid: str, p=P, db=DB):
    return service.list_jobs(db, p, sid)


@app.get("/jobs/{job_id}", tags=["jobs"])
def job(job_id: int, p=P, db=DB):
    return service.get_job(db, p, job_id)


@app.post("/jobs/{job_id}/cancel", tags=["jobs"])
def cancel(job_id: int, p=P, db=DB):
    return service.cancel_job(db, p, job_id)


# ------------------------------------------------------------------ audit anchors
@app.post("/surveys/{sid}/anchors", tags=["audit"])
def anchor(sid: str, body: AnchorsIn, p=P, db=DB):
    return service.anchor(db, p, sid, _dump(body.entries))


@app.post("/surveys/{sid}/anchors/verify", tags=["audit"])
def verify(sid: str, body: AnchorsIn, p=P, db=DB):
    return service.verify_against_anchors(db, p, sid, _dump(body.entries))
