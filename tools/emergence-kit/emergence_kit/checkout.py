"""Check a survey out from a network share to this laptop, and back in again.

Why: reviewing video straight off a network share is slow, and two people saving into
the same survey folder at once is how logs and reports get corrupted. Checking out
gives one person a fast local copy and marks the survey on the share as theirs, so
colleagues see it read-only. Checking in copies the work back safely and releases it.

    share/Barn A/.survey-checkout.json     who has it, on which laptop, since when
    laptop/Barn A/.survey-origin.json      where it came from, and the fingerprint of
                                           every work file at check-out

Rules:
* One check-out at a time. The marker file is created atomically, so two people
  clicking at the same moment can't both get it (this works on SMB/Windows shares).
* Camera originals stay on the share; review copies and all work files come down.
* Check-in copies back only work files (logs, audit trail, reports, detections), each
  via a temporary file and a rename, then re-reads them to confirm. It refuses if any
  work file on the share changed since check-out: nothing is overwritten silently.
* Both folders' audit trails record check-out and check-in. A stale check-out (laptop
  lost) can be released by anyone, with a reason, and that is recorded too.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import audit

LOCK = ".survey-checkout.json"
ORIGIN = ".survey-origin.json"
WORK_SUFFIXES = {".csv", ".json", ".jsonl", ".head", ".html", ".txt", ".sha256"}
SKIP_DIRS = {"originals"}
SKIP_NAMES = {LOCK, ORIGIN}


class CheckoutError(Exception):
    pass


def _me() -> dict:
    a = audit.current_actor()
    return {"user": a["user"], "host": platform.node()}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files(root: Path, work_only: bool) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or rel.parts[0] in SKIP_DIRS or p.name in SKIP_NAMES:
            continue
        if p.name.endswith((".part", ".tmp")) or p.name.startswith(".~"):
            continue
        if work_only and p.suffix.lower() not in WORK_SUFFIXES:
            continue
        out.append(p)
    return out


def _copy(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    with src.open("rb") as fi, part.open("wb") as fo:
        while block := fi.read(8 << 20):
            h.update(block)
            fo.write(block)
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, part)
    os.replace(part, dst)
    return h.hexdigest()


# ------------------------------------------------------------------ status
def lock_info(folder: Path) -> dict | None:
    f = Path(folder) / LOCK
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"user": "unknown", "host": "unknown", "since": None, "corrupt": True}


def origin_info(folder: Path) -> dict | None:
    f = Path(folder) / ORIGIN
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(folder: Path) -> dict:
    """What a survey folder is, from the check-out point of view:
    local-copy (a checked-out copy on this laptop), checked-out (a share folder someone
    has checked out; read-only here), or free."""
    folder = Path(folder)
    origin = origin_info(folder)
    if origin and not origin.get("checked_in"):
        return {"state": "local-copy", "share": origin["share"], "since": origin["since"],
                "by": origin["by"]}
    lock = lock_info(folder)
    if lock:
        return {"state": "checked-out", **lock}
    return {"state": "free"}


def writable(folder: Path) -> str | None:
    """None if the folder may be changed here; otherwise the reason it's read-only."""
    st = status(folder)
    if st["state"] == "checked-out":
        who = st.get("user", "someone")
        where = f" on {st['host']}" if st.get("host") else ""
        since = f" since {st['since'][:16].replace('T', ' ')}" if st.get("since") else ""
        return (f"Checked out by {who}{where}{since}. It's read-only here until it's "
                "checked back in.")
    return None


