import unittest
from datetime import datetime, timezone

from control_plane import (
    METRICS_SCHEMA_VERSION,
    PRIVACY_MODE_AGGREGATE_ONLY,
    FORBIDDEN_METRICS_FIELDS,
    aggregate_batch_id,
    aggregate_batch_material,
    aggregate_batch_material_from_payload,
    aggregate_payload,
    canonical_json,
)
from metrics_policy import is_active_metrics_outbox, metrics_retry_delay, metrics_retry_exhausted


class AggregateMetricsContractTests(unittest.TestCase):
    def build(self):
        observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        daily = [
            {
                "date_utc": "2026-08-27",
                "active_users": 100,
                "interaction_count": 700,
                "observed_at": observed_at,
            }
        ]
        current = {
            "registered_users": 1000,
            "reachable_users": 900,
            "active_24h": 120,
            "active_7d": 300,
            "active_30d": 550,
        }
        return observed_at, current, daily

    def test_aggregate_payload_has_expected_fields_and_no_identifiers(self):
        observed_at, current, daily = self.build()
        payload = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        self.assertEqual(payload["metrics_schema_version"], METRICS_SCHEMA_VERSION)
        self.assertEqual(payload["privacy_mode"], PRIVACY_MODE_AGGREGATE_ONLY)
        self.assertNotIn("users", payload)
        self.assertNotIn("user_id", payload)
        self.assertIn("batch_id", payload)
        body = canonical_json(payload)
        for forbidden in FORBIDDEN_METRICS_FIELDS:
            self.assertNotIn('"%s"' % forbidden, body)

    def test_batch_id_is_deterministic_and_content_sensitive(self):
        observed_at, current, daily = self.build()
        first = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        second = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        self.assertEqual(first["batch_id"], second["batch_id"])
        changed_current = dict(current, registered_users=2000)
        changed = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=changed_current, daily=daily)
        self.assertNotEqual(first["batch_id"], changed["batch_id"])

    def test_batch_material_has_no_batch_id(self):
        observed_at, current, daily = self.build()
        material = aggregate_batch_material(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        self.assertNotIn("batch_id", material)
        self.assertNotIn("batch_id", canonical_json(material))

    def test_server_recomputes_batch_id_from_payload(self):
        observed_at, current, daily = self.build()
        payload = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        expected = aggregate_batch_id(aggregate_batch_material_from_payload(payload))
        self.assertEqual(expected, payload["batch_id"])

    def test_metrics_policy_helpers(self):
        self.assertTrue(is_active_metrics_outbox("pending"))
        self.assertTrue(is_active_metrics_outbox("sending"))
        self.assertTrue(is_active_metrics_outbox("retryable"))
        self.assertFalse(is_active_metrics_outbox("accepted"))
        self.assertFalse(is_active_metrics_outbox("permanent_failure"))
        self.assertEqual(metrics_retry_delay(1, 60, 3600), 60)
        self.assertGreaterEqual(metrics_retry_delay(3, 60, 3600), 60)
        self.assertTrue(metrics_retry_exhausted(5, 5))
        self.assertFalse(metrics_retry_exhausted(4, 5))

    def test_jitter_is_deterministic_and_bounded(self):
        from metrics_policy import metrics_retry_delay_jittered
        import random

        fixed = random.Random(7)
        first = metrics_retry_delay_jittered(1, 60, 3600, rng=fixed)
        second = metrics_retry_delay_jittered(1, 60, 3600, rng=random.Random(7))
        self.assertEqual(first, second)  # deterministic for the same injected RNG
        self.assertGreaterEqual(first, 60)
        self.assertLessEqual(first, 3600)

    def test_jitter_never_exceeds_max_or_negative(self):
        from metrics_policy import metrics_retry_delay_jittered
        import random

        for attempt in (1, 2, 5, 20):
            for seed in range(5):
                value = metrics_retry_delay_jittered(attempt, 60, 3600, rng=random.Random(seed))
                self.assertLessEqual(value, 3600)
                self.assertGreaterEqual(value, 0)

    def test_extreme_attempts_do_not_overflow(self):
        from metrics_policy import metrics_retry_delay, metrics_retry_delay_jittered

        self.assertEqual(metrics_retry_delay(10**7, 60, 3600), 3600)
        self.assertLessEqual(metrics_retry_delay_jittered(10**7, 60, 3600), 3600)

    def test_retry_limits_reject_max_below_base(self):
        with self.assertRaises(ValueError):
            metrics_retry_delay(1, 60, 30)

    def test_auth_failure_classifier(self):
        from metrics_policy import metrics_auth_failure

        self.assertTrue(metrics_auth_failure("permanent_http_401"))
        self.assertTrue(metrics_auth_failure("permanent_http_403"))
        self.assertFalse(metrics_auth_failure("permanent_http_400"))
        self.assertFalse(metrics_auth_failure("permanent_http_409"))
        self.assertFalse(metrics_auth_failure("retryable_http_503"))

    def test_blocked_auth_is_an_active_outbox_state(self):
        from metrics_policy import is_active_metrics_outbox

        self.assertTrue(is_active_metrics_outbox("blocked_auth"))


if __name__ == "__main__":
    unittest.main()