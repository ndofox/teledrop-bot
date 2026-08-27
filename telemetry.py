"""Pure helpers for local user activity telemetry."""

from datetime import datetime, timedelta, timezone
from typing import Mapping


ACTIVITY_WINDOWS = (
    ("active_24h", 1),
    ("active_7d", 7),
    ("active_30d", 30),
)


def activity_cutoff(days: int, *, now: datetime | None = None) -> datetime:
    """Return the UTC cutoff used to classify a user as active."""
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("days must be a positive integer")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference - timedelta(days=days)


def is_active(last_seen_at: datetime | None, days: int, *, now: datetime | None = None) -> bool:
    """Return whether a user's last activity falls inside the requested window."""
    if last_seen_at is None:
        return False
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    return last_seen_at >= activity_cutoff(days, now=now)


def format_user_statistics(statistics: Mapping[str, int]) -> str:
    """Format database counters for the private admin response."""
    return (
        "<b>User telemetry</b>\n"
        f"Registered: {int(statistics.get('registered', 0))}\n"
        f"Active 24 jam: {int(statistics.get('active_24h', 0))}\n"
        f"Active 7 hari: {int(statistics.get('active_7d', 0))}\n"
        f"Active 30 hari: {int(statistics.get('active_30d', 0))}"
    )
