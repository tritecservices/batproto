"""prep: local copies with checksums, review copies, idempotency, tamper detection."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "devtools"))

import make_fake_card                                          # noqa: E402
from emergence_kit.cli import main                             # noqa: E402
from emergence_kit.prep import PrepOptions, clock_epoch, prep, review_filter, verify  # noqa: E402
from emergence_kit.scan import scan                            # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
QUIET = lambda _m: None                                        # noqa: E731


class Pure(unittest.TestCase):
    def test_clock_epoch_shows_camera_wall_clock(self):
        e = clock_epoch("2026-06-14T21:05:00+01:00")           # Sony XML style
        self.assertEqual(datetime.fromtimestamp(e, timezone.utc).strftime("%H:%M:%S"),
                         "21:05:00")
        e2 = clock_epoch("2026-06-14T21:05:00")                  # naive (AVCHD/exiftool)
        self.assertEqual(e, e2)

    def test_filter_only_deinterlaces_and_scales_when_needed(self):
        rec = {"start": "2026-06-14T21:05:00", "interlaced": True, "height": 1080}
        f = review_filter(rec)
        self.assertTrue(f.startswith("yadif") and "scale=-2:720" in f and "drawtext" in f)
        f2 = review_filter({**rec, "interlaced": False, "height": 720})
        self.assertTrue(f2.startswith("drawtext"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class Prep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        make_fake_card.main([str(cls.tmp / "cards"), "--small", "--cards", "1", "--clips", "2",
                             "--clip-seconds", "4", "--format", "xavc"])
        cls.manifest = scan([cls.tmp / "cards"], use_exiftool=False)
        cls.dest = cls.tmp / "review"
        cls.report = prep(cls.manifest, PrepOptions(dest=cls.dest), say=QUIET)
        cls.rid = cls.manifest["recordings"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_copies_keep_card_layout_and_are_checksummed(self):
        lines = (self.dest / "checksums.sha256").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(" *originals/CARD1_XAVC/PRIVATE/M4ROOT/CLIP/" in ln for ln in lines))
        self.assertEqual(verify(self.dest, say=QUIET), [])

    def test_review_copy_matches_length_and_has_1s_keyframes(self):
        review = self.dest / self.rid / f"{self.rid}_review.mp4"
        self.assertTrue(review.exists())
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                              "-skip_frame", "nokey", "-show_entries",
                              # best_effort_timestamp_time exists in ffprobe 4.x and 5+;
                              # pts_time only from 5.0
                              "frame=best_effort_timestamp_time",
                              "-of", "csv=p=0", str(review)],
                             capture_output=True, text=True).stdout.split()
        times = [float(t.strip(",")) for t in out if t.strip(",")]
        self.assertGreaterEqual(len(times), 7)                     # 8 s -> ~8 keyframes
        gaps = {round(b - a, 2) for a, b in zip(times, times[1:])}
        self.assertEqual(gaps, {1.0})
        self.assertAlmostEqual(self.report["recordings"][0]["review"]["duration_s"], 8, delta=0.6)

    def test_review_log_has_header_only(self):
        with (self.dest / self.rid / f"review_log_{self.rid}.csv").open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows, [make_fake_card.REVIEW_COLUMNS])

    def test_second_run_does_nothing(self):
        again = prep(self.manifest, PrepOptions(dest=self.dest), say=QUIET)
        r = again["recordings"][0]
        self.assertEqual((r["copied"], r["skipped_copy"]), (0, 2))
        self.assertEqual(r["review"]["status"], "already done")

    def test_verify_catches_a_changed_copy(self):
        victim = next((self.dest / "originals").rglob("C0002.MP4"))
        data = victim.read_bytes()
        try:
            victim.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            self.assertEqual(len(verify(self.dest, say=QUIET)), 1)
        finally:
            victim.write_bytes(data)

    def test_dry_run_writes_nothing(self):
        dest = self.tmp / "dry"
        rep = prep(self.manifest, PrepOptions(dest=dest, dry_run=True), say=QUIET)
        self.assertTrue(rep["dry_run"])
        self.assertFalse(dest.exists())

    def test_cli_prep_and_verify(self):
        m = self.tmp / "manifest.json"
        m.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assertEqual(main(["prep", str(m), "--to", str(self.dest)]), 0)
        self.assertEqual(main(["verify", str(self.dest)]), 0)


if __name__ == "__main__":
    unittest.main()
