import unittest
from datetime import datetime, timedelta, timezone

from telemetry import activity_cutoff, format_user_statistics, is_active


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def test_activity_cutoff_uses_utc_rolling_window(self):
        self.assertEqual(
            activity_cutoff(1, now=self.now),
            datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )

    def test_activity_boundary_is_inclusive(self):
        self.assertTrue(is_active(self.now - timedelta(days=7), 7, now=self.now))
        self.assertFalse(is_active(self.now - timedelta(days=7, seconds=1), 7, now=self.now))
        self.assertFalse(is_active(None, 7, now=self.now))

    def test_naive_legacy_datetime_is_treated_as_utc(self):
        last_seen = datetime(2026, 8, 27, 11, 0)
        self.assertTrue(is_active(last_seen, 1, now=self.now))

    def test_formats_all_user_counters(self):
        text = format_user_statistics(
            {"registered": 100, "active_24h": 10, "active_7d": 40, "active_30d": 75}
        )
        self.assertIn("Registered: 100", text)
        self.assertIn("Active 24 jam: 10", text)
        self.assertIn("Active 7 hari: 40", text)
        self.assertIn("Active 30 hari: 75", text)


if __name__ == "__main__":
    unittest.main()