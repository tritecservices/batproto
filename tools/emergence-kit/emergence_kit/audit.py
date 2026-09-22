"""Tamper-evident audit trail: who did what, when, to which files.

One append-only file per survey folder, <folder>/audit.jsonl. Every entry records:

  seq, time (UTC), actor (user, how identified, machine), action, tool version,
  details (inputs and outputs with SHA-256), prev (hash of the previous entry), hash

`hash` is SHA-256 over the entry's canonical JSON *including* `prev`, so the entries
form a chain: editing, deleting, reordering or inserting any line breaks every hash
after it, and `emergence-kit audit verify` reports the first broken entry.

<folder>/audit.head holds the latest seq and hash. A log cut short (the last entries
deleted together with a rolled-back head file) cannot be detected from the folder
alone; that needs the head anchored somewhere the user can't edit. The central
survey service (roadmap phase 6) does that. Until then, `audit verify` prints the head
hash so it can be recorded in a ticket or change record.

The trail records identities and file fingerprints, never survey content or
locations.
"""
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import os
import platform
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

AUDIT_FILE = "audit.jsonl"
HEAD_FILE = "audit.head"
GENESIS = "0" * 64
_lock = threading.Lock()


# ---------------------------------------------------------------- identity
def current_actor() -> dict:
    """The person running the tool, as reliably as the machine can say.

    On an Entra- or AD-joined Windows laptop `whoami /upn` gives the directory
    identity (name@company.com), the same one Entra sign-in uses. EMERGENCE_USER
    overrides it for service accounts and CI; the override itself is recorded.
    """
    override = os.environ.get("EMERGENCE_USER")
    if override:
        return {"user": override, "via": "EMERGENCE_USER", "host": platform.node()}
    if os.name == "nt":
        for args, via in ((["whoami", "/upn"], "windows-upn"), (["whoami"], "windows-account")):
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    return {"user": r.stdout.strip(), "via": via, "host": platform.node()}
            except OSError:
                pass
    return {"user": getpass.getuser(), "via": "os-account", "host": platform.node()}


# ---------------------------------------------------------------- hashing
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _entry_hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- write / read
def read_entries(folder: Path) -> list[dict]:
    path = Path(folder) / AUDIT_FILE
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_unreadable_line": n})
    return out


def record(folder: Path, action: str, details: dict | None = None,
           actor: dict | None = None) -> dict:
    """Append one entry. Refuses to append to a chain that no longer verifies, so a
    tampered log can't be quietly 'continued' as if nothing happened."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with _lock:
        entries = read_entries(folder)
        problems = verify_entries(entries, folder)
        if problems:
            raise RuntimeError("audit trail failed verification, not appending: " + problems[0])
        prev = entries[-1]["hash"] if entries else GENESIS
        entry = {
            "seq": len(entries) + 1,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": actor or current_actor(),
            "action": action,
            "tool": f"emergence-kit {__version__}",
            "details": details or {},
            "prev": prev,
        }
        entry["hash"] = _entry_hash(entry)
        with (folder / AUDIT_FILE).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        (folder / HEAD_FILE).write_text(f"{entry['seq']} {entry['hash']}\n", encoding="utf-8")
        return entry


def verify_entries(entries: list[dict], folder: Path | None = None) -> list[str]:
    problems: list[str] = []
    prev = GENESIS
    for i, e in enumerate(entries, 1):
        if "_unreadable_line" in e:
            problems.append(f"line {e['_unreadable_line']} is not valid JSON")
            return problems
        if e.get("seq") != i:
            problems.append(f"entry {i}: sequence is {e.get('seq')} - entries missing or reordered")
            return problems
        if e.get("prev") != prev:
            problems.append(f"entry {i}: does not follow entry {i - 1} - chain broken")
            return problems
        if _entry_hash(e) != e.get("hash"):
            problems.append(f"entry {i} ({e.get('action')}): contents changed after writing")
            return problems
        prev = e["hash"]
    if folder is not None:
        head = Path(folder) / HEAD_FILE
        if head.exists():
            parts = head.read_text(encoding="utf-8").split()
            if len(parts) == 2 and entries and (int(parts[0]) != len(entries) or parts[1] != prev):
                problems.append(f"audit.head says entry {parts[0]} is the latest, the log ends at "
                                f"{len(entries)} - entries removed from the end?")
        elif entries:
            problems.append("audit.head is missing")
    return problems


def verify(folder: Path) -> tuple[list[str], dict | None]:
    entries = read_entries(folder)
    return verify_entries(entries, folder), (entries[-1] if entries else None)


def export_csv(folder: Path, out: Path) -> int:
    entries = read_entries(folder)
    with Path(out).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["seq", "time_utc", "user", "identified_via", "host", "action", "tool",
                    "details", "hash"])
        for e in entries:
            a = e.get("actor", {})
            w.writerow([e.get("seq"), e.get("time"), a.get("user"), a.get("via"), a.get("host"),
                        e.get("action"), e.get("tool"),
                        json.dumps(e.get("details", {}), sort_keys=True), e.get("hash")])
    return len(entries)


# ---------------------------------------------------------------- queries
def latest(folder: Path, action: str, recording: str) -> dict | None:
    for e in reversed(read_entries(folder)):
        if e.get("action") == action and e.get("details", {}).get("recording") == recording:
            return e
    return None
