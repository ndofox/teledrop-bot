import json
import unittest
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from control_plane import METRICS_PATH, aggregate_payload, canonical_json, request_signature
from control_plane_server.app import _read_bounded_body, create_app
from control_plane_server.credentials import CredentialStore


class FakeRepository:
    def __init__(self, database_ok=True):
        self.database_ok = database_ok
        self.agents = {}
        self.nonces = set()
        self.batches = {}
        self.current = {}

    async def ping(self):
        return self.database_ok

    async def consume_nonce(self, instance_id, nonce, ttl_seconds, now_epoch):
        key = (instance_id, nonce)
        if key in self.nonces:
            return False
        self.nonces.add(key)
        return True

    async def register_agent(self, payload, secret_hash, now):
        document = dict(payload)
        document.update({"secret_hash": secret_hash, "last_seen_at": now, "status": "online"})
        self.agents[payload["instance_id"]] = document

    async def agent_secret_matches(self, instance_id, secret_hash):
        return self.agents.get(instance_id, {}).get("secret_hash") == secret_hash

    async def heartbeat_agent(self, payload, now):
        if payload["instance_id"] not in self.agents:
            return False
        self.agents[payload["instance_id"]].update(payload)
        self.agents[payload["instance_id"]]["last_seen_at"] = now
        return True

    async def ingest_metrics(self, payload, now, processing_lease_seconds=300):
        from control_plane_server.repository import MetricsIngestResult

        batch_id = payload["batch_id"]
        if batch_id in self.batches:
            if self.batches[batch_id] == payload:
                return MetricsIngestResult(status="duplicate", payload_hash="same")
            return MetricsIngestResult(status="permanent_conflict", payload_hash="diff")
        self.batches[batch_id] = dict(payload)
        self.current[payload["instance_id"]] = payload["current"]
        return MetricsIngestResult(status="accepted", payload_hash="new")

    async def cleanup_metrics_batches(self, retention_days, limit):
        return 0


