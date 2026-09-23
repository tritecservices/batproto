"""Acoustic engine and Survey Studio's acoustic workspace, on synthetic detector files."""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "devtools"))

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_fake_bat_wavs
        cls.tmp = Path(tempfile.mkdtemp(prefix="bats-"))
        make_fake_bat_wavs.main([str(cls.tmp), "--seconds", "1.5"])
        from emergence_kit import acoustic as A
        cls.A = A
        cls.files = A.list_folder(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rec(self, species_word):
        for f in self.files:
            r = self.A.open_recording(f)
            if species_word in (r.guano.get("Species Auto ID") or ""):
                return r
        raise AssertionError(species_word)

    def test_reads_guano_without_exposing_location(self):
        r = self.rec("pipistrellus")
        s = r.summary()
        self.assertEqual((r.sample_rate, r.bits), (384_000, 16))
        self.assertEqual(s["auto_id"], "Pipistrellus pipistrellus")
        self.assertTrue(s["has_location"])
        self.assertNotIn("51.45", repr(s))

    def test_call_measurements_match_the_species(self):
        pip = self.A.find_calls(self.rec("pipistrellus"))
        noc = self.A.find_calls(self.rec("noctula"))
        self.assertGreater(len(pip), 5)
        self.assertTrue(all(43 <= c["f_end_khz"] <= 49 for c in pip))
        self.assertTrue(all(17 <= c["f_end_khz"] <= 22 for c in noc))
        noise = [f for f in self.files if not self.A.open_recording(f).guano.get("Species Auto ID")]
        self.assertEqual(self.A.find_calls(self.A.open_recording(noise[0])), [])

    def test_24_bit_and_extensible_wav(self):
        x = (np.sin(np.arange(38400) / 3) * 0.5 * 8388607).astype(np.int32)
        pcm = b"".join(struct.pack("<i", v)[:3] for v in x)
        fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 384000, 384000 * 3, 3, 24) + struct.pack(
            "<HHI", 22, 24, 4) + struct.pack("<H", 1) + b"\x00" * 14
        body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
        p = self.tmp / "ext24.wav"
        p.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
        r = self.A.open_recording(p)
        self.assertEqual((r.bits, r.frames), (24, 38400))
        y = self.A.read_samples(r)
        self.assertAlmostEqual(float(np.abs(y).max()), 0.5, places=2)
        p.unlink()

    def test_spectrogram_png_and_time_expansion(self):
        r = self.rec("pygmaeus")
        png, meta = self.A.spectrogram_png(r, fmax_hz=125_000, height=200)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(meta["height"], 200)
        wav = self.A.time_expanded_wav(r, factor=10)
        self.assertEqual(struct.unpack("<I", wav[24:28])[0], 38_400)

    def test_not_a_wav(self):
        p = self.tmp / "bad.wav"
        p.write_bytes(b"hello")
        with self.assertRaises(self.A.WavError):
            self.A.open_recording(p)
        p.unlink()


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class StudioAcoustic(unittest.TestCase):
    def test_open_label_and_media(self):
        import make_fake_bat_wavs
        from test_studio import Client
        from emergence_kit.studio.server import Studio, make_server
        tmp = Path(tempfile.mkdtemp(prefix="studio-ac-"))
        old = {k: os.environ.get(k) for k in ("SURVEY_STUDIO_HOME", "EMERGENCE_USER")}
        os.environ.update(SURVEY_STUDIO_HOME=str(tmp / "home"), EMERGENCE_USER="lb@example.org")
        try:
            make_fake_bat_wavs.main([str(tmp / "bats"), "--seconds", "1"])
            st = Studio(token="t")
            httpd = make_server(st)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            c = Client(st.port, "t")
            status, v, _ = c.post("/api/acoustic/open", {"path": str(tmp / "bats")})
            self.assertEqual(status, 200, v)
            self.assertEqual(len(v["files"]), 8)
            key = v["key"]
            status, png, r = c.get(f"/acoustic/{key}/0/spec.png?fmax=125000&start=0.2&dur=0.3")
            self.assertEqual((status, r.getheader("Content-Type")), (200, "image/png"))
            status, wav, r = c.get(f"/acoustic/{key}/0/te.wav?x=10")
            self.assertEqual((status, r.getheader("Content-Type")), (200, "audio/wav"))
            status, calls, _ = c.get(f"/api/a/{key}/f/0/calls")
            self.assertGreater(len(calls["calls"]), 3)
            status, _, _ = c.req("PUT", f"/api/a/{key}/f/0/label", {"manual_id": "Pipistrellus pipistrellus"})
            self.assertEqual(status, 200)
            labels = (tmp / "bats" / "survey_studio_labels.csv").read_text()
            self.assertIn("Pipistrellus pipistrellus", labels)
            self.assertNotIn("51.45", labels)
            self.assertEqual(c.get(f"/api/a/{key}")[1]["files"][0]["manual_id"], "Pipistrellus pipistrellus")
            from emergence_kit import audit
            self.assertEqual(audit.verify(tmp / "bats")[0], [])
            self.assertEqual(c.get(f"/acoustic/{key}/99/spec.png")[0], 404)
            self.assertEqual(c.get("/acoustic/a000000000000000/0/spec.png")[0], 404)
            httpd.shutdown()
            httpd.server_close()
        finally:
            for k, val in old.items():
                os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
