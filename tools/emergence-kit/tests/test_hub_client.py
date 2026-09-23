"""hub_client without a hub: URL rules, error messages, per-user lock store, folder link.
(End-to-end tests against a real running hub are in the repository's tests/test_hub.py.)"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

from emergence_kit import hub_client as H


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class HubClient(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["EMERGENCE_HOME"] = str(self.tmp / "home")

    def tearDown(self):
        os.environ.pop("EMERGENCE_HOME", None)

    def test_plain_http_refused_except_localhost(self):
        with self.assertRaises(H.HubError):
            H.Client("http://hub.example.org")
        H.Client("http://127.0.0.1:8100")
        H.Client("https://hub.example.org/")

    def test_request_sends_bearer_and_parses_errors(self):
        seen = []

        def ok(req):
            seen.append(req)
            return FakeResp(b'{"id": "s1"}')
        c = H.Client("https://hub.example.org", token="tok", opener=ok)
        self.assertEqual(c.create_survey("s1"), {"id": "s1"})
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(seen[0].data), {"id": "s1", "name": ""})

        def conflict(req):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                         io.BytesIO(b'{"detail": "locked by bob", "holder": "bob"}'))
        c = H.Client("https://hub.example.org", token="tok", opener=conflict)
        with self.assertRaises(H.HubError) as ctx:
            c.lock("s1", "r1", "review")
        self.assertEqual((ctx.exception.status, ctx.exception.body["holder"]), (409, "bob"))
        self.assertIn("locked by bob", str(ctx.exception))

        def down(req):
            raise urllib.error.URLError("connection refused")
        with self.assertRaises(H.HubError) as ctx:
            H.Client("https://hub.example.org", token="t", opener=down).jobs("s1")
        self.assertIn("not reachable", str(ctx.exception))

    def test_lock_tokens_stay_out_of_the_shared_folder(self):
        folder = self.tmp / "review"
        folder.mkdir()
        H.write_link(folder, "https://hub.example.org", "s1")
        H.save_lock("https://hub.example.org", "s1", "r1",
                    {"token": "secret", "kind": "review", "expires_at": "x"})
        self.assertNotIn("secret", (folder / "hub.json").read_text())
        self.assertEqual(H.load_lock("https://hub.example.org/", "s1", "r1")["token"], "secret")
        H.forget_lock("https://hub.example.org", "s1", "r1")
        self.assertIsNone(H.load_lock("https://hub.example.org", "s1", "r1"))

    def test_unlinked_folder_is_left_alone(self):
        self.assertIsNone(H.client_for(self.tmp))
        self.assertIsNone(H.sync_anchors(self.tmp, say=self.fail))


if __name__ == "__main__":
    unittest.main()
