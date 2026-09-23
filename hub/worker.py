"""Job runner: takes queued scan / prep / detect jobs and runs them on the server.

    python -m hub.worker                       # all kinds, until stopped
    python -m hub.worker --kinds prep,detect --once

Run as many workers as the server has cores to spare; the queue hands each job to one
worker only. Each job runs the Emergence Review Kit against the survey's folder in
server storage:

    $HUB_STORAGE_ROOT/<tenant>/<survey>/cards/       camera cards, copied or uploaded
    $HUB_STORAGE_ROOT/<tenant>/<survey>/manifest.json   written by scan
    $HUB_STORAGE_ROOT/<tenant>/<survey>/review/      prep and detect output, audit.jsonl

The worker decides those paths from the job's tenant and survey; a job can't name any
other location. The kit's own audit trail records the worker as the actor, together
with the person who queued the job.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
import threading
import traceback
from pathlib import Path

from . import queue
from .db import Database
from .schema import migrate

log = logging.getLogger("hub.worker")
POLL_S = 5


class JobError(Exception):
    """A job failed in a way retrying won't fix (bad input); goes straight to dead."""


def storage_root() -> Path:
    root = os.environ.get("HUB_STORAGE_ROOT", "")
    if not root:
        raise JobError("HUB_STORAGE_ROOT is not set on the worker")
    return Path(root).resolve()


def survey_dir(job: dict) -> Path:
    safe = lambda s: "".join(ch for ch in s if ch.isalnum() or ch in "-_.")  # noqa: E731
    tenant, survey = safe(job["tenant_id"]), safe(job["survey_id"])
    if not tenant or not survey or survey in (".", ".."):
        raise JobError("invalid tenant or survey id")
    root = storage_root()
    d = (root / tenant / survey).resolve()
    if not d.is_relative_to(root):
        raise JobError("survey folder escapes storage root")
    return d


