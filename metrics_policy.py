"""Pure policy helpers for aggregate control-plane metrics.

These helpers keep retry/backoff/retention decisions testable offline without
importing Telegram, MongoDB, or network modules.
"""

from datetime import datetime, timezone
import random


# Outbox status values persisted locally on the agent. A blocked_auth record
# keeps the single-active outbox slot so no new snapshot is produced while a
# credentials/configuration failure is retried with a bounded interval.
ACTIVE_OUTBOX_STATES = frozenset({"pending", "sending", "retryable", "blocked_auth"})

# Stable value stored on active outbox records so the unique partial index
# `(instance_id, active_slot)` can enforce a single-active-batch invariant.
OUTBOX_ACTIVE_SLOT = "sync"


def is_active_metrics_outbox(status: str) -> bool:
    """Return whether a local outbox record still needs delivery."""
    return status in ACTIVE_OUTBOX_STATES


def metrics_auth_failure(error_class: str) -> bool:
    """Return whether an error class represents a permanent auth/config rejection.

    A permanent 401/403 is treated as a quarantined ``blocked_auth`` state rather
    than a normal permanent failure so it does not produce a fresh permanent
    record on every metrics interval.
    """
    return error_class in ("permanent_http_401", "permanent_http_403")


# Cap the exponent so extreme attempt counts never build huge intermediate
# integers (Python big ints are not an error, but we keep the work bounded).
_EXPONENT_CAP = 40


def _exponential_base(base_delay: int, max_delay: int, attempts: int) -> int:
    shift = min(attempts - 1, _EXPONENT_CAP)
    scaled = base_delay << shift
    return min(max_delay, scaled)


def metrics_retry_delay(attempts: int, base_delay: int, max_delay: int) -> int:
    """Return bounded exponential backoff in seconds for a failed metrics send."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if base_delay < 1 or max_delay < base_delay:
        raise ValueError("invalid metrics retry limits")
    return _exponential_base(base_delay, max_delay, attempts)


def metrics_retry_delay_jittered(
    attempts: int,
    base_delay: int,
    max_delay: int,
    *,
    jitter_ratio: float = 0.25,
    rng: "random.Random | None" = None,
) -> int:
    """Return a bounded exponential backoff with bounded equal jitter.

    The base backoff follows ``metrics_retry_delay`` and the final delay is
    ``min(max_delay, base + randint(0, floor(base * jitter_ratio)))`` so the
    total never exceeds ``max_delay``. An injected ``rng`` keeps tests
    deterministic without touching global random state.
    """
    if jitter_ratio is None or jitter_ratio <= 0:
        return metrics_retry_delay(attempts, base_delay, max_delay)
    base = metrics_retry_delay(attempts, base_delay, max_delay)
    span = int(base * jitter_ratio)
    if span <= 0:
        return base
    if rng is None:
        rng = random.Random()
    return min(max_delay, base + rng.randrange(0, span + 1))


def metrics_retry_exhausted(attempts: int, max_attempts: int) -> bool:
    """Return whether an automatic metrics retry should be skipped."""
    if attempts < 0:
        raise ValueError("attempts cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    return attempts >= max_attempts


def utc_now() -> datetime:
    """Return a tz-aware UTC now value (matches telemetry conventions)."""
    return datetime.now(timezone.utc)


def coerce_daily_days(value: int, *, default: int = 30, minimum: int = 1, maximum: int = 90) -> int:
    """Return a validated daily-history window length for one snapshot."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value