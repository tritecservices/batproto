"""Run: python -m unittest discover -s tests -v   (from the emergence-kit folder)

The integration tests generate real AVCHD-style .MTS clips with ffmpeg, so they
exercise the same ffprobe path the tool uses on a camera card. They are skipped
when ffmpeg isn't installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emergence_kit.cli import main                         # noqa: E402
from emergence_kit.probe import ClipInfo, parse_exif_datetime  # noqa: E402
from emergence_kit.scan import card_root, group_clips, scan  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def clip(name, start, dur=600.0, w=1920, h=1080, fps=25.0, codec="h264"):
    return ClipInfo(path=name, size_bytes=1, duration_s=dur, width=w, height=h, fps=fps,
                    codec=codec, start=start.isoformat(), start_source="exiftool")


class ExifDates(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_exif_datetime("2026:06:14 21:03:55"),
                         datetime(2026, 6, 14, 21, 3, 55))
        self.assertEqual(parse_exif_datetime("2026:06:14 21:03:55+01:00").utcoffset(),
                         timedelta(hours=1))
        self.assertEqual(parse_exif_datetime("2026:06:14 21:03:55 DST"),
                         datetime(2026, 6, 14, 21, 3, 55))
        self.assertIsNone(parse_exif_datetime("not a date"))


class Grouping(unittest.TestCase):
    t0 = datetime(2026, 6, 14, 20, 45, 0)

    def test_contiguous_split_clips_join(self):
        clips = [clip(f"{i:05d}.MTS", self.t0 + timedelta(seconds=600 * i)) for i in range(3)]
        recs = group_clips({"cam1": clips})
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(recs[0].clips), 3)
        self.assertEqual(recs[0].duration_s, 1800)
        self.assertEqual(recs[0].id, "cam1-20260614-204500")

    def test_gap_starts_new_recording(self):
        clips = [clip("00000.MTS", self.t0),
                 clip("00001.MTS", self.t0 + timedelta(seconds=600 + 3)),      # within 5 s
                 clip("00002.MTS", self.t0 + timedelta(minutes=45))]           # new watch
        recs = group_clips({"cam1": clips})
        self.assertEqual([len(r.clips) for r in recs], [2, 1])

    def test_format_change_starts_new_recording(self):
        clips = [clip("00000.MTS", self.t0),
                 clip("00001.MTS", self.t0 + timedelta(seconds=600), w=1280, h=720)]
        self.assertEqual(len(group_clips({"cam1": clips})), 2)

    def test_order_follows_time_not_filename(self):
        clips = [clip("00001.MTS", self.t0 + timedelta(seconds=600)), clip("00000.MTS", self.t0)]
        rec = group_clips({"cam1": clips})[0]
        self.assertEqual([Path(c.path).name for c in rec.clips], ["00000.MTS", "00001.MTS"])

    def test_cards_never_merge_and_ids_stay_unique(self):
        recs = group_clips({"a b": [clip("x.MTS", self.t0)], "a-b": [clip("y.MTS", self.t0)]})
        self.assertEqual(len({r.id for r in recs}), 2)

    def test_mixed_timezone_awareness_does_not_crash(self):
        aware = clip("00001.MTS", (self.t0 + timedelta(seconds=600)).astimezone(timezone.utc))
        self.assertTrue(group_clips({"cam1": [clip("00000.MTS", self.t0), aware]}))

    def test_card_root_for_avchd(self):
        root = Path("/share/survey")
        f = root / "2026-06-14" / "CAM1" / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM" / "00000.MTS"
        self.assertEqual(card_root(f, root), root / "2026-06-14" / "CAM1")
        self.assertEqual(card_root(root / "loose" / "clip.mp4", root), root / "loose")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class RealClips(unittest.TestCase):
    """Two 2-second interlaced H.264 clips in an AVCHD folder, like a Sony card."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        stream = cls.tmp / "CAM1" / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM"
        stream.mkdir(parents=True)
        cls.t_end0 = time.time() - 3600
        for i in range(2):
            out = stream / f"{i:05d}.MTS"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc2=size=320x180:rate=25", "-t", "2",
                 "-c:v", "libx264", "-flags", "+ildct+ilme", "-x264opts", "tff=1",
                 "-pix_fmt", "yuv420p", "-f", "mpegts", str(out)], check=True)
            end = cls.t_end0 + 2 * i
            os.utime(out, (end, end))                   # camera closes file at clip end
        (stream / "._00000.MTS").write_bytes(b"\x00" * 10)   # macOS junk must be skipped

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_scan_groups_and_flags_mtime_times(self):
        m = scan([self.tmp], use_exiftool=False)
        self.assertEqual(m["summary"]["files"], 2)
        self.assertEqual(m["summary"]["recordings"], 1)
        rec = m["recordings"][0]
        self.assertEqual(rec["card"], "CAM1")
        self.assertEqual(rec["clip_count"], 2)
        self.assertEqual((rec["width"], rec["height"], rec["fps"]), (320, 180, 25.0))
        self.assertTrue(rec["interlaced"])
        self.assertEqual(rec["start_source"], "file-mtime")
        self.assertTrue(any("modified time" in w for w in rec["warnings"]))
        self.assertEqual(m["summary"]["needs_time_check"], 1)
        start = datetime.fromisoformat(rec["start"]).timestamp()
        self.assertAlmostEqual(start, self.t_end0 - 2, delta=1.5)

    def test_cli_writes_manifest(self):
        out = self.tmp / "out" / "manifest.json"
        code = main(["scan", str(self.tmp), "--out", str(out), "--no-exiftool", "--quiet"])
        self.assertEqual(code, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "emergence-kit/manifest@1")

    def test_missing_ffprobe_is_one_clear_error(self):
        from emergence_kit import probe
        old_path, old_cands = os.environ.get("PATH", ""), probe._windows_candidates
        old_env = os.environ.pop("EMERGENCE_FFPROBE", None)
        os.environ["PATH"] = ""
        probe._windows_candidates = lambda name: []     # hide winget/choco installs too
        try:
            self.assertEqual(main(["scan", str(self.tmp), "--quiet"]), 3)
        finally:
            os.environ["PATH"] = old_path
            probe._windows_candidates = old_cands
            if old_env is not None:
                os.environ["EMERGENCE_FFPROBE"] = old_env

    def test_cli_missing_path(self):
        self.assertEqual(main(["scan", str(self.tmp / "nope"), "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main()
