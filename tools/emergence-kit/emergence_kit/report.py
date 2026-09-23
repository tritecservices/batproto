"""report: turn filled-in review logs into emergence/re-entry results.

Reads a prep folder (prep_report.json + <id>/review_log_<id>.csv), checks every
log row, works out sunset or sunrise for the survey location, and writes:

  report.html    human-readable tables, with a "Check before use" section first
  summary.csv    one row per recording (for spreadsheets and Night Summary)
  events.csv     every accepted event with its full date/time and minutes from sun
  summary.json   everything above, structured, for the survey-report-drafter agent

Nothing is inferred that the reviewer didn't record: species groups are copied as
written, counts are summed, times are the camera clock. Rows that fail checks are
listed by row number and left out of the totals, never silently "fixed".

The location is used only to compute the sun times. It is not written to any output,
because a roost location in a report appendix is exactly the leak to avoid.
"""
from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .prep import wall_clock
from .sun import sunrise, sunset

EVENTS = ("emergence", "re-entry", "pass", "foraging", "other")
BIN_MIN = 15
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


@dataclass
class Issue:
    recording: str
    row: int | None
    message: str


@dataclass
class Result:
    id: str
    card: str
    start: datetime                     # camera wall clock, naive
    end: datetime
    start_source: str
    camera: str | None
    mode: str                           # dusk | dawn
    sun_label: str                      # sunset | sunrise
    sun_time: datetime | None           # wall clock, naive
    events: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    log_rows: int = 0
    review: dict = field(default_factory=dict)          # sign-off and QA status

    # ---------------------------------------------------------- derived
    def rel(self, t: datetime) -> float | None:
        """Minutes after the sun event (negative = before)."""
        return None if self.sun_time is None else (t - self.sun_time).total_seconds() / 60

    @property
    def key_event(self) -> str:
        return "emergence" if self.mode == "dusk" else "re-entry"

    def totals(self) -> dict[str, int]:
        c: Counter = Counter()
        for e in self.events:
            c[e["event"]] += e["count"]
        return {k: c.get(k, 0) for k in EVENTS}

    def by_species(self) -> dict[str, dict[str, int]]:
        out: dict[str, Counter] = defaultdict(Counter)
        for e in self.events:
            out[e["species_group"] or "(not recorded)"][e["event"]] += e["count"]
        return {sp: dict(c) for sp, c in sorted(out.items())}

    def first_last(self) -> tuple[dict | None, dict | None]:
        key = [e for e in self.events if e["event"] == self.key_event]
        if not key:
            return None, None
        key.sort(key=lambda e: e["time"])
        return key[0], key[-1]

    def bins(self) -> list[dict]:
        """Counts per 15 minutes relative to the sun event, key event and all."""
        if self.sun_time is None:
            return []
        rows: dict[int, Counter] = defaultdict(Counter)
        for e in self.events:
            b = int((e["rel_min"] // BIN_MIN) * BIN_MIN)
            rows[b][e["event"]] += e["count"]
        return [{"from_min": b, "to_min": b + BIN_MIN, **{k: rows[b].get(k, 0) for k in EVENTS}}
                for b in sorted(rows)]

    def window(self) -> tuple[float | None, float | None]:
        return self.rel(self.start), self.rel(self.end)


# ------------------------------------------------------------------ inputs
def _utc_offset(start_iso: str, override_h: float | None) -> timedelta:
    """Offset between camera wall clock and UTC, most trustworthy first."""
    if override_h is not None:
        return timedelta(hours=override_h)
    dt = datetime.fromisoformat(start_iso)
    if dt.utcoffset() is not None and dt.utcoffset().total_seconds() != 0:
        return dt.utcoffset()
    # naive camera time (AVCHD) or UTC: assume the laptop's zone on that date
    return wall_clock(start_iso).astimezone().utcoffset() or timedelta(0)


def parse_log(path: Path, rec: Result) -> None:
    if not path.exists():
        rec.issues.append(Issue(rec.id, None, "no review log found"))
        return
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in ("clock_time", "event", "count") if c not in (reader.fieldnames or [])]
        if missing:
            rec.issues.append(Issue(rec.id, None, f"log is missing columns: {', '.join(missing)}"))
            return
        for n, row in enumerate(reader, start=2):              # row 1 is the header
            if not any((v or "").strip() for v in row.values()):
                continue
            rec.log_rows += 1
            problems = []
            m = TIME_RE.match(row.get("clock_time") or "")
            if not m:
                problems.append(f"clock_time '{row.get('clock_time')}' is not HH:MM:SS")
            event = (row.get("event") or "").strip().lower()
            if event not in EVENTS:
                problems.append(f"event '{row.get('event')}' is not one of {', '.join(EVENTS)}")
            try:
                count = int(str(row.get("count") or "").strip())
                if count < 1:
                    raise ValueError
            except ValueError:
                problems.append(f"count '{row.get('count')}' is not a whole number of 1 or more")
                count = 0
            t = None
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
                if h > 23 or mi > 59 or s > 59:
                    problems.append(f"clock_time '{row.get('clock_time')}' is not a real time")
                else:
                    t = rec.start.replace(hour=h, minute=mi, second=s, microsecond=0)
                    if t < rec.start - timedelta(hours=1):       # survey ran past midnight
                        t += timedelta(days=1)
                    if not (rec.start - timedelta(seconds=60) <= t <= rec.end + timedelta(seconds=60)):
                        problems.append(f"{row['clock_time']} is outside the recording "
                                        f"({rec.start:%H:%M:%S}-{rec.end:%H:%M:%S})")
            if problems:
                rec.issues.append(Issue(rec.id, n, "; ".join(problems)))
                continue
            rec.events.append({
                "time": t, "event": event, "count": count,
                "species_group": (row.get("species_group") or "").strip(),
                "direction": (row.get("direction") or "").strip(),
                "observer": (row.get("observer") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
                "rel_min": rec.rel(t) if rec.sun_time else None,
            })
    rec.events.sort(key=lambda e: e["time"])
    if rec.log_rows == 0:
        rec.issues.append(Issue(rec.id, None, "review log is empty - not reviewed yet?"))


def build(dest: Path, lat: float, lon: float, utc_offset_h: float | None = None) -> list[Result]:
    prep_report = json.loads((dest / "prep_report.json").read_text(encoding="utf-8"))
    results = []
    for entry in prep_report["recordings"]:
        r = entry["recording"]
        start = wall_clock(r["start"])
        end = start + timedelta(seconds=r["duration_s"])
        mode = "dusk" if start.hour >= 12 else "dawn"
        offset = _utc_offset(r["start"], utc_offset_h)
        sun_utc = (sunset if mode == "dusk" else sunrise)(start.date(), lat, lon)
        sun_wall = (sun_utc + offset).replace(tzinfo=None) if sun_utc else None
        res = Result(id=entry["id"], card=entry["card"], start=start, end=end,
                     start_source=r.get("start_source") or "?",
                     camera=" ".join(x for x in (r.get("camera_model"), r.get("camera_serial")) if x) or None,
                     mode=mode, sun_label="sunset" if mode == "dusk" else "sunrise",
                     sun_time=sun_wall)
        if sun_wall is None:
            res.issues.append(Issue(res.id, None, f"no {res.sun_label} at this latitude on {start:%d %b}"))
        if res.start_source in ("file-mtime",):
            res.issues.append(Issue(res.id, None, "recording start estimated from file dates - "
                                    "check against the camera clock before relying on times"))
        elif res.start_source in ("ffprobe", "exiftool") and any(
                "MP4 header" in w for w in entry.get("warnings", [])):
            res.issues.append(Issue(res.id, None, "start time from MP4 header (read as UTC) - "
                                    "confirm the camera clock"))
        parse_log(dest / entry["id"] / f"review_log_{entry['id']}.csv", res)
        from .qa import status as review_status
        res.review = review_status(dest, res.id)
        if not res.review["signed_off_by"]:
            res.issues.append(Issue(res.id, None, "review not signed off by the reviewer"))
        elif res.review["changed_after_signoff"]:
            res.issues.append(Issue(res.id, None, f"review log changed after "
                                    f"{res.review['signed_off_by']} signed it off"))
        if res.review.get("identity_overridden"):
            res.issues.append(Issue(res.id, None, "sign-off or QA identity was set with "
                                    "EMERGENCE_USER, not taken from the Windows account"))
        if res.review["qa"] == "rejected":
            res.issues.append(Issue(res.id, None, f"QA rejected by {res.review['qa_by']}"))
        elif res.review["qa"] != "approved":
            res.issues.append(Issue(res.id, None, f"second-reviewer QA: {res.review['qa']}"))
        results.append(res)
    return results


# ----------------------------------------------------------------- outputs
def _fmt_rel(m: float | None) -> str:
    if m is None:
        return "-"
    m = round(m)
    return f"{m:+d} min"


def summary_rows(results: list[Result]) -> list[dict]:
    rows = []
    for r in results:
        first, last = r.first_last()
        w0, w1 = r.window()
        t = r.totals()
        rows.append({
            "recording": r.id, "card": r.card, "camera": r.camera or "",
            "date": f"{r.start:%Y-%m-%d}", "survey": r.mode,
            "start": f"{r.start:%H:%M:%S}", "end": f"{r.end:%H:%M:%S}",
            "time_source": r.start_source,
            r.sun_label: f"{r.sun_time:%H:%M}" if r.sun_time else "",
            "start_vs_sun_min": round(w0) if w0 is not None else "",
            "end_vs_sun_min": round(w1) if w1 is not None else "",
            f"first_{r.key_event}": f"{first['time']:%H:%M:%S}" if first else "",
            f"first_{r.key_event}_vs_sun_min": round(first["rel_min"]) if first and first["rel_min"] is not None else "",
            f"last_{r.key_event}": f"{last['time']:%H:%M:%S}" if last else "",
            f"last_{r.key_event}_vs_sun_min": round(last["rel_min"]) if last and last["rel_min"] is not None else "",
            **{f"total_{k}": v for k, v in t.items()},
            "log_rows": r.log_rows, "rows_rejected": sum(1 for i in r.issues if i.row),
            "signed_off_by": r.review.get("signed_off_by") or "",
            "qa": r.review.get("qa", ""), "qa_by": r.review.get("qa_by") or "",
            "qa_agreement": r.review.get("qa_agreement") if r.review.get("qa_agreement") is not None else "",
        })
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    cols: list[str] = []
    for row in rows:
        cols += [k for k in row if k not in cols]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _table(headers: list[str], rows: list[list], cls: str = "") -> str:
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
                   for row in rows)
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def render_html(results: list[Result], site: str, audit_problems: list[str] | None = None) -> str:
    esc = html.escape
    issues = [Issue("audit trail", None, p) for p in (audit_problems or [])]
    issues += [i for r in results for i in r.issues]
    parts = [f"<h1>Emergence survey results: {esc(site)}</h1>",
             f'<p class="meta">Generated {datetime.now():%d %B %Y %H:%M} by Emergence Review '
             f"Kit {__version__}. Times are camera clock times; sun times are calculated "
             f"for the survey location (not shown). Species groups are as recorded by the "
             f"reviewer.</p>"]
    if issues:
        parts.append('<section class="check"><h2>Check before use</h2><ul>')
        for i in issues:
            where = f"{i.recording}, log row {i.row}" if i.row else i.recording
            parts.append(f"<li><b>{esc(where)}</b>: {esc(i.message)}</li>")
        parts.append("</ul></section>")

    parts.append("<h2>Summary</h2>")
    srows = []
    for r in results:
        first, last = r.first_last()
        w0, w1 = r.window()
        t = r.totals()
        srows.append([r.id, f"{r.start:%d/%m/%Y}", f"{r.start:%H:%M}-{r.end:%H:%M}",
                      f"{r.sun_label} {r.sun_time:%H:%M}" if r.sun_time else "-",
                      f"{_fmt_rel(w0)} to {_fmt_rel(w1)}",
                      f"{first['time']:%H:%M:%S} ({_fmt_rel(first['rel_min'])})" if first else "none recorded",
                      f"{last['time']:%H:%M:%S} ({_fmt_rel(last['rel_min'])})" if last else "none recorded",
                      t["emergence"], t["re-entry"], t["pass"]])
    parts.append(_table(["Recording", "Date", "Recorded", "Sun", "Window vs sun",
                         "First key event", "Last key event", "Emerged", "Re-entered",
                         "Passes"], srows))

    for r in results:
        parts.append(f"<h2>{esc(r.id)}</h2>")
        parts.append(f'<p class="meta">Card {esc(r.card)}'
                     + (f", camera {esc(r.camera)}" if r.camera else "")
                     + f". {r.mode.title()} survey, {r.start:%H:%M:%S}-{r.end:%H:%M:%S}; "
                     + (f"{r.sun_label} {r.sun_time:%H:%M}" if r.sun_time else "no sun time")
                     + f". Start time from: {esc(r.start_source)}. "
                     + f"Reviewed by: {esc(r.review.get('signed_off_by') or 'not signed off')}. "
                     + f"QA: {esc(r.review.get('qa', 'not done'))}"
                     + (f" by {esc(r.review['qa_by'])}, agreement {r.review['qa_agreement']:.0%}"
                        if r.review.get("qa_by") and r.review.get("qa_agreement") is not None else "")
                     + ".</p>")
        if not r.events:
            parts.append("<p>No accepted events.</p>")
            continue
        sp = r.by_species()
        parts.append(_table(["Species group", *[e for e in EVENTS]],
                            [[s, *[c.get(e, 0) for e in EVENTS]] for s, c in sp.items()]))
        b = r.bins()
        if b:
            parts.append(f"<h3>Counts per {BIN_MIN} minutes from {r.sun_label}</h3>")
            parts.append(_table(["Minutes", *EVENTS],
                                [[f"{x['from_min']:+d} to {x['to_min']:+d}", *[x[e] for e in EVENTS]]
                                 for x in b], "bins"))
    css = """body{font:14px/1.5 Segoe UI,Arial,sans-serif;color:#1b2620;max-width:1000px;margin:2em auto;padding:0 16px}
h1{font-size:1.6em}h2{margin-top:1.8em;border-bottom:1px solid #ccc}h3{font-size:1em}
table{border-collapse:collapse;margin:.6em 0 1.2em;width:100%}th,td{border:1px solid #bbb;padding:4px 8px;text-align:left}
th{background:#eef2ee}.meta{color:#555}.check{background:#fff6dc;border-left:4px solid #d9a400;padding:.4em 1em}
@media print{.check{break-inside:avoid}}"""
    return (f"<!doctype html><html lang='en-GB'><head><meta charset='utf-8'>"
            f"<title>{esc(site)} - emergence results</title><style>{css}</style></head>"
            f"<body>{''.join(parts)}</body></html>")


def run(dest: Path, lat: float, lon: float, site: str, utc_offset_h: float | None = None,
        out_dir: Path | None = None) -> dict:
    from . import audit
    out_dir = out_dir or dest
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_problems, _ = audit.verify(dest)
    results = build(dest, lat, lon, utc_offset_h)
    rows = summary_rows(results)
    write_csv(out_dir / "summary.csv", rows)
    events = [{"recording": r.id, "date": f"{e['time']:%Y-%m-%d}", "clock_time": f"{e['time']:%H:%M:%S}",
               "minutes_from_sun": round(e["rel_min"], 1) if e["rel_min"] is not None else "",
               "sun_event": r.sun_label, **{k: e[k] for k in ("event", "count", "species_group",
                                                            "direction", "observer", "notes")}}
              for r in results for e in r.events]
    write_csv(out_dir / "events.csv", events or [{"recording": ""}])
    (out_dir / "report.html").write_text(render_html(results, site, audit_problems), encoding="utf-8")
    summary = {
        "site": site, "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": f"emergence-kit {__version__}",
        "note": "Camera clock times. Sun times computed for the survey location, which is "
                "deliberately not included. Species groups as recorded by the reviewer.",
        "audit": {"verified": not audit_problems, "problems": audit_problems},
        "recordings": rows,
        "issues": [{"recording": "audit trail", "row": None, "message": p} for p in audit_problems]
                  + [{"recording": i.recording, "row": i.row, "message": i.message}
                     for r in results for i in r.issues],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not audit_problems:        # a broken chain is reported, never extended
        audit.record(dest, "report", {
            "site": site, "recordings": [r.id for r in results],
            "outputs": {n: audit.sha256_file(out_dir / n) for n in
                        ("report.html", "summary.csv", "events.csv", "summary.json")}})
    return summary
