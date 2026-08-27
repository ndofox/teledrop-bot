import os
import unittest
from unittest.mock import patch

from control_plane_server.config import _parse_agent_secrets, load_config
from control_plane_server.credentials import CredentialStore


class ControlPlaneConfigTests(unittest.TestCase):
    def test_parses_multiple_agent_secrets(self):
        values = _parse_agent_secrets('{"bot-01":"first-secret-123456","bot-02":"second-secret-123456"}')
        self.assertEqual(set(values), {"bot-01", "bot-02"})

    def test_rejects_short_agent_secret(self):
        with self.assertRaises(RuntimeError):
            _parse_agent_secrets('{"bot-01":"too-short"}')

    def test_load_config_uses_server_specific_environment(self):
        environment = {
            "CONTROL_PLANE_DATABASE_URL": "mongodb://localhost/control",
            "CONTROL_PLANE_DATABASE_NAME": "control_test",
            "CONTROL_PLANE_AGENT_SECRETS_JSON": '{"bot-01":"server-secret-123456"}',
            "CONTROL_PLANE_PORT": "8091",
        }
        with patch.dict(os.environ, environment, clear=False):
            config = load_config()
        self.assertEqual(config.database_name, "control_test")
        self.assertEqual(config.port, 8091)
        self.assertNotIn("server-secret-123456", repr(config))

    def test_credential_store_exposes_hash_not_raw_secret(self):
        store = CredentialStore({"bot-01": "server-secret-123456"})
        self.assertIsNone(store.secret_hash("unknown"))
        self.assertNotEqual(store.secret_hash("bot-01"), "server-secret-123456")
        self.assertTrue(store.matches_hash("bot-01", store.secret_hash("bot-01")))


if __name__ == "__main__":
    unittest.main()