def _inside(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise JobError(f"{rel} is outside the survey folder")
    return p


# ------------------------------------------------------------------ runners
def run_scan(job: dict, say) -> dict:
    from emergence_kit import audit
    from emergence_kit.scan import scan, write_manifest
    d = survey_dir(job)
    cards = d / "cards"
    if not cards.is_dir():
        raise JobError(f"no cards folder for this survey ({cards.name}/ is missing)")
    manifest = scan([cards], progress=say)
    out = d / "manifest.json"
    write_manifest(manifest, out)
    audit.record(d / "review", "scan", {
        "roots": ["cards"], "files": manifest["summary"]["files"],
        "recordings": manifest["summary"]["recordings"], "manifest": "../manifest.json",
        "manifest_sha256": audit.sha256_file(out), "job": job["id"]})
    return {"recordings": [r["id"] for r in manifest["recordings"]],
            "failed": len(manifest.get("failed", []))}


def run_prep(job: dict, say) -> dict:
    from emergence_kit.prep import PrepOptions, prep
    d = survey_dir(job)
    p = job["params"]
    mpath = _inside(d, p.get("manifest", "manifest.json"))
    if not mpath.exists():
        raise JobError("manifest not found; run a scan job first")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    opts = PrepOptions(dest=d / "review", hw=p.get("hw", "none"), crf=int(p.get("crf", 23)),
                       only=list(p.get("only") or []), skip_review=bool(p.get("skip_review")))
    report = prep(manifest, opts, say=say)
    return {"recordings": [r["id"] for r in report["recordings"]]}


def run_detect(job: dict, say) -> dict:
    from emergence_kit.detect import DetectOptions, run
    d = survey_dir(job)
    p = job["params"]
    if not (d / "review" / "prep_report.json").exists():
        raise JobError("no prep output; run a prep job first")
    opts = DetectOptions(only=list(p.get("only") or []))
    for k in ("sensitivity", "min_pixels", "min_peak"):
        if k in p:
            setattr(opts, k, type(getattr(opts, k))(p[k]))
    out = run(d / "review", opts, say=say)
    return {r["id"]: r["motion"] for r in out["recordings"]}


RUNNERS = {"scan": run_scan, "prep": run_prep, "detect": run_detect}


# ------------------------------------------------------------------ loop
class Worker:
    def __init__(self, db: Database, kinds: list[str] | None = None, name: str | None = None,
                 lease_s: int = queue.LEASE_S):
        self.db = db
        self.kinds = kinds or sorted(RUNNERS)
        self.name = name or f"{platform.node()}:{os.getpid()}"
        self.lease_s = lease_s
        self.stopping = threading.Event()

    def _heartbeat(self, job_id: int, done: threading.Event, lost: threading.Event) -> None:
        while not done.wait(self.lease_s / 3):
            if not queue.heartbeat(self.db, job_id, self.name, self.lease_s):
                lost.set()
                return

    def run_one(self) -> dict | None:
        queue.reap(self.db)
        job = queue.claim(self.db, self.name, self.kinds, self.lease_s)
        if not job:
            return None
        log.info("job %s: %s for survey %s (attempt %s)", job["id"], job["kind"],
                 job["survey_id"], job["attempts"])
        done, lost = threading.Event(), threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(job["id"], done, lost), daemon=True)
        hb.start()
        # the kit's audit trail names the worker and whoever queued the job
        prev_user = os.environ.get("EMERGENCE_USER")
        os.environ["EMERGENCE_USER"] = (f"hub-worker {self.name} for "
                                        f"{job.get('created_by_name') or job['created_by']}")
        say = lambda m: log.info("job %s: %s", job["id"], m)  # noqa: E731
        try:
            result = RUNNERS[job["kind"]](job, say)
        except JobError as exc:
            status = queue.fail(self.db, job["id"], self.name, str(exc), retry=False)
            log.error("job %s failed (%s): %s", job["id"], status, exc)
            return {**job, "status": status, "error": str(exc)}
        except Exception as exc:                  # noqa: BLE001 - anything else may be transient
            err = f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc(limit=5)}"
            status = queue.fail(self.db, job["id"], self.name, err)
            log.error("job %s failed (%s): %s", job["id"], status, exc)
            return {**job, "status": status, "error": err}
        finally:
            done.set()
            if prev_user is None:
                os.environ.pop("EMERGENCE_USER", None)
            else:
                os.environ["EMERGENCE_USER"] = prev_user
        if lost.is_set():
            log.warning("job %s: lease lost while running; result discarded", job["id"])
            return {**job, "status": "lost"}
        if queue.complete(self.db, job["id"], self.name, result) and job["kind"] == "scan":
            from .service import insert_recordings
            with self.db.tx() as c:
                insert_recordings(c, job["tenant_id"], job["survey_id"], result["recordings"])
        self._anchor(job)
        log.info("job %s done", job["id"])
        return {**job, "status": "done", "result": result}

    def _anchor(self, job: dict) -> None:
        """Anchor the survey folder's audit chain after every job, so even people with
        access to server storage can't quietly rewrite it."""
        from emergence_kit.audit import read_entries
        from .service import anchor_rows
        try:
            entries = read_entries(survey_dir(job) / "review")
            with self.db.tx() as c:
                _, _, conflicts = anchor_rows(c, job["tenant_id"], job["survey_id"],
                                              entries, f"worker:{self.name}")
            if conflicts:
                log.error("job %s: audit trail of survey %s conflicts with its anchors: %s",
                          job["id"], job["survey_id"], conflicts[:3])
        except Exception:                         # noqa: BLE001 - never fail a finished job
            log.exception("job %s: anchoring failed", job["id"])

    def run_forever(self) -> None:
        log.info("worker %s taking %s", self.name, ",".join(self.kinds))
        while not self.stopping.is_set():
            try:
                job = self.run_one()
            except Exception:                     # noqa: BLE001 - database blip: back off
                log.exception("queue error; retrying in %ss", POLL_S * 6)
                self.stopping.wait(POLL_S * 6)
                continue
            if job is None:
                self.stopping.wait(POLL_S)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hub.worker")
    ap.add_argument("--kinds", help="comma-separated: scan,prep,detect (default: all)")
    ap.add_argument("--once", action="store_true", help="run at most one job, then exit")
    ap.add_argument("--name", help="worker name in the jobs table (default host:pid)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from brain import secrets as _secrets
    _secrets.ensure_loaded()
    db = Database.connect()
    migrate(db)
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    unknown = set(kinds or []) - set(RUNNERS)
    if unknown:
        print(f"unknown job kinds: {sorted(unknown)}", file=sys.stderr)
        return 2
    w = Worker(db, kinds, args.name)
    if args.once:
        job = w.run_one()
        print(json.dumps({"job": job and job["id"], "status": job and job["status"]}))
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        # finish the current job, then stop (systemd sends SIGTERM on stop/restart)
        signal.signal(sig, lambda *_: w.stopping.set())
    w.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
