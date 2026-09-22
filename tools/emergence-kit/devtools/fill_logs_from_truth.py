"""Play the reviewer: fill prep's empty review logs from a fake card's ground truth.

    python devtools/fill_logs_from_truth.py C:\\FakeCards\\ground_truth.csv C:\\Review\\test
    python devtools/fill_logs_from_truth.py ... --with-mistakes     # add 2 bad rows per log

Lets you run scan -> prep -> report end to end tonight, and check that report's
numbers match what was simulated. --with-mistakes adds a row with an impossible
time and one with an unknown event, to show how report flags them.

SYNTHETIC DATA ONLY. Never point this at real review logs: it overwrites them.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emergence_kit.prep import REVIEW_COLUMNS, wall_clock   # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("truth", help="ground_truth.csv from make_fake_card.py")
    ap.add_argument("prep_dir", help="folder prep wrote to")
    ap.add_argument("--with-mistakes", action="store_true")
    args = ap.parse_args(argv)

    truth = list(csv.DictReader(open(args.truth, encoding="utf-8")))
    if not truth or truth[0].get("notes") != "synthetic":
        sys.exit("refusing: that doesn't look like make_fake_card ground truth")
    prep = json.loads((Path(args.prep_dir) / "prep_report.json").read_text(encoding="utf-8"))
    for entry in prep["recordings"]:
        start = wall_clock(entry["recording"]["start"])
        end = start + timedelta(seconds=entry["recording"]["duration_s"])
        rows = []
        for t in truth:
            if t["card"] != entry["card"]:
                continue
            clock = datetime.combine(start.date(), datetime.strptime(t["clock_time"], "%H:%M:%S").time())
            if start - timedelta(seconds=2) <= clock <= end + timedelta(seconds=2):
                rows.append({k: t[k] for k in REVIEW_COLUMNS})
        if args.with_mistakes:
            rows.append({**dict.fromkeys(REVIEW_COLUMNS, ""), "clock_time": "25:61:00",
                         "event": "emergence", "count": "1", "observer": "SIM"})
            rows.append({**dict.fromkeys(REVIEW_COLUMNS, ""), "clock_time": f"{start:%H:%M:%S}",
                         "event": "swooped", "count": "1", "observer": "SIM"})
        log = Path(args.prep_dir) / entry["id"] / f"review_log_{entry['id']}.csv"
        with log.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
            w.writeheader()
            w.writerows(rows)
        print(f"{entry['id']}: {len(rows)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
