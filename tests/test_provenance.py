"""The clean-room gate itself: it must catch what it claims to catch."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_provenance as cp          # noqa: E402

FAKE_ID = "1" * 18 + "/" + "2" * 18


class Provenance(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "provenance.yaml").write_text(
            "repo_data_allowlist:\n  - ok.json\n  - provenance.yaml\n", encoding="utf-8")
        (self.root / "ok.json").write_text("{}", encoding="utf-8")
        (self.root / "code.py").write_text("key = os.environ['BRAIN_API_KEY']\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_clean_tree_passes(self):
        self.assertEqual(cp.check(self.root), [])

    def test_unlisted_data_file_fails(self):
        (self.root / "survey.csv").write_text("a,b\n", encoding="utf-8")
        self.assertTrue(any("survey.csv" in p for p in cp.check(self.root)))

    def test_discord_link_fails(self):
        (self.root / "notes.md").write_text(f"see https://discord.com/channels/{FAKE_ID}\n",
                                            encoding="utf-8")
        self.assertTrue(any("Discord" in p for p in cp.check(self.root)))

    def test_real_looking_secret_fails_placeholder_passes(self):
        (self.root / "a.env").write_text("API_KEY=change-me\n", encoding="utf-8")
        self.assertEqual(cp.check(self.root), [])
        (self.root / "b.env").write_text("API_KEY=Zx9Qm2Lp7Rt4Vw8Yb3Nc6Hd1Kf5Jg0Ts\n", encoding="utf-8")
        self.assertTrue(any("credential" in p for p in cp.check(self.root)))

    def test_big_file_fails(self):
        (self.root / "blob.bin").write_bytes(b"\0" * (cp.MAX_BYTES + 1))
        self.assertTrue(any("5 MB" in p for p in cp.check(self.root)))


if __name__ == "__main__":
    unittest.main()
