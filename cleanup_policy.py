"""Pure cleanup retry policy helpers."""


def cleanup_retry_delay(failure_number: int, base_delay: int, max_delay: int) -> int:
    """Return bounded exponential backoff in seconds for a failed cleanup."""
    if failure_number < 1:
        raise ValueError("failure_number must be positive")
    if base_delay < 1 or max_delay < base_delay:
        raise ValueError("invalid cleanup retry limits")
    return min(max_delay, base_delay * (2 ** (failure_number - 1)))


def cleanup_is_exhausted(failure_number: int, max_attempts: int) -> bool:
    """Return whether another automatic cleanup retry should be skipped."""
    if failure_number < 0:
        raise ValueError("failure_number cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    return failure_number >= max_attempts