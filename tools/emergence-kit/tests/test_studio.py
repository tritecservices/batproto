"""Survey Studio's local server: security checks, survey import, log editing, video
seeking, sign-off/QA rules and reports - over real HTTP, as the app window uses it."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "devtools"))

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class Client:
    def __init__(self, port: int, cookie: str | None):
        self.port, self.cookie = port, cookie

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = f"studio={self.cookie}"
        if method != "GET":
            h["X-Studio"] = "1"
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            parsed = json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data
        except ValueError:
            parsed = data
        return r.status, parsed, r

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body if body is not None else {}, **kw)

    def wait(self, tid, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            _, t, _ = self.get(f"/api/tasks/{tid}")
            if t["state"] != "running":
                return t
            time.sleep(0.3)
        raise AssertionError("task did not finish")


class StudioCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emergence_kit.studio.server import Studio, make_server
        cls.tmp = Path(tempfile.mkdtemp(prefix="studio-"))
        cls._env = {k: os.environ.get(k) for k in ("SURVEY_STUDIO_HOME", "EMERGENCE_USER")}
        os.environ["SURVEY_STUDIO_HOME"] = str(cls.tmp / "home")
        os.environ["EMERGENCE_USER"] = "reviewer@example.org"
        cls.studio = Studio(token="test-token-123")
        cls.httpd = make_server(cls.studio)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.studio.port
        cls.c = Client(cls.port, "test-token-123")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)


class Security(StudioCase):
    def test_needs_the_window_cookie(self):
        anon = Client(self.port, None)
        self.assertEqual(anon.get("/api/state")[0], 403)
        self.assertEqual(anon.get("/")[0], 403)
        self.assertEqual(Client(self.port, "wrong").get("/api/state")[0], 403)

    def test_first_load_swaps_url_token_for_cookie(self):
        anon = Client(self.port, None)
        status, _, r = anon.get("/?t=test-token-123")
        self.assertEqual(status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(anon.get("/?t=nope")[0], 403)

    def test_dns_rebinding_and_forged_posts_blocked(self):
        self.assertEqual(self.c.get("/api/state", host="evil.example:80")[0], 403)
        status, body, _ = self.c.req("POST", "/api/ping", {}, headers={"X-Studio": ""})
        self.assertEqual(status, 403)

    def test_app_page_has_strict_csp(self):
        status, _, r = self.c.get("/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))

    def test_static_traversal_refused(self):
        self.assertEqual(self.c.get("/static/../server.py")[0], 404)
        self.assertEqual(self.c.get("/static/%2e%2e/server.py")[0], 404)

    def test_unopened_survey_is_unknown(self):
        self.assertEqual(self.c.get("/api/s/0123456789abcdef")[0], 404)
        self.assertEqual(self.c.get("/media/0123456789abcdef/x")[0], 404)


class Settings(StudioCase):
    def test_state_and_settings(self):
        status, st, _ = self.c.get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("Pipistrellus pygmaeus", st["species"])
        self.assertEqual(st["user"]["user"], "reviewer@example.org")
        _, s, _ = self.c.post("/api/settings", {"observer": "lb", "skip_seconds": 10, "bogus": 1})
        self.assertEqual((s["observer"], s["skip_seconds"]), ("LB", 10))
        self.assertNotIn("bogus", s)
        self.assertEqual(self.c.get("/api/state")[1]["settings"]["observer"], "LB")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class Workflow(StudioCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import make_fake_card
        cls.cards = cls.tmp / "cards"
        make_fake_card.main([str(cls.cards), "--small", "--format", "xavc", "--cards", "1",
                             "--clips", "2", "--clip-seconds", "3"])
        status, r, _ = cls.c.post("/api/import", {
            "name": "Barn A / dusk <14 June>", "cards": [str(cls.cards)],
            "dest": str(cls.tmp / "surveys")})
        assert status == 200, r
        t = cls.c.wait(r["task"])
        assert t["state"] == "done", t
        cls.key = t["result"]["key"]
        cls.sv = cls.c.get(f"/api/s/{cls.key}")[1]
        cls.rid = cls.sv["recordings"][0]["id"]

    def test_import_made_a_survey_with_a_safe_folder_name(self):
        self.assertEqual(self.sv["name"], "Barn A / dusk <14 June>")
        self.assertEqual(Path(self.sv["folder"]).name, "Barn A  dusk 14 June")
        self.assertEqual(len(self.sv["recordings"]), 1)
        self.assertTrue(self.sv["recordings"][0]["has_video"])
        self.assertTrue(self.sv["audit"]["ok"])
        _, st, _ = self.c.get("/api/state")
        self.assertEqual(st["recent"][0]["name"], "Barn A / dusk <14 June>")
        # importing into the same place again is refused, not overwritten
        status, _, _ = self.c.post("/api/import", {"name": "Barn A / dusk <14 June>",
                                                   "cards": [str(self.cards)],
                                                   "dest": str(self.tmp / "surveys")})
        self.assertEqual(status, 409)

    def test_video_supports_range_requests(self):
        path = f"/media/{self.key}/{self.rid}"
        status, body, r = self.c.get(path, headers={"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)
        self.assertTrue(r.getheader("Content-Range").startswith("bytes 100-199/"))
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(self.c.get(f"/media/{self.key}/..%2Fx")[0], 404)

    def test_log_validation_and_atomic_save(self):
        url = f"/api/s/{self.key}/r/{self.rid}/log?kind=review"
        bad = [{"clock_time": "25:00:00", "event": "emergence"}]
        self.assertEqual(self.c.req("PUT", url, {"rows": bad})[0], 422)
        bad = [{"clock_time": "21:05:01", "event": "flying"}]
        self.assertEqual(self.c.req("PUT", url, {"rows": bad})[0], 422)
        rows = [{"clock_time": "9:05:01", "event": "Emergence", "count": "2",
                 "species_group": "Pipistrellus pygmaeus", "observer": "LB",
                 "notes": "line one\nline two"}]
        status, saved, _ = self.c.req("PUT", url, {"rows": rows})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["rows"][0]["clock_time"], "09:05:01")
        self.assertEqual(saved["rows"][0]["event"], "emergence")
        self.assertNotIn("\n", saved["rows"][0]["notes"])
        self.assertEqual(self.c.get(url)[1]["rows"], saved["rows"])
        folder = Path(self.sv["folder"]) / self.rid
        self.assertEqual([p.name for p in folder.glob(".*tmp")], [])

    def test_signoff_then_qa_by_someone_else(self):
        base = f"/api/s/{self.key}/r/{self.rid}"
        self.c.req("PUT", base + "/log?kind=review", {"rows": [
            {"clock_time": "21:05:01", "event": "emergence", "count": 1}]})
        # QA before sign-off is refused
        self.assertEqual(self.c.get(base + "/log?kind=qa")[0], 409)
        status, r, _ = self.c.post(base + "/signoff", {"note": "done"})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["by"], "reviewer@example.org")
        # the reviewer can't see or write the QA log
        self.assertEqual(self.c.get(base + "/log?kind=qa")[0], 403)
        self.assertEqual(self.c.req("PUT", base + "/log?kind=qa", {"rows": []})[0], 403)
        os.environ["EMERGENCE_USER"] = "checker@example.org"
        try:
            self.assertEqual(self.c.get(base + "/log?kind=qa")[0], 200)
            self.c.req("PUT", base + "/log?kind=qa", {"rows": [
                {"clock_time": "21:05:02", "event": "emergence", "count": 1}]})
            status, r, _ = self.c.post(base + "/qa", {"decision": "approve"})
            self.assertEqual(status, 200, r)
            self.assertEqual(r["comparison"]["matched"], 1)
        finally:
            os.environ["EMERGENCE_USER"] = "reviewer@example.org"
        rec = self.c.get(f"/api/s/{self.key}")[1]["recordings"][0]
        self.assertEqual((rec["qa"], rec["qa_by"]), ("approved", "checker@example.org"))
        # not linked to a hub: locking is a no-op
        self.assertEqual(self.c.post(base + "/lock", {"kind": "review"})[1], {"hub": False})

    def test_report_never_contains_the_location(self):
        status, _, _ = self.c.post(f"/api/s/{self.key}/report", {"site": "Barn", "lat": "91", "lon": "0"})
        self.assertEqual(status, 400)
        status, r, _ = self.c.post(f"/api/s/{self.key}/report",
                                   {"site": "Barn A", "lat": "51.4545", "lon": "-2.5879"})
        self.assertEqual(status, 200, r)
        t = self.c.wait(r["task"])
        self.assertEqual(t["state"], "done", t)
        status, html, resp = self.c.get(t["result"]["url"])
        self.assertEqual(status, 200)
        self.assertIn("style-src 'unsafe-inline'", resp.getheader("Content-Security-Policy"))
        for out in ("report.html", "summary.csv", "events.csv", "summary.json"):
            text = (Path(self.sv["folder"]) / out).read_text(encoding="utf-8")
            self.assertNotIn("51.45", text)
            self.assertNotIn("2.587", text)
        self.assertEqual(self.c.get(f"/files/{self.key}/audit.jsonl")[0], 404)

    def test_detect_runs_in_the_background(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        status, r, _ = self.c.post(f"/api/s/{self.key}/detect")
        self.assertEqual(status, 200)
        t = self.c.wait(r["task"])
        self.assertEqual(t["state"], "done", t)
        _, d, _ = self.c.get(f"/api/s/{self.key}/r/{self.rid}/detections")
        self.assertIsInstance(d["rows"], list)


if __name__ == "__main__":
    unittest.main()