class ControlPlaneServerTests(unittest.IsolatedAsyncioTestCase):
    INSTANCE_ID = "bot-01"
    SECRET = "a-secure-test-secret-1234"
    NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    async def asyncSetUp(self):
        self.repository = FakeRepository()
        self.app = create_app(
            repository=self.repository,
            credential_store=CredentialStore({self.INSTANCE_ID: self.SECRET}),
            max_clock_skew_seconds=300,
            nonce_ttl_seconds=600,
            clock=lambda: self.NOW,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def payload(self, status="online"):
        value = {
            "instance_id": self.INSTANCE_ID,
            "telegram_bot_id": 123456789,
            "username": "example_bot",
            "version": "2.2.0",
            "started_at": "2026-08-27T12:00:00Z",
            "protocol_version": "1",
        }
        if status is not None:
            value.update({"uptime_seconds": 60, "status": status})
        return value

    def signed_request(self, path, payload, *, nonce="nonce-1", timestamp="1787832000", secret=None):
        body = canonical_json(payload)
        signing_secret = secret or self.SECRET
        signature = request_signature(signing_secret, timestamp, nonce, "POST", path, body)
        return body, {
            "Content-Type": "application/json",
            "X-TeleDrop-Protocol": "1",
            "X-TeleDrop-Timestamp": timestamp,
            "X-TeleDrop-Nonce": nonce,
            "X-TeleDrop-Signature": signature,
        }

    async def test_healthz_reports_database_status(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["database"], "ok")

    async def test_registration_and_heartbeat(self):
        body, headers = self.signed_request("/api/v1/agents/register", self.payload(), nonce="register-1")
        response = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "registered")

        heartbeat = self.payload()
        heartbeat.update({"uptime_seconds": 120, "status": "online"})
        body, headers = self.signed_request("/api/v1/agents/heartbeat", heartbeat, nonce="heartbeat-1")
        response = await self.client.post("/api/v1/agents/heartbeat", data=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "accepted")

    async def test_heartbeat_requires_registration(self):
        payload = self.payload()
        payload.update({"uptime_seconds": 60, "status": "online"})
        body, headers = self.signed_request("/api/v1/agents/heartbeat", payload, nonce="heartbeat-unregistered")
        response = await self.client.post("/api/v1/agents/heartbeat", data=body, headers=headers)
        self.assertEqual(response.status, 404)

    async def test_invalid_signature_is_rejected_without_persisting_nonce(self):
        body, headers = self.signed_request(
            "/api/v1/agents/register", self.payload(), nonce="bad-signature", secret="wrong-secret-123456"
        )
        response = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.repository.nonces, set())

    async def test_replayed_nonce_is_rejected(self):
        body, headers = self.signed_request("/api/v1/agents/register", self.payload(), nonce="replay-1")
        first = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        second = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 401)
        self.assertEqual((await second.json())["error"], "replayed request")

    async def test_stale_timestamp_is_rejected(self):
        body, headers = self.signed_request(
            "/api/v1/agents/register", self.payload(), nonce="stale-1", timestamp="1787000000"
        )
        response = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        self.assertEqual(response.status, 401)

    async def test_non_canonical_body_is_rejected(self):
        payload = self.payload()
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        headers = {
            "Content-Type": "application/json",
            "X-TeleDrop-Protocol": "1",
            "X-TeleDrop-Timestamp": "1787832000",
            "X-TeleDrop-Nonce": "noncanonical-1",
            "X-TeleDrop-Signature": request_signature(
                self.SECRET, "1787832000", "noncanonical-1", "POST", "/api/v1/agents/register", body
            ),
        }
        response = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        self.assertEqual(response.status, 400)

    async def test_secret_is_not_in_response_or_persisted_metadata(self):
        body, headers = self.signed_request("/api/v1/agents/register", self.payload(), nonce="secret-check-1")
        response = await self.client.post("/api/v1/agents/register", data=body, headers=headers)
        response_text = await response.text()
        self.assertNotIn(self.SECRET, response_text)
        self.assertNotEqual(self.repository.agents[self.INSTANCE_ID]["secret_hash"], self.SECRET)
        self.assertNotIn(self.SECRET, json.dumps(self.repository.agents, default=str))

    async def test_database_failure_makes_healthz_degraded(self):
        self.repository.database_ok = False
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["status"], "degraded")

    def metrics_payload(self):
        observed_at = self.NOW
        current = {
            "registered_users": 10,
            "reachable_users": 9,
            "active_24h": 3,
            "active_7d": 5,
            "active_30d": 7,
        }
        daily = [
            {
                "date_utc": "2026-08-27",
                "active_users": 4,
                "interaction_count": 12,
                "observed_at": observed_at,
            }
        ]
        return aggregate_payload(instance_id=self.INSTANCE_ID, observed_at=observed_at, current=current, daily=daily)

    async def register_agent(self):
        body, headers = self.signed_request("/api/v1/agents/register", self.payload(), nonce="reg-metrics-1")
        await self.client.post("/api/v1/agents/register", data=body, headers=headers)

    async def test_metrics_accepted(self):
        await self.register_agent()
        payload = self.metrics_payload()
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-1")
        response = await self.client.post(METRICS_PATH, data=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "accepted")

    async def test_metrics_duplicate_exact(self):
        await self.register_agent()
        payload = self.metrics_payload()
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-dup-1")
        first = await self.client.post(METRICS_PATH, data=body, headers=headers)
        body2, headers2 = self.signed_request(METRICS_PATH, payload, nonce="metrics-dup-2")
        second = await self.client.post(METRICS_PATH, data=body2, headers=headers2)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual((await second.json())["status"], "duplicate")

    async def test_metrics_raw_user_id_rejected(self):
        await self.register_agent()
        payload = self.metrics_payload()
        payload["user_id"] = 99999
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-raw-1")
        response = await self.client.post(METRICS_PATH, data=body, headers=headers)
        self.assertEqual(response.status, 400)

    async def test_metrics_unknown_field_rejected(self):
        await self.register_agent()
        payload = self.metrics_payload()
        payload["surprise"] = True
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-unknown-1")
        response = await self.client.post(METRICS_PATH, data=body, headers=headers)
        self.assertEqual(response.status, 400)

    async def test_metrics_oversized_daily_rejected(self):
        await self.register_agent()
        observed_at = self.NOW
        current = {
            "registered_users": 1,
            "reachable_users": 1,
            "active_24h": 1,
            "active_7d": 1,
            "active_30d": 1,
        }
        daily = []
        for i in range(1, 32):
            daily.append(
                {
                    "date_utc": "2026-08-%02d" % i,
                    "active_users": 1,
                    "interaction_count": 1,
                    "observed_at": observed_at,
                }
            )
        payload = aggregate_payload(instance_id=self.INSTANCE_ID, observed_at=observed_at, current=current, daily=daily)
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-oversize-1")
        response = await self.client.post(METRICS_PATH, data=body, headers=headers)
        self.assertEqual(response.status, 400)

    async def test_metrics_body_too_large(self):
        small_app = create_app(
            repository=self.repository,
            credential_store=CredentialStore({self.INSTANCE_ID: self.SECRET}),
            max_clock_skew_seconds=300,
            nonce_ttl_seconds=600,
            max_body_bytes=100,
            clock=lambda: self.NOW,
        )
        client = TestClient(TestServer(small_app))
        await client.start_server()
        await self.register_agent()
        payload = self.metrics_payload()
        body, headers = self.signed_request(METRICS_PATH, payload, nonce="metrics-large-1")
        response = await client.post(METRICS_PATH, data=body, headers=headers)
        self.assertEqual(response.status, 413)
        await client.close()

    async def test_bounded_reader_missing_length_consumes_only_limit_plus_one(self):
        class Content:
            def __init__(self):
                self.data = b"x" * 20
                self.calls = []

            async def read(self, size):
                self.calls.append(size)
                result, self.data = self.data[:size], self.data[size:]
                return result

        content = Content()
        request = type("Request", (), {"headers": {}, "content": content})()
        self.assertIsNone(await _read_bounded_body(request, 10))
        self.assertEqual(sum(content.calls), 11)
        self.assertEqual(len(content.data), 9)

    async def test_bounded_reader_rejects_oversized_content_length_without_read(self):
        class Content:
            async def read(self, size):
                raise AssertionError("content reader must not be called")

        request = type("Request", (), {"headers": {"Content-Length": "11"}, "content": Content()})()
        self.assertIsNone(await _read_bounded_body(request, 10))

    async def test_bounded_reader_accepts_exact_limit_byte_for_byte(self):
        class Content:
            def __init__(self):
                self.data = b"0123456789"
                self.calls = []

            async def read(self, size):
                self.calls.append(size)
                result, self.data = self.data[:size], self.data[size:]
                return result

        content = Content()
        request = type("Request", (), {"headers": {"Content-Length": "10"}, "content": content})()
        self.assertEqual(await _read_bounded_body(request, 10), b"0123456789")


if __name__ == "__main__":
    unittest.main()