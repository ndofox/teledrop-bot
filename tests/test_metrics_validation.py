import unittest
from datetime import datetime, timezone

from control_plane import aggregate_payload
from control_plane_server.security import validate_metrics_payload


class AggregateValidationTests(unittest.TestCase):
    def build(self):
        observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
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
                "observed_at": "2026-08-27T12:00:00Z",
            }
        ]
        return aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)

    def test_valid_aggregate_passes(self):
        validate_metrics_payload(self.build(), max_daily=30)

    def test_raw_user_id_is_rejected(self):
        payload = self.build()
        payload["user_id"] = 12345
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_users_array_is_rejected(self):
        payload = self.build()
        payload["users"] = []
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_unknown_top_field_is_rejected(self):
        payload = self.build()
        payload["unexpected"] = True
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_negative_count_is_rejected(self):
        payload = self.build()
        payload["current"]["active_24h"] = -1
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_boolean_count_is_rejected(self):
        payload = self.build()
        payload["current"]["active_24h"] = True
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_batch_id_mismatch_is_rejected(self):
        payload = self.build()
        payload["batch_id"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_unsupported_privacy_mode_is_rejected(self):
        payload = self.build()
        payload["privacy_mode"] = "raw_users"
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_invalid_calendar_date_is_rejected(self):
        payload = self.build()
        payload["daily"][0]["date_utc"] = "2026-13-40"
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_oversized_daily_is_rejected(self):
        observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
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
                    "observed_at": "2026-08-27T12:00:00Z",
                }
            )
        payload = aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)

    def test_unreasonable_future_timestamp_is_rejected(self):
        payload = self.build()
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        payload["observed_at"] = "2026-12-01T00:00:00Z"
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30, now=now)

    def test_current_interaction_count_is_not_allowed(self):
        payload = self.build()
        payload["current"]["interaction_count"] = 999
        with self.assertRaises(ValueError):
            validate_metrics_payload(payload, max_daily=30)


if __name__ == "__main__":
    unittest.main()