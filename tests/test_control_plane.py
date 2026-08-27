import unittest
import asyncio
from datetime import datetime, timezone

from control_plane import (
    PROTOCOL_VERSION,
    canonical_json,
    heartbeat_payload,
    registration_payload,
    request_signature,
    signed_headers,
)
from control_plane_client import ControlPlaneClient


class ControlPlaneProtocolTests(unittest.TestCase):
    def setUp(self):
        self.started_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def test_canonical_json_is_deterministic(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')

    def test_signature_changes_when_signed_request_changes(self):
        signature = request_signature("secret", "100", "nonce", "POST", "/path", "{}")
        self.assertNotEqual(signature, request_signature("secret", "100", "nonce-2", "POST", "/path", "{}"))
        self.assertNotEqual(signature, request_signature("secret", "100", "nonce", "POST", "/other", "{}"))
        self.assertNotEqual(signature, request_signature("secret", "100", "nonce", "POST", "/path", '{"x":1}'))

    def test_signed_headers_contain_protocol_metadata_without_secret(self):
        headers = signed_headers("secret", "100", "nonce", "POST", "/path", "{}")
        self.assertEqual(headers["X-TeleDrop-Protocol"], PROTOCOL_VERSION)
        self.assertNotIn("secret", str(headers))
        self.assertEqual(len(headers["X-TeleDrop-Signature"]), 64)

    def test_registration_payload_contains_public_instance_metadata(self):
        payload = registration_payload(
            instance_id="bot-01", telegram_bot_id=123, username="example_bot",
            version="2.2.0", started_at=self.started_at,
        )
        self.assertEqual(payload["instance_id"], "bot-01")
        self.assertEqual(payload["started_at"], "2026-08-27T12:00:00Z")
        self.assertNotIn("token", canonical_json(payload).lower())

    def test_heartbeat_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            heartbeat_payload(
                instance_id="bot-01", telegram_bot_id=123, username="example_bot",
                version="2.2.0", started_at=self.started_at, uptime_seconds=60, status="unknown",
            )

    def test_disabled_client_does_not_create_a_network_session(self):
        client = ControlPlaneClient("", "", "", 10)
        self.assertFalse(client.enabled)
        self.assertFalse(asyncio.run(client.post("/path", {})))
        self.assertIsNone(client._session)
        self.assertFalse(client.health_snapshot()["enabled"])


if __name__ == "__main__":
    unittest.main()