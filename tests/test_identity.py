"""Entra ID token checks, roles and tenant isolation - with a locally generated RSA
key standing in for Microsoft's signing keys, so these tests run offline.

The API-level tests (real HTTP requests through FastAPI) run when FastAPI is
installed; CI installs it.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    HAVE_JWT = True
except ImportError:
    HAVE_JWT = False

from brain import identity as I                                    # noqa: E402

T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"
EVIL = "99999999-9999-9999-9999-999999999999"
AUD = "api://ecomsp-brain"


@unittest.skipUnless(HAVE_JWT, "pyjwt[crypto] not installed")
class Entra(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(cls.key.public_key()))
        jwk["kid"] = "k1"
        cls.fetches = []

        def fetch(tenant):
            cls.fetches.append(tenant)
            return {"keys": [jwk]}
        cls.jwks = I.JwksCache(fetch=fetch)
        cls.cfg = I.EntraConfig(AUD, frozenset({T1, T2}))

    def token(self, tid=T1, aud=AUD, roles=("Brain.Read",), exp_in=3600, kid="k1",
              key=None, iss=None, **extra):
        now = int(time.time())
        claims = {"tid": tid, "aud": aud, "iss": iss or f"https://login.microsoftonline.com/{tid}/v2.0",
                  "iat": now, "exp": now + exp_in, "oid": "user-oid",
                  "preferred_username": "reviewer@example.org", "roles": list(roles), **extra}
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid})

    def verify(self, tok):
        return I.verify_entra(tok, self.cfg, self.jwks)

    def assertStatus(self, tok, status):
        with self.assertRaises(I.AuthError) as ctx:
            self.verify(tok)
        self.assertEqual(ctx.exception.status, status, ctx.exception.message)

    def test_valid_user_token(self):
        p = self.verify(self.token(roles=("Brain.Read", "Survey.Review", "Made.Up")))
        self.assertEqual((p.kind, p.tenant_id, p.name), ("user", T1, "reviewer@example.org"))
        self.assertEqual(p.roles, frozenset({"Brain.Read", "Survey.Review"}))   # unknown dropped

    def test_v2_bare_audience_accepted(self):
        self.assertEqual(self.verify(self.token(aud="ecomsp-brain")).tenant_id, T1)

    def test_app_token_is_an_app(self):
        p = self.verify(self.token(idtyp="app", preferred_username=None))
        self.assertEqual(p.kind, "app")

    def test_wrong_audience(self):
        self.assertStatus(self.token(aud="api://something-else"), 401)

    def test_expired(self):
        self.assertStatus(self.token(exp_in=-3600), 401)

    def test_tenant_not_allowed_and_no_key_fetch(self):
        before = len(self.fetches)
        self.assertStatus(self.token(tid=EVIL), 403)
        self.assertEqual(len(self.fetches), before)          # never contacted for EVIL

    def test_forged_signature(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertStatus(self.token(key=other), 401)

    def test_issuer_from_another_tenant(self):
        self.assertStatus(self.token(iss=f"https://login.microsoftonline.com/{T2}/v2.0"), 401)

    def test_unknown_key_id(self):
        self.assertStatus(self.token(kid="nope"), 401)

    def test_none_algorithm_rejected(self):
        now = int(time.time())
        tok = jwt.encode({"tid": T1, "aud": AUD, "exp": now + 60, "iat": now,
                          "iss": f"https://login.microsoftonline.com/{T1}/v2.0",
                          "roles": ["Platform.Admin"]}, None, algorithm="none")
        self.assertStatus(tok, 401)


class RolesAndTenants(unittest.TestCase):
    def test_admin_implies_every_role(self):
        p = I.Principal("user", T1, "x", roles=frozenset({"Platform.Admin"}))
        self.assertTrue(all(p.has(r) for r in I.ROLES))

    def test_missing_role_is_403(self):
        p = I.Principal("user", T1, "x", roles=frozenset({"Brain.Read"}))
        with self.assertRaises(I.AuthError) as ctx:
            I.require(p, "Survey.QA")
        self.assertEqual(ctx.exception.status, 403)

    def test_shared_key_can_only_read(self):
        os.environ["BRAIN_API_KEY"] = "a-real-key-for-tests-0123456789"
        try:
            p = I.verify_key("a-real-key-for-tests-0123456789")
            self.assertEqual(p.roles, frozenset({"Brain.Read"}))
            self.assertFalse(p.has("Survey.QA"))
        finally:
            os.environ.pop("BRAIN_API_KEY", None)

    def test_each_tenant_gets_its_own_database(self):
        os.environ["BRAIN_TENANT_DB_DIR"] = "/data/tenants"
        try:
            a = I.tenant_db_path(I.Principal("user", T1, "x"), "data/brain.db")
            b = I.tenant_db_path(I.Principal("user", T2, "x"), "data/brain.db")
            k = I.tenant_db_path(I.Principal("key", "default", "api-key"), "data/brain.db")
            self.assertNotEqual(a, b)
            self.assertTrue(a.endswith(f"{T1}.db") and b.endswith(f"{T2}.db"))
            self.assertEqual(k, "data/brain.db")
            evil = I.tenant_db_path(I.Principal("user", "../../etc/passwd", "x"), "d.db")
            self.assertNotIn("..", evil)                    # no path traversal
        finally:
            os.environ.pop("BRAIN_TENANT_DB_DIR", None)

    def test_entra_mode_without_settings_fails_closed(self):
        os.environ["BRAIN_AUTH"] = "entra"
        try:
            with self.assertRaises(I.AuthError) as ctx:
                I.authenticate("Bearer abc", None)
            self.assertEqual(ctx.exception.status, 503)
            with self.assertRaises(I.AuthError) as ctx:
                I.authenticate(None, "some-key")             # key not accepted in entra mode
            self.assertEqual(ctx.exception.status, 401)
        finally:
            os.environ.pop("BRAIN_AUTH", None)


try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI and HAVE_JWT, "fastapi / pyjwt not installed")
class ApiWithEntra(unittest.TestCase):
    """End to end through HTTP: two tenants, roles, isolation, whoami."""

    def setUp(self):
        import tempfile
        from fastapi.testclient import TestClient
        from brain import api
        from brain.store import Document, Store

        self.tmp = tempfile.mkdtemp()
        os.environ.update({"BRAIN_AUTH": "entra", "ENTRA_AUDIENCE": AUD,
                           "ENTRA_ALLOWED_TENANTS": f"{T1},{T2}",
                           "BRAIN_TENANT_DB_DIR": self.tmp})
        for tid, text in ((T1, "tenant one geopackage secret"), (T2, "tenant two geopackage")):
            st = Store(os.path.join(self.tmp, f"{tid}.db"))
            st.upsert(Document(id=f"doc-{tid[:2]}", source="discord", title=text, text=text))
            st.close()
        Entra.setUpClass()
        I._jwks = Entra.jwks
        api._tenant_stores.clear()
        self.client = TestClient(api.app)
        self.maker = Entra()
        self.maker.key = Entra.key

    def tearDown(self):
        for k in ("BRAIN_AUTH", "ENTRA_AUDIENCE", "ENTRA_ALLOWED_TENANTS", "BRAIN_TENANT_DB_DIR"):
            os.environ.pop(k, None)

    def get(self, path, tok=None, **params):
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        return self.client.get(path, params=params, headers=headers)

    def test_tenants_only_see_their_own_data(self):
        r1 = self.get("/search", self.maker.token(tid=T1), q="geopackage").json()
        r2 = self.get("/search", self.maker.token(tid=T2), q="geopackage").json()
        self.assertEqual([h["doc_id"] for h in r1["hits"]], ["doc-11"])
        self.assertEqual([h["doc_id"] for h in r2["hits"]], ["doc-22"])

    def test_no_token_401_no_role_403(self):
        self.assertEqual(self.get("/search", q="x").status_code, 401)
        self.assertEqual(self.get("/search", self.maker.token(roles=()), q="x").status_code, 403)

    def test_whoami(self):
        me = self.get("/whoami", self.maker.token()).json()
        self.assertEqual((me["tenant"], me["name"], me["auth_mode"]),
                         (T1, "reviewer@example.org", "entra"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
