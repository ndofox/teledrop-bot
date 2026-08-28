"""Exact-body HTTP and heartbeat-isolation tests for the control-plane agent client.

These tests prove the client sends the exact persisted canonical payload, never
uses ``json=``, HMAC-signs the exact bytes sent, and that a metrics request held
open does not delay the heartbeat. No real Telegram service or production
database is contacted.
"""

import asyncio
import json
import time
import unittest
from datetime import datetime, timezone

from control_plane import (
    FORBIDDEN_METRICS_FIELDS,
    HEARTBEAT_PATH,
    METRICS_PATH,
    aggregate_payload,
    canonical_json,
    heartbeat_payload,
    request_signature,
)
from control_plane_client import ControlPlaneClient


class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {"status": "accepted"}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RequestContext:
    def __init__(self, session, url, data, json, headers):
        self._session = session
        self._url = url
        self._data = data
        self._json = json
        self._headers = headers

    async def __aenter__(self):
        self._session.record(self._url, self._data, self._json, self._headers)
        if self._url.endswith(METRICS_PATH) and self._session.gate is not None:
            self._session.metrics_waiters += 1
            await self._session.gate.wait()
        return self._session.response or _FakeResponse()

    async def __aexit__(self, *exc):
        return False


class _SessionHarness:
    def __init__(self, gate=None, response=None):
        self.calls = []
        self.gate = gate
        self.response = response
        self.metrics_waiters = 0
        self.closed = False

    def record(self, url, data, json, headers):
        self.calls.append({"url": url, "data": data, "json": json, "headers": headers})

    def post(self, url, *, data=None, json=None, headers=None):
        return _RequestContext(self, url, data, json, headers)

    async def close(self):
        self.closed = True


class _HarnessClient(ControlPlaneClient):
    def __init__(self, session):
        super().__init__("http://control.test", "bot-01", "a-secure-test-secret-1234", timeout_seconds=5)
        self._session = session

    async def _session_or_create(self):
        return self._session


class ExactBodyTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def _payload(self):
        return aggregate_payload(
            instance_id="bot-01",
            observed_at=self.NOW,
            current={
                "registered_users": 100,
                "reachable_users": 90,
                "active_24h": 10,
                "active_7d": 40,
                "active_30d": 70,
            },
            daily=[
                {
                    "date_utc": "2026-08-27",
                    "active_users": 5,
                    "interaction_count": 20,
                    "observed_at": self.NOW,
                }
            ],
        )

    async def test_exact_body_sent_and_signed(self):
        payload = self._payload()
        canonical = canonical_json(payload)
        session = _SessionHarness()
        client = _HarnessClient(session)
        result = await client.send_metrics(canonical)
        self.assertTrue(result.ok)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertIsNone(call["json"], "client must not reserialize via json=")
        sent = call["data"].decode("utf-8")
        self.assertEqual(sent, canonical, "the exact stored canonical payload must be sent")
        self.assertEqual(call["url"], "http://control.test" + METRICS_PATH)

        headers = call["headers"]
        recomputed = request_signature(
            "a-secure-test-secret-1234",
            headers["X-TeleDrop-Timestamp"],
            headers["X-TeleDrop-Nonce"],
            "POST",
            METRICS_PATH,
            sent,
        )
        self.assertEqual(recomputed, headers["X-TeleDrop-Signature"])

        parsed = json.loads(sent)
        self.assertEqual(parsed["batch_id"], payload["batch_id"])
        forbidden = FORBIDDEN_METRICS_FIELDS
        for field in forbidden:
            self.assertNotIn('"%s"' % field, sent)


class HeartbeatIsolationTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    async def test_metrics_hang_does_not_block_heartbeat(self):
        gate = asyncio.Event()
        session = _SessionHarness(gate=gate)
        client = _HarnessClient(session)
        canonical = canonical_json(
            aggregate_payload(
                instance_id="bot-01",
                observed_at=self.NOW,
                current={
                    "registered_users": 1,
                    "reachable_users": 1,
                    "active_24h": 1,
                    "active_7d": 1,
                    "active_30d": 1,
                },
                daily=[
                    {"date_utc": "2026-08-27", "active_users": 1, "interaction_count": 1, "observed_at": self.NOW}
                ],
            )
        )
        metrics_task = asyncio.create_task(client.send_metrics(canonical))
        await asyncio.sleep(0)  # let the metrics request enter the hang
        hb = heartbeat_payload(
            instance_id="bot-01",
            telegram_bot_id=123456789,
            username="bot",
            version="2.2.0",
            started_at=self.NOW,
            uptime_seconds=60,
            status="online",
        )
        start = time.perf_counter()
        ok = await asyncio.wait_for(client.heartbeat(hb), timeout=2)
        elapsed = time.perf_counter() - start
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.5, "heartbeat must not wait on a hung metrics request")
        self.assertFalse(metrics_task.done(), "metrics request is still pending")
        self.assertEqual(session.metrics_waiters, 1)
        gate.set()
        await metrics_task
        await client.close()


class MetricsClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_machine_readable_registration_404_is_retryable(self):
        response = _FakeResponse(status=404, payload={"error": "agent is not registered", "error_code": "agent_not_registered"})
        client = _HarnessClient(_SessionHarness(response=response))
        result = await client.send_metrics("{}")
        self.assertTrue(result.retryable)
        self.assertTrue(result.agent_not_registered)

    async def test_arbitrary_404_remains_permanent(self):
        response = _FakeResponse(status=404, payload={"error": "not found"})
        client = _HarnessClient(_SessionHarness(response=response))
        result = await client.send_metrics("{}")
        self.assertTrue(result.permanent)
        self.assertFalse(result.retryable)


if __name__ == "__main__":
    unittest.main()