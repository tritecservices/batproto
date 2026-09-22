"""Key Vault secret loading, with a fake vault client (no Azure needed)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import secrets as S          # noqa: E402


class ResourceNotFoundError(Exception):   # same name the Azure SDK raises
    pass


class FakeVault:
    def __init__(self, data, broken=False):
        self.data, self.broken, self.calls = data, broken, []

    def get_secret(self, name):
        self.calls.append(name)
        if self.broken:
            raise ConnectionError("vault unreachable")
        if name not in self.data:
            raise ResourceNotFoundError(name)
        return type("Secret", (), {"value": self.data[name]})()


class Secrets(unittest.TestCase):
    def setUp(self):
        self.saved = {v: os.environ.pop(v, None) for v in S.SECRET_MAP.values()}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_loads_from_vault_and_skips_absent(self):
        res = S.load(FakeVault({"brain-api-key": "from-vault", "discord-bot-token": "tok"}))
        self.assertEqual(os.environ["BRAIN_API_KEY"], "from-vault")
        self.assertEqual((res["BRAIN_API_KEY"], res["NIGHTARC_PASSWORD"]), ("vault", "absent"))

    def test_existing_environment_wins_unless_override(self):
        os.environ["BRAIN_API_KEY"] = "pinned"
        S.load(FakeVault({"brain-api-key": "from-vault"}))
        self.assertEqual(os.environ["BRAIN_API_KEY"], "pinned")
        S.load(FakeVault({"brain-api-key": "from-vault"}), override=True)
        self.assertEqual(os.environ["BRAIN_API_KEY"], "from-vault")

    def test_unreachable_vault_fails_closed(self):
        with self.assertRaises(S.SecretsError):
            S.load(FakeVault({}, broken=True))

    def test_no_vault_configured_is_a_no_op(self):
        os.environ.pop("AZURE_KEYVAULT_URL", None)
        self.assertEqual(S.load(), {})

    def test_http_vault_url_refused(self):
        os.environ["AZURE_KEYVAULT_URL"] = "http://not-tls.example"
        try:
            with self.assertRaises(S.SecretsError):
                S.load()
        finally:
            os.environ.pop("AZURE_KEYVAULT_URL", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