# ------------------------------------------------------------------ check out
def checkout(share: Path, local_root: Path, say: Callable[[str], None] = lambda m: None) -> Path:
    share = Path(share).resolve()
    if not (share / "prep_report.json").exists():
        raise CheckoutError("that folder isn't a prepared survey")
    if origin_info(share) and not origin_info(share).get("checked_in"):
        raise CheckoutError("this is already a checked-out copy; open the original on the share")
    me = _me()
    lock = {**me, "since": datetime.now().isoformat(timespec="seconds"), "to": None}
    try:
        fd = os.open(share / LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise CheckoutError(writable(share) or "already checked out") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(lock, fh)
    dest = Path(local_root) / share.name
    n = 2
    while dest.exists():
        dest = Path(local_root) / f"{share.name} ({n})"
        n += 1
    try:
        audit.record(share, "checkout", {"to_host": me["host"], "to": str(dest)})
        files = _files(share, work_only=False)
        total = sum(p.stat().st_size for p in files) or 1
        done = 0
        base = {}
        for p in files:
            rel = p.relative_to(share).as_posix()
            digest = _copy(p, dest / rel)
            if p.suffix.lower() in WORK_SUFFIXES:
                base[rel] = digest
            done += p.stat().st_size
            say(f"copying {rel}  {100 * done / total:.0f}%")
        (dest / ORIGIN).write_text(json.dumps({
            "share": str(share), "since": lock["since"], "by": me, "base": base,
            "checked_in": False}, indent=2), encoding="utf-8")
        lock["to"] = str(dest)
        (share / LOCK).write_text(json.dumps(lock), encoding="utf-8")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        (share / LOCK).unlink(missing_ok=True)
        raise
    return dest


# ------------------------------------------------------------------ check in
def checkin(local: Path, say: Callable[[str], None] = lambda m: None) -> dict:
    local = Path(local).resolve()
    origin = origin_info(local)
    if not origin or origin.get("checked_in"):
        raise CheckoutError("this folder isn't a checked-out copy")
    share = Path(origin["share"])
    if not share.exists():
        raise CheckoutError(f"can't reach the share folder {share} (network connected?)")
    lock = lock_info(share)
    me = _me()
    if not lock or (lock.get("user"), lock.get("host")) != (origin["by"]["user"], origin["by"]["host"]):
        raise CheckoutError("the check-out was released on the share (someone broke it). "
                            "Your work is safe in this folder; ask the service desk to merge it.")
    # nothing on the share may have changed since check-out
    changed = []
    for rel, digest in origin["base"].items():
        p = share / rel
        if not p.exists() or _sha(p) != digest:
            changed.append(rel)
    if changed:
        raise CheckoutError("files on the share changed while it was checked out: "
                            + ", ".join(changed[:5]) + ". Nothing was copied back.")
    audit.record(local, "checkin", {"to": str(share), "by_host": me["host"]})
    sent = []
    for p in _files(local, work_only=True):
        rel = p.relative_to(local).as_posix()
        target = share / rel
        if target.exists() and _sha(target) == _sha(p):
            continue
        digest = _copy(p, target)
        if _sha(target) != digest:
            raise CheckoutError(f"{rel} didn't copy back correctly; check-out kept")
        sent.append(rel)
        say(f"copied back {rel}")
    problems, _ = audit.verify(share)
    if problems:
        raise CheckoutError("the share's audit trail doesn't verify after check-in: "
                            + problems[0] + ". Check-out kept; contact the service desk.")
    (share / LOCK).unlink(missing_ok=True)
    origin["checked_in"] = datetime.now().isoformat(timespec="seconds")
    (local / ORIGIN).write_text(json.dumps(origin, indent=2), encoding="utf-8")
    return {"share": str(share), "files": sent}


def release(share: Path, reason: str) -> dict:
    """Release a stale check-out (the laptop was lost, or someone left). Recorded."""
    share = Path(share)
    lock = lock_info(share)
    if not lock:
        raise CheckoutError("it isn't checked out")
    if not reason or len(reason.strip()) < 5:
        raise CheckoutError("give a reason (it goes in the audit trail)")
    audit.record(share, "checkout.release", {"was": lock, "reason": reason.strip()})
    (share / LOCK).unlink(missing_ok=True)
    return lock
