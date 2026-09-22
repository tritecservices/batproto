"""Access audit for the brain API: who read what, when - tamper-evident.

One hash-chained JSONL file per tenant: <BRAIN_AUDIT_DIR>/<tenant>.jsonl (default
data/audit/), plus <tenant>.head with the latest seq and hash. Same scheme as the
Emergence Review Kit's audit trail: each entry's SHA-256 covers the previous entry's
hash, so editing, deleting or reordering lines breaks the chain.

Recorded per call: the caller (from Entra ID or the shared key), the endpoint, a
SHA-256 and length of the query (not the text - people paste sensitive details into
searches), and the ids of the documents returned or opened.

    python -m brain.audit verify <tenant>      # or: all
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
_lock = threading.Lock()


def audit_dir() -> Path:
    return Path(os.environ.get("BRAIN_AUDIT_DIR", "data/audit"))


def _safe(tenant: str) -> str:
    return "".join(c for c in tenant if c.isalnum() or c == "-") or "default"


def _hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _paths(tenant: str) -> tuple[Path, Path]:
    d = audit_dir()
    return d / f"{_safe(tenant)}.jsonl", d / f"{_safe(tenant)}.head"


def _last(log: Path, head: Path) -> tuple[int, str]:
    if head.exists():
        parts = head.read_text(encoding="utf-8").split()
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    return 0, GENESIS


def record(principal: dict, action: str, details: dict) -> dict:
    tenant = principal.get("tenant") or "default"
    log, head = _paths(tenant)
    with _lock:
        log.parent.mkdir(parents=True, exist_ok=True)
        seq, prev = _last(log, head)            # O(1): the head file, not a re-read
        entry = {"seq": seq + 1, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "actor": principal, "action": action, "details": details, "prev": prev}
        entry["hash"] = _hash(entry)
        with log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        head.write_text(f"{entry['seq']} {entry['hash']}\n", encoding="utf-8")
        return entry


def query_fingerprint(q: str) -> dict:
    return {"query_sha256": hashlib.sha256(q.encode("utf-8")).hexdigest(), "query_chars": len(q)}


def verify(tenant: str) -> list[str]:
    log, head = _paths(tenant)
    if not log.exists():
        return []
    prev, n = GENESIS, 0
    for n, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return [f"line {n} is not valid JSON"]
        if e.get("seq") != n or e.get("prev") != prev:
            return [f"entry {n}: chain broken (missing, reordered or inserted entries)"]
        if _hash(e) != e.get("hash"):
            return [f"entry {n}: contents changed after writing"]
        prev = e["hash"]
    hs, hh = _last(log, head)
    if (hs, hh) != (n, prev):
        return [f"head says entry {hs}, log ends at {n} - entries removed from the end?"]
    return []


def main(argv: list[str]) -> int:
    tenants = ([p.stem for p in audit_dir().glob("*.jsonl")] if not argv or argv[0] == "all"
               else argv)
    bad = 0
    for t in tenants:
        problems = verify(t)
        bad += bool(problems)
        print(f"{t}: {'OK' if not problems else problems[0]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] == "verify" else sys.argv[1:]))
