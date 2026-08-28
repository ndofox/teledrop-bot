import asyncio
import logging
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TG_BOT_TOKEN", "123:TESTTOKEN")
os.environ.setdefault("APP_ID", "123456")
os.environ.setdefault("API_HASH", "0" * 32)
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017/test_teledrop")
os.environ.setdefault("CONTROL_PLANE_URL", "http://localhost:8090")
os.environ.setdefault("CONTROL_PLANE_INSTANCE_ID", "bot-01")
os.environ.setdefault("CONTROL_PLANE_SECRET", "a-secure-test-secret-1234")

import bot as bot_module
from control_plane_client import MetricsSendResult


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class FakeControlPlane:
    enabled = True

    def __init__(self, results=None):
        self.results = list(results or [])
        self.register_calls = 0
        self.heartbeat_calls = 0
        self.send_calls = 0
        self.first_send = asyncio.Event()

    async def register(self, payload):
        self.register_calls += 1
        return True

    async def heartbeat(self, payload):
        self.heartbeat_calls += 1
        return True

    async def send_metrics(self, body):
        self.send_calls += 1
        self.first_send.set()
        return self.results.pop(0) if self.results else MetricsSendResult(status_code=200, ok=True)


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, results=None):
        instance = bot_module.Bot.__new__(bot_module.Bot)
        instance.control_plane = FakeControlPlane(results)
        instance.control_plane_registered = asyncio.Event()
        instance.control_plane_registration_requested = asyncio.Event()
        instance.control_plane_metrics_wake = asyncio.Event()
        instance.LOGGER = lambda name: logging.getLogger(name)
        instance.telegram_bot_id = 123
        instance.username = "test_bot"
        instance.uptime = NOW
        return instance

    @staticmethod
    def record(status="pending", next_attempt_at=NOW):
        return {
            "_id": "batch-1", "canonical_payload": "{}", "status": status,
            "attempts": 0, "next_attempt_at": next_attempt_at,
        }

    async def test_metrics_loop_executes_production_tick_only_after_readiness_and_wake(self):
        instance = self.make_bot()
        active = self.record()
        claimed = dict(active, status="sending", sending_owner="owner", claim_generation=1)
        with patch.object(bot_module, "get_active_metrics_outbox", side_effect=[active, active]), \
                patch.object(bot_module, "claim_metrics_outbox", return_value=claimed), \
                patch.object(bot_module, "mark_metrics_outbox_accepted", return_value="transitioned"), \
                patch.object(bot_module, "cleanup_metrics_outbox", return_value=0), \
                patch.object(bot_module, "get_active_metrics_outbox_delay", return_value=None), \
                patch.object(bot_module, "CONTROL_PLANE_METRICS_INTERVAL", 1), \
                patch.object(bot_module, "CONTROL_PLANE_INSTANCE_ID", "bot-01"):
            task = asyncio.create_task(instance._metrics_loop())
            await asyncio.sleep(0.01)
            self.assertEqual(instance.control_plane.send_calls, 0)
            instance.control_plane_registered.set()
            instance.control_plane_metrics_wake.set()
            await asyncio.wait_for(instance.control_plane.first_send.wait(), timeout=0.2)
            self.assertEqual(instance.control_plane.send_calls, 1)
            await asyncio.sleep(0)
            self.assertEqual(instance.control_plane.send_calls, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.done())

    async def test_agent_not_registered_clears_readiness_requests_registration_and_is_not_a_tight_loop(self):
        instance = self.make_bot([MetricsSendResult(status_code=404, retryable=True, agent_not_registered=True)])
        instance.control_plane_registered.set()
        active = self.record()
        claimed = dict(active, status="sending", sending_owner="owner", claim_generation=1)
        with patch.object(bot_module, "get_active_metrics_outbox", return_value=active), \
                patch.object(bot_module, "claim_metrics_outbox", return_value=claimed), \
                patch.object(bot_module, "cleanup_metrics_outbox", return_value=0), \
                patch.object(bot_module, "mark_metrics_outbox_retryable", return_value="transitioned"), \
                patch.object(bot_module, "CONTROL_PLANE_INSTANCE_ID", "bot-01"):
            await instance._process_metrics_tick()
            self.assertFalse(instance.control_plane_registered.is_set())
            self.assertTrue(instance.control_plane_registration_requested.is_set())
            self.assertEqual(instance.control_plane.send_calls, 1)
            await instance._process_metrics_tick()
            self.assertEqual(instance.control_plane.send_calls, 1)

    async def test_control_plane_loop_re_registers_on_production_request_and_wakes_metrics(self):
        instance = self.make_bot()
        instance.control_plane_registration_requested.set()
        with patch.object(bot_module, "CONTROL_PLANE_HEARTBEAT_INTERVAL", 0.2):
            task = asyncio.create_task(instance._control_plane_loop())
            await asyncio.wait_for(asyncio.sleep(0.03), timeout=0.2)
            self.assertGreaterEqual(instance.control_plane.register_calls, 1)
            self.assertTrue(instance.control_plane_registered.is_set())
            self.assertTrue(instance.control_plane_metrics_wake.is_set())
            instance.control_plane_registration_requested.set()
            await asyncio.wait_for(asyncio.sleep(0.03), timeout=0.2)
            self.assertGreaterEqual(instance.control_plane.register_calls, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.done())

    async def test_registration_wake_is_sticky_before_scheduler_wait_and_cancellation_is_clean(self):
        instance = self.make_bot()
        instance.control_plane_registered.set()
        instance.control_plane_metrics_wake.set()
        tick = AsyncMock()
        instance._process_metrics_tick = tick
        with patch.object(bot_module, "CONTROL_PLANE_METRICS_INTERVAL", 10), \
                patch.object(bot_module, "get_active_metrics_outbox_delay", return_value=None):
            task = asyncio.create_task(instance._metrics_loop())
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            tick.assert_awaited_once()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.done())

    async def test_retry_due_wakes_before_normal_interval_after_registration_recovery(self):
        instance = self.make_bot()
        instance.control_plane_registered.set()
        active = self.record(status="retryable")
        claimed = dict(active, status="sending", sending_owner="owner", claim_generation=1)
        claim_calls = 0

        async def claim(*args):
            nonlocal claim_calls
            claim_calls += 1
            return None if claim_calls == 1 else claimed

        with patch.object(bot_module, "get_active_metrics_outbox", return_value=active), \
                patch.object(bot_module, "get_active_metrics_outbox_delay", side_effect=[0.03, None]), \
                patch.object(bot_module, "claim_metrics_outbox", side_effect=claim), \
                patch.object(bot_module, "mark_metrics_outbox_accepted", return_value="transitioned"), \
                patch.object(bot_module, "cleanup_metrics_outbox", return_value=0), \
                patch.object(bot_module, "CONTROL_PLANE_METRICS_INTERVAL", 10), \
                patch.object(bot_module, "CONTROL_PLANE_INSTANCE_ID", "bot-01"):
            instance.control_plane_metrics_wake.set()
            task = asyncio.create_task(instance._metrics_loop())
            await asyncio.wait_for(instance.control_plane.first_send.wait(), timeout=0.3)
            self.assertEqual(instance.control_plane.send_calls, 1)
            self.assertEqual(claim_calls, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_retry_due_already_elapsed_is_processed_on_registration_wake(self):
        instance = self.make_bot()
        instance.control_plane_registered.set()
        active = self.record(status="retryable")
        claimed = dict(active, status="sending", sending_owner="owner", claim_generation=1)
        with patch.object(bot_module, "get_active_metrics_outbox", return_value=active), \
                patch.object(bot_module, "get_active_metrics_outbox_delay", return_value=0), \
                patch.object(bot_module, "claim_metrics_outbox", return_value=claimed), \
                patch.object(bot_module, "mark_metrics_outbox_accepted", return_value="transitioned"), \
                patch.object(bot_module, "cleanup_metrics_outbox", return_value=0), \
                patch.object(bot_module, "CONTROL_PLANE_METRICS_INTERVAL", 10), \
                patch.object(bot_module, "CONTROL_PLANE_INSTANCE_ID", "bot-01"):
            instance.control_plane_metrics_wake.set()
            task = asyncio.create_task(instance._metrics_loop())
            await asyncio.wait_for(instance.control_plane.first_send.wait(), timeout=0.2)
            self.assertEqual(instance.control_plane.send_calls, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_normal_interval_has_one_send_and_no_tight_loop(self):
        instance = self.make_bot()
        instance.control_plane_registered.set()
        active = self.record()
        claimed = dict(active, status="sending", sending_owner="owner", claim_generation=1)
        with patch.object(bot_module, "get_active_metrics_outbox", return_value=active), \
                patch.object(bot_module, "get_active_metrics_outbox_delay", return_value=None), \
                patch.object(bot_module, "claim_metrics_outbox", return_value=claimed), \
                patch.object(bot_module, "mark_metrics_outbox_accepted", return_value="transitioned"), \
                patch.object(bot_module, "cleanup_metrics_outbox", return_value=0), \
                patch.object(bot_module, "CONTROL_PLANE_METRICS_INTERVAL", 0.05), \
                patch.object(bot_module, "CONTROL_PLANE_INSTANCE_ID", "bot-01"):
            task = asyncio.create_task(instance._metrics_loop())
            await asyncio.wait_for(instance.control_plane.first_send.wait(), timeout=0.2)
            await asyncio.sleep(0.01)
            self.assertEqual(instance.control_plane.send_calls, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()