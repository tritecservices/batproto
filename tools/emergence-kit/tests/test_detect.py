"""detect: scored against fake-card ground truth, plus the scorer's own honesty."""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "devtools"))

import make_fake_card                                         # noqa: E402
import score_detections                                       # noqa: E402
from emergence_kit.detect import DETECTION_COLUMNS, DetectOptions, _direction, run  # noqa: E402
from emergence_kit.prep import PrepOptions, prep              # noqa: E402
from emergence_kit.scan import scan                           # noqa: E402

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class Direction(unittest.TestCase):
    def test_directions(self):
        self.assertEqual(_direction([(10, 100), (12, 20)]), "up")
        self.assertEqual(_direction([(10, 50), (90, 52)]), "right")
        self.assertEqual(_direction([(90, 90), (10, 10)]), "up-left")
        self.assertEqual(_direction([(5, 5), (6, 6)]), "stationary")


@unittest.skipUnless(HAVE_FFMPEG and HAVE_NUMPY, "needs ffmpeg and numpy")
class Detect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cards, cls.dest = cls.tmp / "cards", cls.tmp / "review"
        make_fake_card.main([str(cards), "--small", "--cards", "1", "--clips", "2",
                             "--clip-seconds", "20", "--format", "xavc", "--seed", "11"])
        prep(scan([cards], use_exiftool=False), PrepOptions(dest=cls.dest), say=lambda m: None)
        run(cls.dest, DetectOptions(), say=lambda m: None)
        cls.score = score_detections.score(cards / "ground_truth.csv", cls.dest)["total"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_catches_simulated_bats(self):
        self.assertGreaterEqual(self.score["recall"], 0.9, self.score)

    def test_false_alarms_stay_low(self):
        self.assertGreaterEqual(self.score["precision"], 0.8, self.score)

    def test_no_catch_all_events(self):
        for f in self.dest.rglob("detections_*.csv"):
            rows = list(csv.DictReader(open(f, encoding="utf-8")))
            self.assertEqual(list(rows[0]) if rows else DETECTION_COLUMNS, DETECTION_COLUMNS)
            self.assertTrue(all(float(r["duration_s"]) <= score_detections.MAX_DET_S for r in rows))
        self.assertTrue(json.loads((self.dest / "detect_report.json").read_text())["recordings"])


class ScorerHonesty(unittest.TestCase):
    """A single detection spanning the whole video must NOT score as catching everything."""

    def test_catch_all_detection_scores_zero(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "r").mkdir()
            (tmp / "prep_report.json").write_text(json.dumps({"recordings": [{
                "id": "r", "card": "C1", "recording": {"start": "2026-06-14T21:05:00",
                                                        "duration_s": 60}}]}))
            with open(tmp / "truth.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["card", "clock_time", "event", "notes"])
                w.writerows([["C1", "21:05:10", "pass", "synthetic"],
                             ["C1", "21:05:30", "pass", "synthetic"]])
            with open(tmp / "r" / "detections_r.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=DETECTION_COLUMNS)
                w.writeheader()
                w.writerow({"clock_time": "21:05:00", "duration_s": "60", "kind": "motion"})
            t = score_detections.score(tmp / "truth.csv", tmp)["total"]
            self.assertEqual((t["caught"], t["false_alarms"]), (0, 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
