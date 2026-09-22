"""MP4 cards (Sony XAVC S and generic DCIM cameras) plus the fake-card generator.

Uses devtools/make_fake_card.py to build tiny cards (640x360, 3-second clips), so
these tests also prove the generator works. Skipped without ffmpeg.
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "devtools"))

import make_fake_card                                   # noqa: E402
from emergence_kit.probe import read_sony_xml           # noqa: E402
from emergence_kit.scan import card_root, scan          # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class SonySidecar(unittest.TestCase):
    def test_reads_start_model_serial(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            xml = tmp / "C0001M01.XML"
            xml.write_text(make_fake_card.sony_xml(
                datetime.fromisoformat("2026-06-14T21:05:00+01:00"), 1500, "SN123"),
                encoding="utf-8")
            meta = read_sony_xml(xml)
            self.assertEqual(meta["start"].isoformat(), "2026-06-14T21:05:00+01:00")
            self.assertEqual((meta["model"], meta["serial"]), ("SIMULATED-CAM", "SN123"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_card_roots_for_mp4_layouts(self):
        root = Path("/cards")
        self.assertEqual(card_root(root / "A" / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4", root),
                         root / "A")
        self.assertEqual(card_root(root / "B" / "DCIM" / "100MEDIA" / "VID_0001.MP4", root),
                         root / "B")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class FakeCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        make_fake_card.main([str(cls.tmp), "--small", "--cards", "2", "--clips", "2",
                             "--clip-seconds", "8", "--start", "2026-06-14 21:05:00"])
        cls.m = scan([cls.tmp], use_exiftool=False)
        cls.by_card: dict[str, list[dict]] = {}
        for r in cls.m["recordings"]:
            cls.by_card.setdefault(r["card"], []).append(r)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_card_found_and_nothing_failed(self):
        self.assertEqual(self.m["summary"]["failed"], 0)
        expected = sorted(f"CARD{i}_{f}" for f in ("AVCHD", "DCIM", "XAVC") for i in (1, 2))
        self.assertEqual(sorted(self.by_card), expected)

    def test_continuous_cards_are_one_recording(self):
        for fmt in ("AVCHD", "DCIM", "XAVC"):
            recs = self.by_card[f"CARD1_{fmt}"]
            self.assertEqual(len(recs), 1, fmt)
            self.assertEqual(recs[0]["clip_count"], 2, fmt)

    def test_restarted_cards_are_two_recordings(self):
        for fmt in ("AVCHD", "DCIM", "XAVC"):
            self.assertEqual(len(self.by_card[f"CARD2_{fmt}"]), 2, fmt)

    def test_xavc_uses_sidecar_time_exactly(self):
        rec = self.by_card["CARD1_XAVC"][0]
        self.assertEqual(rec["start_source"], "sony-xml")
        self.assertEqual(rec["start"], "2026-06-14T21:05:00+01:00")
        self.assertEqual(rec["camera_serial"], "SIM0001")
        self.assertEqual(rec["id"], "card1-xavc-20260614-210500")   # camera wall clock
        self.assertFalse(rec["interlaced"])

    def test_dcim_mp4_header_time_is_flagged(self):
        rec = self.by_card["CARD1_DCIM"][0]
        self.assertEqual(rec["start_source"], "ffprobe")
        self.assertTrue(any("MP4 header" in w for w in rec["warnings"]))

    def test_avchd_is_interlaced_and_mtime_timed(self):
        rec = self.by_card["CARD1_AVCHD"][0]
        self.assertTrue(rec["interlaced"])
        self.assertEqual(rec["start_source"], "file-mtime")

    def test_ground_truth_matches_review_log_columns(self):
        with (self.tmp / "ground_truth.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(rows)
        self.assertEqual(list(rows[0])[1:], make_fake_card.REVIEW_COLUMNS)
        self.assertTrue(all(r["event"] in ("emergence", "re-entry", "pass") for r in rows))
        self.assertTrue((self.tmp / "SYNTHETIC_DATA_README.txt").exists())


if __name__ == "__main__":
    unittest.main()
