"""report + sun: sunset accuracy, log validation, midnight, end-to-end vs ground truth."""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "devtools"))

import fill_logs_from_truth                                      # noqa: E402
import make_fake_card                                            # noqa: E402
from emergence_kit.prep import REVIEW_COLUMNS, PrepOptions, prep  # noqa: E402
from emergence_kit.report import Result, parse_log, run          # noqa: E402
from emergence_kit.scan import scan                              # noqa: E402
from emergence_kit.sun import sunrise, sunset                    # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
LONDON = (51.5074, -0.1278)


class Sun(unittest.TestCase):
    """Reference: published London times (UTC) - within 2 minutes."""

    def near(self, got: datetime, hh: int, mm: int):
        want = got.replace(hour=hh, minute=mm, second=0, microsecond=0)
        self.assertLess(abs((got - want).total_seconds()), 120, got)

    def test_midsummer_and_midwinter(self):
        self.near(sunset(date(2026, 6, 21), *LONDON), 20, 21)
        self.near(sunrise(date(2026, 6, 21), *LONDON), 3, 43)
        self.near(sunset(date(2026, 12, 21), *LONDON), 15, 53)
        self.near(sunrise(date(2026, 12, 21), *LONDON), 8, 4)

    def test_polar_day_has_no_sunset(self):
        self.assertIsNone(sunset(date(2026, 6, 21), 78.2, 15.6))


class LogChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        start = datetime(2026, 6, 14, 23, 30)
        self.rec = Result(id="r", card="c", start=start, end=start + timedelta(hours=1),
                          start_source="sony-xml", camera=None, mode="dusk",
                          sun_label="sunset", sun_time=datetime(2026, 6, 14, 21, 19))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rows):
        p = self.tmp / "log.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({**dict.fromkeys(REVIEW_COLUMNS, ""), **r})
        return p

    def test_past_midnight_lands_on_next_day(self):
        parse_log(self.write([{"clock_time": "00:10:00", "event": "pass", "count": "1"}]), self.rec)
        self.assertEqual(self.rec.events[0]["time"], datetime(2026, 6, 15, 0, 10))
        self.assertAlmostEqual(self.rec.events[0]["rel_min"], 171)

    def test_bad_rows_are_reported_by_row_and_excluded(self):
        parse_log(self.write([
            {"clock_time": "23:40", "event": "Emergence", "count": "2"},     # ok, HH:MM + case
            {"clock_time": "23:41:00", "event": "emergence", "count": "0"},  # bad count
            {"clock_time": "21:00:00", "event": "emergence", "count": "1"},  # before recording
            {"clock_time": "", "event": "", "count": ""},                    # blank: ignored
        ]), self.rec)
        self.assertEqual([e["count"] for e in self.rec.events], [2])
        self.assertEqual(sorted(i.row for i in self.rec.issues), [3, 4])

    def test_empty_log_is_flagged(self):
        parse_log(self.write([]), self.rec)
        self.assertTrue(any("empty" in i.message for i in self.rec.issues))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class EndToEnd(unittest.TestCase):
    """make_fake_card -> scan -> prep -> reviewer (from ground truth) -> report."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cards, dest = cls.tmp / "cards", cls.tmp / "review"
        make_fake_card.main([str(cards), "--small", "--cards", "1", "--clips", "2",
                             "--clip-seconds", "10", "--format", "xavc"])
        prep(scan([cards], use_exiftool=False), PrepOptions(dest=dest), say=lambda m: None)
        fill_logs_from_truth.main([str(cards / "ground_truth.csv"), str(dest), "--with-mistakes"])
        cls.summary = run(dest, *LONDON, site="Test barn")
        cls.truth = list(csv.DictReader(open(cards / "ground_truth.csv", encoding="utf-8")))
        cls.dest = dest

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_first_emergence_and_totals_match_truth(self):
        rec = self.summary["recordings"][0]
        em = sorted(t["clock_time"] for t in self.truth if t["event"] == "emergence")
        self.assertEqual(rec["first_emergence"], em[0])
        for ev in ("emergence", "re-entry", "pass"):
            self.assertEqual(rec[f"total_{ev}"],
                             sum(int(t["count"]) for t in self.truth if t["event"] == ev))
        self.assertEqual(rec["rows_rejected"], 2)
        self.assertEqual(rec["sunset"], "21:19")                   # 14 June, London, BST

    def test_outputs_exist_and_location_is_not_leaked(self):
        for name in ("report.html", "summary.csv", "events.csv", "summary.json"):
            text = (self.dest / name).read_text(encoding="utf-8")
            self.assertNotIn("51.5", text)
            self.assertNotIn("0.127", text)
        self.assertIn("Check before use", (self.dest / "report.html").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.dest / "summary.json").read_text())["site"], "Test barn")


if __name__ == "__main__":
    unittest.main()
