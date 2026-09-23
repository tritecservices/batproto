"""Review sign-off and second-reviewer QA.

Sign-off: the reviewer declares their log complete. The log's SHA-256 goes into the
audit trail, so any later edit is detectable (report flags it).

QA: a *different* person reviews the same recording independently, into
<id>/qa_log_<id>.csv (same columns as the review log), without seeing the first log.
The two are compared event by event; the QA reviewer then approves or rejects, and
the comparison figures are recorded with the decision.

Why a different person: a second review by the same person isn't a second opinion,
and a QA record that says otherwise would mislead whoever relies on the survey.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import audit
from .report import EVENTS, TIME_RE

MATCH_TOLERANCE_S = 2


class QAError(Exception):
    pass


def log_paths(dest: Path, rid: str) -> tuple[Path, Path]:
    d = Path(dest) / rid
    return d / f"review_log_{rid}.csv", d / f"qa_log_{rid}.csv"


def _read(path: Path) -> list[dict]:
    """Rows that parse; QA compares what both reviewers actually recorded."""
    out = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            m = TIME_RE.match(row.get("clock_time") or "")
            ev = (row.get("event") or "").strip().lower()
            try:
                n = int(str(row.get("count") or "").strip())
            except ValueError:
                continue
            if not m or ev not in EVENTS or n < 1:
                continue
            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if h > 23 or mi > 59 or s > 59:
                continue
            out.append({"t": h * 3600 + mi * 60 + s, "event": ev, "count": n})
    return sorted(out, key=lambda r: r["t"])


def compare(primary: list[dict], second: list[dict], tol: int = MATCH_TOLERANCE_S) -> dict:
    """Greedy one-to-one matching: same event type within `tol` seconds.
    Times are seconds since midnight; surveys crossing midnight are unwrapped."""
    def unwrap(rows):
        if rows and rows[-1]["t"] - rows[0]["t"] > 12 * 3600:      # crossed midnight
            return [{**r, "t": r["t"] + (86400 if r["t"] < 12 * 3600 else 0)} for r in rows]
        return rows
    a, b = unwrap(primary), unwrap(second)
    used = set()
    matched = 0
    for r in a:
        best = None
        for j, q in enumerate(b):
            if j in used or q["event"] != r["event"]:
                continue
            d = abs(q["t"] - r["t"])
            if d <= tol and (best is None or d < best[1]):
                best = (j, d)
        if best:
            used.add(best[0])
            matched += 1
    totals = {}
    for ev in EVENTS:
        pa = sum(r["count"] for r in a if r["event"] == ev)
        pb = sum(r["count"] for r in b if r["event"] == ev)
        if pa or pb:
            totals[ev] = {"primary": pa, "qa": pb, "difference": pb - pa}
    n1, n2 = len(a), len(b)
    return {
        "primary_rows": n1, "qa_rows": n2, "matched": matched,
        "only_primary": n1 - matched, "only_qa": n2 - matched,
        "agreement": round(2 * matched / (n1 + n2), 3) if (n1 + n2) else 1.0,
        "totals": totals, "tolerance_s": tol,
    }


def signoff(dest: Path, rid: str, note: str = "") -> dict:
    log, _ = log_paths(dest, rid)
    if not log.exists():
        raise QAError(f"no review log for {rid} at {log}")
    rows = _read(log)
    return audit.record(dest, "review.signoff", {
        "recording": rid, "log": log.relative_to(dest).as_posix(),
        "log_sha256": audit.sha256_file(log), "rows": len(rows), "note": note})


def qa(dest: Path, rid: str, decision: str, note: str = "",
       qa_log: Path | None = None) -> dict:
    if decision not in ("approve", "reject"):
        raise QAError("decision must be approve or reject")
    log, default_qa = log_paths(dest, rid)
    qa_log = Path(qa_log) if qa_log else default_qa
    so = audit.latest(dest, "review.signoff", rid)
    if not so:
        raise QAError(f"{rid} has not been signed off by its reviewer yet")
    if audit.sha256_file(log) != so["details"]["log_sha256"]:
        raise QAError(f"{rid}: the review log changed after sign-off - the reviewer must "
                      "sign off again before QA")
    me = audit.current_actor()
    if me["user"].lower() == so["actor"]["user"].lower():
        raise QAError(f"QA must be done by someone other than the reviewer ({me['user']})")
    if not qa_log.exists():
        raise QAError(f"no QA log at {qa_log} - review the recording independently first")
    result = compare(_read(log), _read(qa_log))
    return audit.record(dest, "review.qa", {
        "recording": rid, "decision": decision, "note": note,
        "reviewer": so["actor"]["user"], "review_signoff_seq": so["seq"],
        "log_sha256": so["details"]["log_sha256"],
        "qa_log": qa_log.relative_to(dest).as_posix() if qa_log.is_relative_to(dest) else str(qa_log),
        "qa_log_sha256": audit.sha256_file(qa_log), "comparison": result}, actor=me)


def status(dest: Path, rid: str) -> dict:
    """Sign-off and QA state of one recording, for report."""
    log, _ = log_paths(dest, rid)
    so = audit.latest(dest, "review.signoff", rid)
    q = audit.latest(dest, "review.qa", rid)
    st: dict = {"signed_off_by": None, "signed_off_at": None, "changed_after_signoff": False,
                "qa": "not done", "qa_by": None, "qa_agreement": None,
                "identity_overridden": False}
    if so:
        st["signed_off_by"], st["signed_off_at"] = so["actor"]["user"], so["time"]
        st["identity_overridden"] |= so["actor"].get("via") == "EMERGENCE_USER"
        st["changed_after_signoff"] = (log.exists() and
                                       audit.sha256_file(log) != so["details"]["log_sha256"])
    if q and so and q["details"].get("review_signoff_seq") == so["seq"]:
        st["qa"] = {"approve": "approved", "reject": "rejected"}[q["details"]["decision"]]
        st["qa_by"] = q["actor"]["user"]
        st["qa_agreement"] = q["details"]["comparison"]["agreement"]
        st["identity_overridden"] |= q["actor"].get("via") == "EMERGENCE_USER"
    elif q:
        st["qa"] = "out of date (log re-signed after QA)"
    return st
