"""Check-out / check-in of surveys on a network share (here: two local folders)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from emergence_kit import audit, checkout as co

LOG = "clock_time,event,species_group,count,direction,observer,notes\n"


def make_survey(root: Path) -> Path:
    s = root / "share" / "Barn A dusk"
    (s / "r1").mkdir(parents=True)
    (s / "originals" / "CARD1").mkdir(parents=True)
    (s / "originals" / "CARD1" / "00000.MTS").write_bytes(b"\0" * 50_000)
    (s / "prep_report.json").write_text(json.dumps({"recordings": [{"id": "r1"}]}))
    (s / "r1" / "r1_review.mp4").write_bytes(b"\1" * 20_000)
    (s / "r1" / "review_log_r1.csv").write_text(LOG)
    audit.record(s, "prep", {"test": True})
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="checkout-"))
        self._user = os.environ.get("EMERGENCE_USER")
        os.environ["EMERGENCE_USER"] = "lb@example.org"
        self.share = make_survey(self.tmp)
        self.laptop = self.tmp / "laptop"

    def tearDown(self):
        if self._user is None:
            os.environ.pop("EMERGENCE_USER", None)
        else:
            os.environ["EMERGENCE_USER"] = self._user
        shutil.rmtree(self.tmp, ignore_errors=True)


class CheckoutCase(Base):
    def test_round_trip(self):
        local = co.checkout(self.share, self.laptop)
        self.assertTrue((local / "r1" / "r1_review.mp4").exists())
        self.assertFalse((local / "originals").exists())          # originals stay put
        self.assertEqual(co.status(local)["state"], "local-copy")
        self.assertEqual(co.status(self.share)["state"], "checked-out")
        self.assertIn("lb@example.org", co.writable(self.share))
        self.assertIsNone(co.writable(local))
        with self.assertRaises(co.CheckoutError):
            co.checkout(self.share, self.laptop)                    # one at a time
        (local / "r1" / "review_log_r1.csv").write_text(LOG + "21:14:32,emergence,,1,,LB,\n")
        audit.record(local, "review.signoff", {"recording": "r1"})
        res = co.checkin(local)
        self.assertIn("r1/review_log_r1.csv", res["files"])
        self.assertIn("audit.jsonl", res["files"])
        self.assertIn("21:14:32", (self.share / "r1" / "review_log_r1.csv").read_text())
        self.assertEqual(co.status(self.share)["state"], "free")
        self.assertEqual(audit.verify(self.share)[0], [])
        actions = [e["action"] for e in audit.read_entries(self.share)]
        self.assertEqual(actions[-3:], ["checkout", "review.signoff", "checkin"])
        self.assertEqual(co.status(local)["state"], "free")        # local copy retired

    def test_refuses_when_share_changed_meanwhile(self):
        local = co.checkout(self.share, self.laptop)
        (self.share / "r1" / "review_log_r1.csv").write_text(LOG + "21:00:00,pass,,1,,XX,\n")
        (local / "r1" / "review_log_r1.csv").write_text(LOG + "21:14:32,emergence,,1,,LB,\n")
        with self.assertRaises(co.CheckoutError) as ctx:
            co.checkin(local)
        self.assertIn("review_log_r1.csv", str(ctx.exception))
        self.assertIn("21:00:00", (self.share / "r1" / "review_log_r1.csv").read_text())
        self.assertEqual(co.status(self.share)["state"], "checked-out")

    def test_release_is_recorded_and_blocks_stale_checkin(self):
        local = co.checkout(self.share, self.laptop)
        with self.assertRaises(co.CheckoutError):
            co.release(self.share, "")
        co.release(self.share, "laptop lost on site")
        self.assertEqual(audit.read_entries(self.share)[-1]["action"], "checkout.release")
        with self.assertRaises(co.CheckoutError) as ctx:
            co.checkin(local)
        self.assertIn("released", str(ctx.exception))

    def test_simultaneous_checkouts_one_winner(self):
        wins, errors = [], []
        barrier = threading.Barrier(6)

        def go(i):
            barrier.wait()
            try:
                wins.append(co.checkout(self.share, self.tmp / f"laptop{i}"))
            except co.CheckoutError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(wins), len(errors)), (1, 5))


class StudioReadOnly(Base):
    def test_checked_out_share_is_read_only_in_the_app(self):
        from test_studio import Client
        from emergence_kit.studio.server import Studio, make_server
        os.environ["SURVEY_STUDIO_HOME"] = str(self.tmp / "home")
        try:
            st = Studio(token="t")
            httpd = make_server(st)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            c = Client(st.port, "t")
            key = c.post("/api/surveys/open", {"path": str(self.share)})[1]["key"]
            co.checkout(self.share, self.laptop)
            sv = c.get(f"/api/s/{key}")[1]
            self.assertEqual(sv["checkout"]["state"], "checked-out")
            self.assertTrue(sv["read_only"])
            status, body, _ = c.req("PUT", f"/api/s/{key}/r/r1/log?kind=review", {"rows": []})
            self.assertEqual(status, 423)
            self.assertEqual(c.post(f"/api/s/{key}/r/r1/signoff")[0], 423)
            httpd.shutdown()
            httpd.server_close()
        finally:
            os.environ.pop("SURVEY_STUDIO_HOME", None)


if __name__ == "__main__":
    unittest.main()
