"""Sign-off and QA as single actions, shared by the command line and Survey Studio.

When the folder is linked to a survey hub, the hub decides first (one reviewer per
recording, QA by a different person, across every laptop); the folder's own audit trail
is written second and then anchored at the hub.
"""
from __future__ import annotations

from pathlib import Path

from . import audit
from . import hub_client as H
from .qa import QAError, _read, log_paths, qa, signoff

ActionError = (QAError, RuntimeError, H.HubError)


def hub_lock_token(client, sid: str, rid: str, kind: str) -> str:
    """The lock token this user holds for the recording, taking the lock if needed."""
    held = H.load_lock(client.url, sid, rid)
    if held and held["kind"] == kind:
        return held["token"]
    lock = client.lock(sid, rid, kind)
    H.save_lock(client.url, sid, rid, lock)
    return lock["token"]


def do_signoff(dest: Path, rid: str, note: str = "") -> dict:
    dest = Path(dest)
    found = H.client_for(dest)
    if found:
        client, sid = found
        log, _ = log_paths(dest, rid)
        if not log.exists():
            raise QAError(f"no review log for {rid} at {log}")
        token = hub_lock_token(client, sid, rid, "review")
        client.signoff(sid, rid, token, audit.sha256_file(log), len(_read(log)))
        H.forget_lock(client.url, sid, rid)
    entry = signoff(dest, rid, note)
    H.sync_anchors(dest, say=lambda m: None)
    return entry


def do_qa(dest: Path, rid: str, decision: str, note: str = "",
          qa_log: Path | None = None) -> dict:
    dest = Path(dest)
    found = H.client_for(dest)
    if found:
        # checks that would fail locally go first, so the hub never records a QA decision
        # the folder then refuses
        client, sid = found
        log, default_qa = log_paths(dest, rid)
        qlog = Path(qa_log) if qa_log else default_qa
        if not audit.latest(dest, "review.signoff", rid):
            raise QAError(f"{rid} has not been signed off by its reviewer yet")
        if not qlog.exists():
            raise QAError(f"no QA log at {qlog} - review the recording independently first")
        token = hub_lock_token(client, sid, rid, "qa")
        client.qa(sid, rid, token, decision, audit.sha256_file(log), note)
        H.forget_lock(client.url, sid, rid)
    entry = qa(dest, rid, decision, note, qa_log)
    H.sync_anchors(dest, say=lambda m: None)
    return entry
