"""Score `detect` against a fake card's ground truth: how many bats did it catch, and
how many detections were nothing?

    python devtools/score_detections.py C:\\FakeCards\\ground_truth.csv C:\\Review\\test

A simulated crossing counts as caught when a motion detection covers its time
(+/- TOLERANCE seconds). A detection is a false alarm when no crossing is near it.
Two bats flying together are one crossing, and one detection is enough for them.
Detections longer than MAX_DET_S are counted as false alarms and catch nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emergence_kit.prep import wall_clock   # noqa: E402

TOLERANCE = 1.5
# A detection longer than this can't count as catching anything: otherwise one
# detection spanning the whole video would "catch" every bat and score perfectly.
MAX_DET_S = 6.0


def score(truth_csv: Path, prep_dir: Path) -> dict:
    truth = list(csv.DictReader(open(truth_csv, encoding="utf-8")))
    prep = json.loads((prep_dir / "prep_report.json").read_text(encoding="utf-8"))
    totals = {"crossings": 0, "caught": 0, "detections": 0, "false_alarms": 0}
    per = []
    for entry in prep["recordings"]:
        rid = entry["id"]
        start = wall_clock(entry["recording"]["start"])
        end = start + timedelta(seconds=entry["recording"]["duration_s"])
        det_file = prep_dir / rid / f"detections_{rid}.csv"
        if not det_file.exists():
            continue

        def at(clock: str) -> datetime:
            return datetime.combine(start.date(), datetime.strptime(clock, "%H:%M:%S").time())

        crossings = [at(t["clock_time"]) for t in truth if t["card"] == entry["card"]
                     and start <= at(t["clock_time"]) <= end]
        dets = [(at(d["clock_time"]), float(d["duration_s"]))
                for d in csv.DictReader(open(det_file, encoding="utf-8")) if d["kind"] == "motion"]
        tol = timedelta(seconds=TOLERANCE)
        usable = [(s, dur) for s, dur in dets if dur <= MAX_DET_S]
        caught = sum(1 for c in crossings
                     if any(s - tol <= c <= s + timedelta(seconds=dur) + tol for s, dur in usable))
        false = sum(1 for s, dur in dets
                    if dur > MAX_DET_S
                    or not any(s - tol <= c <= s + timedelta(seconds=dur) + tol for c in crossings))
        per.append({"recording": rid, "crossings": len(crossings), "caught": caught,
                    "detections": len(dets), "false_alarms": false})
        for k, v in zip(("crossings", "caught", "detections", "false_alarms"),
                        (len(crossings), caught, len(dets), false)):
            totals[k] += v
    totals["recall"] = round(totals["caught"] / totals["crossings"], 3) if totals["crossings"] else None
    totals["precision"] = (round(1 - totals["false_alarms"] / totals["detections"], 3)
                           if totals["detections"] else None)
    return {"per_recording": per, "total": totals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("truth")
    ap.add_argument("prep_dir")
    a = ap.parse_args(argv)
    res = score(Path(a.truth), Path(a.prep_dir))
    for r in res["per_recording"]:
        print(f"{r['recording']}: caught {r['caught']}/{r['crossings']}, "
              f"{r['false_alarms']} false alarm(s) from {r['detections']} detection(s)")
    t = res["total"]
    print(f"\nTOTAL caught {t['caught']}/{t['crossings']} (recall {t['recall']}), "
          f"{t['false_alarms']} false alarm(s) (precision {t['precision']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
