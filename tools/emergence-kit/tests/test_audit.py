"""Audit trail, reviewer sign-off and second-reviewer QA (no ffmpeg needed)."""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emergence_kit import audit                                    # noqa: E402
from emergence_kit.cli import main                                 # noqa: E402
from emergence_kit.prep import REVIEW_COLUMNS                      # noqa: E402
from emergence_kit.qa import QAError, compare, qa, signoff, status  # noqa: E402
from emergence_kit.report import run as report_run                 # noqa: E402


@contextmanager
def as_user(name):
    old = os.environ.get("EMERGENCE_USER")
    os.environ["EMERGENCE_USER"] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("EMERGENCE_USER", None)
        else:
            os.environ["EMERGENCE_USER"] = old


def write_log(path: Path, rows: list[tuple]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        for t, ev, n in rows:
            w.writerow({**dict.fromkeys(REVIEW_COLUMNS, ""), "clock_time": t, "event": ev,
                        "count": n, "species_group": "Pipistrellus sp."})


class Chain(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        with as_user("alice@example.org"):
            for i in range(4):
                audit.record(self.d, "test", {"i": i})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def lines(self):
        return (self.d / audit.AUDIT_FILE).read_text(encoding="utf-8").splitlines()

    def put(self, lines):
        (self.d / audit.AUDIT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_intact_chain_verifies(self):
        problems, last = audit.verify(self.d)
        self.assertEqual(problems, [])
        self.assertEqual((last["seq"], last["actor"]["user"]), (4, "alice@example.org"))

    def test_edited_entry_detected(self):
        ls = self.lines()
        e = json.loads(ls[1])
        e["details"]["i"] = 99
        ls[1] = json.dumps(e, sort_keys=True)
        self.put(ls)
        self.assertIn("entry 2", audit.verify(self.d)[0][0])

    def test_deleted_middle_entry_detected(self):
        ls = self.lines()
        del ls[2]
        self.put(ls)
        self.assertTrue(audit.verify(self.d)[0])

    def test_truncated_tail_detected_by_head(self):
        self.put(self.lines()[:-1])
        self.assertIn("audit.head", audit.verify(self.d)[0][0])

    def test_broken_chain_is_never_extended(self):
        ls = self.lines()
        e = json.loads(ls[0])
        e["actor"]["user"] = "mallory"
        ls[0] = json.dumps(e, sort_keys=True)
        self.put(ls)
        with self.assertRaises(RuntimeError):
            audit.record(self.d, "cover-up", {})

    def test_export(self):
        out = self.d / "export.csv"
        self.assertEqual(audit.export_csv(self.d, out), 4)
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        self.assertEqual(rows[0]["user"], "alice@example.org")


class Compare(unittest.TestCase):
    def test_counts_and_agreement(self):
        a = [{"t": 100, "event": "emergence", "count": 1}, {"t": 200, "event": "pass", "count": 1},
             {"t": 300, "event": "emergence", "count": 2}]
        b = [{"t": 101, "event": "emergence", "count": 1}, {"t": 305, "event": "emergence", "count": 1},
             {"t": 400, "event": "pass", "count": 1}]
        c = compare(a, b, tol=2)
        self.assertEqual((c["matched"], c["only_primary"], c["only_qa"]), (1, 2, 2))
        self.assertEqual(c["agreement"], round(2 / 6, 3))
        self.assertEqual(c["totals"]["emergence"], {"primary": 3, "qa": 2, "difference": -1})

    def test_midnight_crossing_matches(self):
        a = [{"t": 23 * 3600 + 59 * 60 + 59, "event": "pass", "count": 1},
             {"t": 1, "event": "pass", "count": 1}]
        b = [{"t": 23 * 3600 + 59 * 60 + 58, "event": "pass", "count": 1},
             {"t": 2, "event": "pass", "count": 1}]
        self.assertEqual(compare(a, b)["matched"], 2)


class Workflow(unittest.TestCase):
    """Reviewer signs off -> a different person QAs -> report shows both."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.rid = "cam1-20260614-210500"
        (self.d / "prep_report.json").write_text(json.dumps({"recordings": [{
            "id": self.rid, "card": "CAM1", "warnings": [],
            "recording": {"start": "2026-06-14T21:05:00+01:00", "duration_s": 3600,
                          "start_source": "sony-xml"}}]}), encoding="utf-8")
        self.review = [("21:30:00", "emergence", 1), ("21:31:10", "emergence", 2),
                       ("21:40:00", "pass", 1)]
        write_log(self.d / self.rid / f"review_log_{self.rid}.csv", self.review)
        write_log(self.d / self.rid / f"qa_log_{self.rid}.csv",
                  [("21:30:01", "emergence", 1), ("21:31:10", "emergence", 2)])

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_full_workflow(self):
        with as_user("alice@example.org"):
            signoff(self.d, self.rid)
            with self.assertRaises(QAError):                         # can't QA yourself
                qa(self.d, self.rid, "approve")
        with as_user("bob@example.org"):
            e = qa(self.d, self.rid, "approve", "spot checked")
        self.assertEqual(e["details"]["comparison"]["matched"], 2)
        self.assertEqual(e["details"]["reviewer"], "alice@example.org")
        st = status(self.d, self.rid)
        self.assertEqual((st["signed_off_by"], st["qa"], st["qa_by"]),
                         ("alice@example.org", "approved", "bob@example.org"))
        with as_user("alice@example.org"):
            summary = report_run(self.d, 51.5, -0.12, "Test")
        rec = summary["recordings"][0]
        self.assertEqual((rec["signed_off_by"], rec["qa"]), ("alice@example.org", "approved"))
        self.assertTrue(summary["audit"]["verified"])
        self.assertTrue(any("EMERGENCE_USER" in i["message"] for i in summary["issues"]))
        self.assertEqual(audit.read_entries(self.d)[-1]["action"], "report")

    def test_edit_after_signoff_is_caught(self):
        with as_user("alice@example.org"):
            signoff(self.d, self.rid)
        write_log(self.d / self.rid / f"review_log_{self.rid}.csv", self.review[:1])
        self.assertTrue(status(self.d, self.rid)["changed_after_signoff"])
        with as_user("bob@example.org"), self.assertRaises(QAError):
            qa(self.d, self.rid, "approve")
        with as_user("alice@example.org"):
            s = report_run(self.d, 51.5, -0.12, "Test")
        self.assertTrue(any("changed after" in i["message"] for i in s["issues"]))

    def test_tampered_audit_reported_not_extended(self):
        with as_user("alice@example.org"):
            signoff(self.d, self.rid)
        p = self.d / audit.AUDIT_FILE
        p.write_text(p.read_text(encoding="utf-8").replace("alice", "carol"), encoding="utf-8")
        before = len(audit.read_entries(self.d))
        s = report_run(self.d, 51.5, -0.12, "Test")
        self.assertFalse(s["audit"]["verified"])
        self.assertEqual(len(audit.read_entries(self.d)), before)    # nothing appended

    def test_cli(self):
        with as_user("alice@example.org"):
            self.assertEqual(main(["signoff", str(self.d), self.rid]), 0)
        with as_user("bob@example.org"):
            self.assertEqual(main(["qa", str(self.d), self.rid, "--decision", "reject",
                                   "--note", "missed a pass at 21:40"]), 0)
        self.assertEqual(main(["audit", "verify", str(self.d)]), 0)
        self.assertEqual(status(self.d, self.rid)["qa"], "rejected")


if __name__ == "__main__":
    unittest.main()
