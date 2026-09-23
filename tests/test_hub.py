"""Survey hub: locks, separation of duties, tenant isolation, job queue, audit anchors,
backup/restore, the HTTP API and a worker running real jobs.

Runs on SQLite always. Set HUB_TEST_PG_URL to a PostgreSQL database that may be wiped
(e.g. postgresql://hub@127.0.0.1:5432/hubtest) to run everything on PostgreSQL too,
including the concurrency tests; CI does this with a postgres service container.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tools" / "emergence-kit"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "devtools"))

from brain.identity import Principal                                   # noqa: E402
from hub import backup, queue, service                                 # noqa: E402
from hub.db import Database, ts, utcnow                                # noqa: E402
from hub.schema import LATEST, current, migrate                        # noqa: E402
from hub.service import HubError                                       # noqa: E402

PG_URL = os.environ.get("HUB_TEST_PG_URL")
T1, T2 = "tenant-one", "tenant-two"


def person(sub, roles, tenant=T1):
    return Principal("user", tenant, sub, f"{sub}@example.org", frozenset(roles))


ALICE = person("alice", {"Survey.Review"})
BOB = person("bob", {"Survey.Review"})
CAROL = person("carol", {"Survey.QA"})
DAVE = person("dave", {"Survey.Review", "Survey.QA"})
ADMIN = person("admin", {"Platform.Admin"})
READER = person("rita", {"Brain.Read"})
EVE = person("eve", {"Survey.Review", "Survey.QA", "Platform.Admin"}, tenant=T2)
SHA_A, SHA_B = "a" * 64, "b" * 64


def fresh_db(kind: str, tmp: Path) -> Database:
    if kind == "sqlite":
        db = Database.connect(f"sqlite:///{tmp / 'hub.db'}")
    else:
        db = Database.connect(PG_URL)
        with db.tx() as c:
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
    migrate(db)
    return db


class HubCase(unittest.TestCase):
    kind = "sqlite"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hubtest-"))
        self._env = {k: os.environ.get(k) for k in ("BRAIN_AUDIT_DIR", "HUB_STORAGE_ROOT")}
        os.environ["BRAIN_AUDIT_DIR"] = str(self.tmp / "audit")
        self.db = fresh_db(self.kind, self.tmp)
        service.create_survey(self.db, ALICE, "s1", "Barn A dusk")
        service.register_recordings(self.db, ALICE, "s1", ["r1", "r2"])

    def tearDown(self):
        self.db.close()
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, fn, *a, **kw) -> int:
        with self.assertRaises(HubError) as ctx:
            fn(*a, **kw)
        return ctx.exception.status

    def expire_locks(self):
        with self.db.tx() as c:
            c.execute("UPDATE locks SET expires_at = ?", (ts(utcnow() - timedelta(seconds=1)),))


class Schema(HubCase):
    def test_migrate_is_idempotent_and_versioned(self):
        self.assertEqual(migrate(self.db), [])
        self.assertEqual(current(self.db), LATEST)


class Tenancy(HubCase):
    def test_other_tenant_sees_nothing(self):
        self.assertEqual(service.list_surveys(self.db, EVE), [])
        self.assertEqual(self.status(service.get_survey, self.db, EVE, "s1"), 404)
        self.assertEqual(self.status(service.acquire_lock, self.db, EVE, "s1", "r1", "review"), 404)
        self.assertEqual(self.status(service.enqueue, self.db, EVE, "s1", "scan", {}), 404)

    def test_same_survey_id_in_two_tenants_is_two_surveys(self):
        service.create_survey(self.db, EVE, "s1", "Their s1")
        self.assertEqual(service.get_survey(self.db, EVE, "s1")["name"], "Their s1")
        self.assertEqual(service.get_survey(self.db, ALICE, "s1")["name"], "Barn A dusk")

    def test_roles_required(self):
        self.assertEqual(self.status(service.create_survey, self.db, READER, "x", ""), 403)
        self.assertEqual(self.status(service.acquire_lock, self.db, CAROL, "s1", "r1", "review"), 403)
        self.assertEqual(self.status(service.break_lock, self.db, ALICE, "s1", "r1", "because"), 403)

    def test_ids_validated(self):
        self.assertEqual(self.status(service.create_survey, self.db, ALICE, "../etc", ""), 422)
        self.assertEqual(self.status(service.register_recordings, self.db, ALICE, "s1",
                                     ["ok", "bad/id"]), 422)


class Locks(HubCase):
    def test_one_reviewer_at_a_time(self):
        lock = service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        with self.assertRaises(HubError) as ctx:
            service.acquire_lock(self.db, BOB, "s1", "r1", "review")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.extra["holder"], "alice@example.org")
        # other recordings are free
        service.acquire_lock(self.db, BOB, "s1", "r2", "review")
        # re-locking your own lock renews it
        again = service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        self.assertEqual(again["token"], lock["token"])
        service.release_lock(self.db, ALICE, "s1", "r1", lock["token"])
        service.acquire_lock(self.db, BOB, "s1", "r1", "review")

    def test_expired_lock_can_be_taken_and_old_holder_cannot_sign_off(self):
        lock = service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        self.expire_locks()
        self.assertEqual(self.status(service.signoff, self.db, ALICE, "s1", "r1",
                                     lock["token"], SHA_A), 409)
        service.acquire_lock(self.db, BOB, "s1", "r1", "review")
        self.assertEqual(self.status(service.renew_lock, self.db, ALICE, "s1", "r1",
                                     lock["token"]), 409)

    def test_admin_breaks_lock_with_reason(self):
        service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        self.assertEqual(self.status(service.break_lock, self.db, ADMIN, "s1", "r1", ""), 422)
        broken = service.break_lock(self.db, ADMIN, "s1", "r1", "laptop lost in the field")
        self.assertEqual(broken["holder"], "alice")
        service.acquire_lock(self.db, BOB, "s1", "r1", "review")

    def test_lock_list_shows_holder(self):
        service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        rows = {r["id"]: r for r in service.list_recordings(self.db, BOB, "s1")}
        self.assertEqual(rows["r1"]["lock_holder"], "alice@example.org")
        self.assertIsNone(rows["r2"]["lock_holder"])


class SeparationOfDuties(HubCase):
    def signed_off(self, who=ALICE, sha=SHA_A):
        lock = service.acquire_lock(self.db, who, "s1", "r1", "review")
        return service.signoff(self.db, who, "s1", "r1", lock["token"], sha, rows=12)

    def test_full_lifecycle(self):
        self.assertEqual(self.status(service.acquire_lock, self.db, CAROL, "s1", "r1", "qa"), 409)
        rec = self.signed_off()
        self.assertEqual((rec["status"], rec["reviewer"]), ("signed_off", "alice"))
        q = service.acquire_lock(self.db, CAROL, "s1", "r1", "qa")
        # the log QA'd must be the log signed off
        self.assertEqual(self.status(service.qa_decision, self.db, CAROL, "s1", "r1", q["token"],
                                     "approve", SHA_B), 409)
        rec = service.qa_decision(self.db, CAROL, "s1", "r1", q["token"], "approve", SHA_A)
        self.assertEqual((rec["status"], rec["qa_by"]), ("approved", "carol"))
        self.assertEqual(self.status(service.acquire_lock, self.db, ALICE, "s1", "r1", "review"), 409)
        service.reopen(self.db, ADMIN, "s1", "r1", "species id queried by client")
        service.acquire_lock(self.db, ALICE, "s1", "r1", "review")

    def test_reviewer_cannot_qa_own_work_even_with_qa_role(self):
        self.signed_off(who=DAVE)
        self.assertEqual(self.status(service.acquire_lock, self.db, DAVE, "s1", "r1", "qa"), 403)

    def test_reject_sends_back_for_review(self):
        self.signed_off()
        q = service.acquire_lock(self.db, CAROL, "s1", "r1", "qa")
        rec = service.qa_decision(self.db, CAROL, "s1", "r1", q["token"], "reject", SHA_A,
                                  note="missed three emergences")
        self.assertEqual(rec["status"], "rejected")
        rec = self.signed_off(who=BOB, sha=SHA_B)
        self.assertEqual((rec["reviewer"], rec["qa_by"]), ("bob", None))

    def test_signoff_needs_a_review_lock(self):
        q_holder = service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        self.assertEqual(self.status(service.signoff, self.db, BOB, "s1", "r1",
                                     q_holder["token"], SHA_A), 409)

    def test_everything_is_audited(self):
        self.signed_off()
        from brain import audit
        self.assertEqual(audit.verify(T1), [])
        actions = [json.loads(line)["action"] for line in
                   (self.tmp / "audit" / f"{T1}.jsonl").read_text().splitlines()]
        for a in ("hub.survey.create", "hub.lock.acquire", "hub.review.signoff"):
            self.assertIn(a, actions)


class Jobs(HubCase):
    def test_params_are_restricted(self):
        self.assertEqual(self.status(service.enqueue, self.db, ALICE, "s1", "prep",
                                     {"dest": "C:/"}), 422)
        for bad in ("../../other/manifest.json", "/etc/passwd", "C:\\x.json", "a/../../b"):
            self.assertEqual(self.status(service.enqueue, self.db, ALICE, "s1", "prep",
                                         {"manifest": bad}), 422, bad)
        self.assertEqual(self.status(service.enqueue, self.db, ALICE, "s1", "report", {}), 422)
        self.assertEqual(self.status(service.enqueue, self.db, CAROL, "s1", "scan", {}), 403)

    def test_claim_priority_retry_dead(self):
        low = service.enqueue(self.db, ALICE, "s1", "scan", {})
        high = service.enqueue(self.db, ALICE, "s1", "detect", {}, priority=5)
        self.assertEqual(queue.claim(self.db, "w1")["id"], high["id"])
        job = queue.claim(self.db, "w2")
        self.assertEqual(job["id"], low["id"])
        self.assertIsNone(queue.claim(self.db, "w3"))
        self.assertEqual(queue.fail(self.db, job["id"], "w1", "not mine"), "not-ours")
        self.assertEqual(queue.fail(self.db, job["id"], "w2", "disk full"), "queued")
        self.assertIsNone(queue.claim(self.db, "w2"))          # backing off
        for attempt in (2, 3):
            with self.db.tx() as c:
                c.execute("UPDATE jobs SET run_after = ? WHERE id = ?", (ts(utcnow()), job["id"]))
            j = queue.claim(self.db, "w2")
            self.assertEqual(j["attempts"], attempt)
            status = queue.fail(self.db, j["id"], "w2", "disk full")
        self.assertEqual(status, "dead")
        self.assertEqual(service.get_job(self.db, ALICE, job["id"])["status"], "dead")

    def test_crashed_worker_job_is_reaped(self):
        job = service.enqueue(self.db, ALICE, "s1", "scan", {})
        queue.claim(self.db, "crashy", lease_s=60)
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET locked_until = ?", (ts(utcnow() - timedelta(seconds=5)),))
        self.assertEqual(queue.reap(self.db), {"requeued": 1, "dead": 0})
        again = queue.claim(self.db, "healthy")
        self.assertEqual((again["id"], again["attempts"]), (job["id"], 2))
        self.assertFalse(queue.heartbeat(self.db, job["id"], "crashy"))
        self.assertTrue(queue.complete(self.db, job["id"], "healthy", {"ok": 1}))

    def test_cancel_only_queued_and_only_owner(self):
        job = service.enqueue(self.db, ALICE, "s1", "scan", {})
        self.assertEqual(self.status(service.cancel_job, self.db, BOB, job["id"]), 403)
        service.cancel_job(self.db, ALICE, job["id"])
        self.assertEqual(self.status(service.cancel_job, self.db, ALICE, job["id"]), 409)

    def test_jobs_are_tenant_scoped(self):
        job = service.enqueue(self.db, ALICE, "s1", "scan", {})
        self.assertEqual(self.status(service.get_job, self.db, EVE, job["id"]), 404)
        self.assertEqual(service.list_jobs(self.db, EVE), [])


class Anchors(HubCase):
    def chain(self, n=5, folder="f", label="real"):
        from emergence_kit import audit as kit
        d = self.tmp / folder
        os.environ["EMERGENCE_USER"] = "alice@example.org"
        try:
            for i in range(n):
                kit.record(d, "test", {"i": i, "label": label})
        finally:
            os.environ.pop("EMERGENCE_USER", None)
        return d, kit.read_entries(d)

    def test_truncated_and_rewritten_trails_are_detected(self):
        d, entries = self.chain(5)
        res = service.anchor(self.db, ALICE, "s1", entries)
        self.assertEqual((res["accepted"], res["conflicts"]), (5, []))
        self.assertTrue(service.verify_against_anchors(self.db, ALICE, "s1", entries)["ok"])
        # entries 4 and 5 deleted, head rolled back: invisible locally, caught here
        cut = service.verify_against_anchors(self.db, ALICE, "s1", entries[:3])
        self.assertFalse(cut["ok"])
        self.assertIn("cut short", cut["problems"][0])
        # whole chain rebuilt with different content: verifies locally, not here
        _, forged = self.chain(5, folder="forged", label="forged")
        self.assertNotEqual(forged[0]["hash"], entries[0]["hash"])
        v = service.verify_against_anchors(self.db, ALICE, "s1", forged)
        self.assertIn("rewritten", v["problems"][0])
        res = service.anchor(self.db, ALICE, "s1", forged)
        self.assertEqual((res["accepted"], len(res["conflicts"])), (0, 5))

    def test_incremental_anchoring_must_follow_the_chain(self):
        _, entries = self.chain(3)
        service.anchor(self.db, ALICE, "s1", entries[:2])
        forged_next = {**entries[2], "prev": "c" * 64}
        res = service.anchor(self.db, ALICE, "s1", [forged_next])
        self.assertEqual(res["accepted"], 0)
        res = service.anchor(self.db, ALICE, "s1", entries)
        self.assertEqual((res["accepted"], res["already_anchored"]), (1, 2))
        v = service.verify_against_anchors(self.db, ALICE, "s1", entries)
        self.assertEqual((v["ok"], v["anchored_up_to"], v["not_yet_anchored"]), (True, 3, 0))


class Backups(HubCase):
    def test_backup_verify_tamper_and_restore(self):
        storage = self.tmp / "storage"
        review = storage / T1 / "s1" / "review" / "r1"
        review.mkdir(parents=True)
        (review / "review_log_r1.csv").write_text("clock_time,event\n21:14:32,emergence\n")
        (review / "r1_review.mp4").write_bytes(b"\0" * 1024)       # video: not backed up
        lock = service.acquire_lock(self.db, ALICE, "s1", "r1", "review")
        service.signoff(self.db, ALICE, "s1", "r1", lock["token"], SHA_A)
        service.enqueue(self.db, ALICE, "s1", "scan", {})
        out = self.tmp / "backups"
        b = backup.create(self.db, out, storage_root=storage)
        files = json.loads((b / "manifest.json").read_text())["files"]
        self.assertIn(f"review/{T1}/s1/r1/review_log_r1.csv", files)
        self.assertFalse(any(f.endswith(".mp4") for f in files))
        self.assertTrue(any(f.startswith("audit/") for f in files))
        self.assertEqual(backup.verify(b), [])

        target = (f"sqlite:///{self.tmp / 'restored.db'}" if self.kind == "sqlite" else PG_URL)
        if self.kind == "postgresql":
            self.db.close()
        problems = backup.restore_test(b, target)
        self.assertEqual(problems, [])
        restored = Database.connect(target)
        self.assertEqual(service.list_recordings(restored, ALICE, "s1")[0]["status"], "signed_off")
        restored.close()

        # tampering is caught before anything is restored
        (b / "audit" / f"{T1}.jsonl").write_text("{}\n")
        self.assertTrue(any("checksum" in p for p in backup.verify(b)))
        with self.assertRaises(backup.BackupError):
            backup.restore(b, target, force=True)

    def test_restore_refuses_non_empty_target_and_retention(self):
        out = self.tmp / "backups"
        b = backup.create(self.db, out)
        if self.kind == "sqlite":
            with self.assertRaises(backup.BackupError):
                backup.restore(b, self.db.url)
        for i in range(3):
            (out / f"hub-2020010{i}T000000Z").mkdir()
            (out / f"hub-2020010{i}T000000Z" / "manifest.json").write_text("{}")
        (out / "unrelated").mkdir()
        removed = backup.prune(out, keep=2)
        self.assertEqual(len(removed), 2)
        self.assertTrue((out / "unrelated").exists() and b.exists())


class Api(HubCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")
        from hub import api
        self.api = api
        api._db = self.db
        self.who = ALICE
        api.app.dependency_overrides[api.principal] = lambda: self.who
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()
        self.api._db = None
        super().tearDown()

    def test_needs_sign_in(self):
        self.api.app.dependency_overrides.clear()
        r = self.client.get("/surveys")
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/surveys", headers={"Authorization": "Bearer not-a-token"})
        self.assertIn(r.status_code, (401, 503))

    def test_health_is_public(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["schema"], LATEST)

    def test_review_flow_over_http(self):
        r = self.client.post("/surveys/s1/recordings/r1/lock", json={"kind": "review"})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        self.who = BOB
        r = self.client.post("/surveys/s1/recordings/r1/lock", json={"kind": "review"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["holder"], "alice@example.org")
        self.who = ALICE
        r = self.client.post("/surveys/s1/recordings/r1/signoff",
                             json={"token": token, "log_sha256": SHA_A, "rows": 3})
        self.assertEqual(r.json()["status"], "signed_off")
        self.who = CAROL
        token = self.client.post("/surveys/s1/recordings/r1/lock", json={"kind": "qa"}).json()["token"]
        r = self.client.post("/surveys/s1/recordings/r1/qa",
                             json={"token": token, "decision": "approve", "log_sha256": SHA_A})
        self.assertEqual(r.json()["status"], "approved")

    def test_validation(self):
        r = self.client.post("/surveys/s1/recordings/r1/signoff",
                             json={"token": "x", "log_sha256": "not-a-hash"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/surveys/s1/jobs", json={"kind": "shell", "params": {}})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/surveys/s1/anchors", json={"entries": [{"seq": 0, "hash": SHA_A}]})
        self.assertEqual(r.status_code, 422)


class KitAgainstRealHub(HubCase):
    """The Emergence Kit's own CLI talking HTTP to a running hub (uvicorn): two people on
    two 'laptops', with the hub enforcing locks and separation of duties."""

    def setUp(self):
        super().setUp()
        try:
            import uvicorn
            from fastapi import Header
        except ImportError:
            self.skipTest("fastapi/uvicorn not installed")
        import socket
        from hub import api
        people = {"alice": ALICE, "bob": BOB, "carol": CAROL}

        def fake_principal(authorization: str | None = Header(default=None)):
            return people[(authorization or " nobody").split()[1]]
        api._db = self.db
        api.app.dependency_overrides[api.principal] = fake_principal
        self.api = api
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port,
                                                    log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        import time
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        os.environ["EMERGENCE_HOME"] = str(self.tmp / "kit-home")
        self.folder = self.tmp / "review"
        (self.folder / "r1").mkdir(parents=True)
        (self.folder / "prep_report.json").write_text(json.dumps({"recordings": [{"id": "r1"}]}))
        rows = "clock_time,event,species_group,count,direction,observer,notes\n"
        (self.folder / "r1" / "review_log_r1.csv").write_text(rows + "21:14:32,emergence,P,1,,A,\n")
        (self.folder / "r1" / "qa_log_r1.csv").write_text(rows + "21:14:33,emergence,P,1,,C,\n")

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.should_exit = True
            self.thread.join(timeout=10)
            self.api.app.dependency_overrides.clear()
            self.api._db = None
        for k in ("EMERGENCE_HOME", "EMERGENCE_HUB_TOKEN", "EMERGENCE_USER"):
            os.environ.pop(k, None)
        super().tearDown()

    def kit(self, who, *argv):
        import contextlib
        import io
        from emergence_kit.cli import main
        os.environ["EMERGENCE_HUB_TOKEN"] = who
        os.environ["EMERGENCE_USER"] = f"{who}@example.org"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in argv])
        return code, out.getvalue() + err.getvalue()

    def test_two_reviewers_one_hub(self):
        f = self.folder
        code, out = self.kit("alice", "hub", "link", f, "--survey", "barn-c", "--url", self.url)
        self.assertEqual(code, 0, out)
        self.assertIn("1 recordings registered", out)
        code, out = self.kit("alice", "hub", "lock", f, "r1")
        self.assertEqual(code, 0, out)
        code, out = self.kit("bob", "hub", "lock", f, "r1")          # bob's laptop
        self.assertEqual(code, 1)
        self.assertIn("locked for review by alice@example.org", out)
        code, out = self.kit("alice", "signoff", f, "r1")
        self.assertEqual(code, 0, out)
        code, out = self.kit("alice", "qa", f, "r1", "--decision", "approve")
        self.assertEqual(code, 1)                                    # hub: no QA role
        self.assertIn("403", out)
        code, out = self.kit("carol", "qa", f, "r1", "--decision", "approve")
        self.assertEqual(code, 0, out)
        code, out = self.kit("alice", "hub", "status", f)
        self.assertIn("approved", out)
        self.assertIn("QA carol@example.org", out)
        code, out = self.kit("alice", "hub", "verify", f)
        self.assertEqual(code, 0, out)

        # cut the last two audit entries and roll the head back: the folder alone can't
        # tell, the hub can
        from emergence_kit import audit as kit
        entries = kit.read_entries(f)
        lines = (f / "audit.jsonl").read_text().splitlines()[:-2]
        (f / "audit.jsonl").write_text("\n".join(lines) + "\n")
        (f / "audit.head").write_text(f"{entries[-3]['seq']} {entries[-3]['hash']}\n")
        self.assertEqual(kit.verify(f)[0], [])
        code, out = self.kit("alice", "hub", "verify", f)
        self.assertEqual(code, 1)
        self.assertIn("cut short", out)

    def test_offline_hub_does_not_stop_work(self):
        f = self.folder
        self.assertEqual(self.kit("alice", "hub", "link", f, "--survey", "s1",
                                  "--url", self.url)[0], 0)
        self.server.should_exit = True
        self.thread.join(timeout=10)
        del self.server
        code, out = self.kit("alice", "hub", "anchor", f)
        self.assertEqual(code, 1)
        self.assertIn("not reachable", out)
        # signing off needs the hub (it's what stops two people signing off)...
        code, out = self.kit("alice", "signoff", f, "r1")
        self.assertEqual(code, 1)
        # ...but the best-effort anchoring after other commands never fails them
        from emergence_kit.hub_client import sync_anchors
        msgs = []
        self.assertIsNone(sync_anchors(f, say=msgs.append))
        self.assertIn("not anchored", msgs[0])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class WorkerEndToEnd(HubCase):
    """Real jobs on a fake camera card: scan -> prep -> detect, run by the worker."""

    def test_pipeline_on_server_storage(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        import make_fake_card
        from hub.worker import Worker
        storage = self.tmp / "storage"
        os.environ["HUB_STORAGE_ROOT"] = str(storage)
        service.create_survey(self.db, ALICE, "barn-b", "Barn B")
        cards = storage / T1 / "barn-b" / "cards"
        make_fake_card.main([str(cards), "--small", "--format", "xavc", "--cards", "1",
                             "--clips", "2", "--clip-seconds", "3"])
        w = Worker(self.db, name="test-worker")
        self.assertIsNone(w.run_one())                     # nothing queued yet

        service.enqueue(self.db, ALICE, "barn-b", "scan", {})
        job = w.run_one()
        self.assertEqual(job["status"], "done", job.get("error"))
        recs = service.list_recordings(self.db, ALICE, "barn-b")
        self.assertEqual(len(recs), 1)
        rid = recs[0]["id"]

        service.enqueue(self.db, ALICE, "barn-b", "prep", {})
        self.assertEqual(w.run_one()["status"], "done")
        review = storage / T1 / "barn-b" / "review"
        self.assertTrue((review / rid / f"{rid}_review.mp4").exists())

        service.enqueue(self.db, ALICE, "barn-b", "detect", {})
        self.assertEqual(w.run_one()["status"], "done")
        self.assertTrue((review / rid / f"detections_{rid}.csv").exists())

        # the folder's audit trail names the worker and the requester, and is anchored
        from emergence_kit import audit as kit
        entries = kit.read_entries(review)
        self.assertEqual([e["action"] for e in entries][:1], ["scan"])
        self.assertIn("for alice@example.org", entries[0]["actor"]["user"])
        v = service.verify_against_anchors(self.db, ALICE, "barn-b", entries)
        self.assertEqual((v["ok"], v["anchored_up_to"]), (True, len(entries)))

        # a job pointing outside the survey is refused at the door and at the worker
        self.assertEqual(self.status(service.enqueue, self.db, ALICE, "barn-b", "prep",
                                     {"manifest": "../../x.json"}), 422)
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs (tenant_id, survey_id, kind, params, status, run_after, "
                      "created_by, created_at) VALUES (?, 'barn-b', 'prep', ?, 'queued', ?, "
                      "'x', ?)", (T1, json.dumps({"manifest": "../../../x.json"}),
                                  ts(utcnow()), ts(utcnow())))
        bad = w.run_one()
        self.assertEqual(bad["status"], "dead")
        self.assertIn("outside the survey folder", bad["error"])


@unittest.skipUnless(PG_URL, "HUB_TEST_PG_URL not set")
class PgConcurrency(HubCase):
    kind = "postgresql"

    def test_many_workers_never_share_a_job(self):
        for _ in range(40):
            service.enqueue(self.db, ALICE, "s1", "scan", {})
        got, errors = [], []

        def work(name):
            db = Database.connect(PG_URL)
            try:
                while True:
                    job = queue.claim(db, name)
                    if not job:
                        return
                    got.append(job["id"])
                    queue.complete(db, job["id"], name, {})
            except Exception as exc:              # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)

    def test_simultaneous_lock_attempts_one_winner(self):
        people = [person(f"u{i}", {"Survey.Review"}) for i in range(8)]
        wins, conflicts, errors = [], [], []
        barrier = threading.Barrier(len(people))

        def grab(p):
            db = Database.connect(PG_URL)
            try:
                barrier.wait()
                service.acquire_lock(db, p, "s1", "r1", "review")
                wins.append(p.subject)
            except HubError as exc:
                conflicts.append(exc.status)
            except Exception as exc:              # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=grab, args=(p,)) for p in people]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(wins), 1)
        self.assertEqual(conflicts, [409] * 7)


def _pg(cls):
    return unittest.skipUnless(PG_URL, "HUB_TEST_PG_URL not set")(
        type(f"Pg{cls.__name__}", (cls,), {"kind": "postgresql"}))


PgTenancy, PgLocks, PgSeparation, PgJobs, PgAnchors, PgBackups, PgApi = map(
    _pg, (Tenancy, Locks, SeparationOfDuties, Jobs, Anchors, Backups, Api))


if __name__ == "__main__":
    unittest.main()
