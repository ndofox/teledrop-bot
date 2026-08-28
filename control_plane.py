"""Pure protocol helpers for the TeleDrop control-plane agent contract."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping


PROTOCOL_VERSION = "1"
REGISTER_PATH = "/api/v1/agents/register"
HEARTBEAT_PATH = "/api/v1/agents/heartbeat"
METRICS_PATH = "/api/v1/metrics/aggregates"

# Aggregate metrics contract, versioned independently from the transport protocol.
METRICS_SCHEMA_VERSION = "1"
PRIVACY_MODE_AGGREGATE_ONLY = "aggregate_only"

# Exact body-size bound enforced by the server for metrics requests.
METRICS_MAX_BODY_BYTES = 65536

# Fields permitted in an aggregate metrics payload (strict allowlist).
METRICS_ALLOWED_TOP_FIELDS = frozenset(
    {
        "instance_id",
        "protocol_version",
        "metrics_schema_version",
        "privacy_mode",
        "batch_id",
        "observed_at",
        "current",
        "daily",
    }
)
METRICS_ALLOWED_CURRENT_FIELDS = frozenset(
    {"registered_users", "reachable_users", "active_24h", "active_7d", "active_30d"}
)
METRICS_ALLOWED_DAILY_FIELDS = frozenset(
    {"date_utc", "active_users", "interaction_count", "observed_at"}
)
FORBIDDEN_METRICS_FIELDS = frozenset(
    {
        "users",
        "user_id",
        "username",
        "first_name",
        "last_name",
        "phone",
        "interaction_type",
        "user_cursor_before",
        "user_cursor_after",
        "user_has_more",
        "activity_cursor_before",
        "activity_cursor_after",
        "activity_has_more",
    }
)


def utc_isoformat(value: datetime) -> str:
    """Serialize a datetime as a stable UTC ISO-8601 value."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_iso(value: Any) -> str:
    """Return an ISO string, accepting either a datetime or an already-collapsed string."""
    if isinstance(value, str):
        return value
    return utc_isoformat(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize JSON exactly as it is signed and sent over HTTP."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def request_signature(secret: str, timestamp: str, nonce: str, method: str, path: str, body: str) -> str:
    """Return an HMAC-SHA256 signature for one request."""
    if not secret:
        raise ValueError("secret must not be empty")
    signing_input = "\n".join((timestamp, nonce, method.upper(), path, body)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()


def signed_headers(secret: str, timestamp: str, nonce: str, method: str, path: str, body: str) -> dict[str, str]:
    """Build headers required by the control-plane API."""
    return {
        "Content-Type": "application/json",
        "X-TeleDrop-Protocol": PROTOCOL_VERSION,
        "X-TeleDrop-Timestamp": timestamp,
        "X-TeleDrop-Nonce": nonce,
        "X-TeleDrop-Signature": request_signature(secret, timestamp, nonce, method, path, body),
    }


def registration_payload(
    *, instance_id: str, telegram_bot_id: int, username: str | None, version: str, started_at: datetime
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "telegram_bot_id": telegram_bot_id,
        "username": username,
        "version": version,
        "started_at": utc_isoformat(started_at),
        "protocol_version": PROTOCOL_VERSION,
    }


def heartbeat_payload(
    *, instance_id: str, telegram_bot_id: int, username: str | None, version: str,
    started_at: datetime, uptime_seconds: int, status: str = "online"
) -> dict[str, Any]:
    if status not in {"online", "stopping", "offline"}:
        raise ValueError("invalid control-plane status")
    return {
        "instance_id": instance_id,
        "telegram_bot_id": telegram_bot_id,
        "username": username,
        "version": version,
        "started_at": utc_isoformat(started_at),
        "uptime_seconds": max(0, int(uptime_seconds)),
        "status": status,
        "protocol_version": PROTOCOL_VERSION,
    }


def aggregate_batch_material(
    *, instance_id: str, observed_at: datetime, current: Mapping[str, Any],
    daily: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the canonical batch material minus the self-referential batch_id."""
    current_values = {
        "registered_users": current["registered_users"],
        "reachable_users": current["reachable_users"],
        "active_24h": current["active_24h"],
        "active_7d": current["active_7d"],
        "active_30d": current["active_30d"],
    }
    daily_values = [
        {
            "date_utc": item["date_utc"],
            "active_users": item["active_users"],
            "interaction_count": item["interaction_count"],
            "observed_at": _coerce_iso(item["observed_at"]),
        }
        for item in sorted(daily, key=lambda item: item["date_utc"])
    ]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "privacy_mode": PRIVACY_MODE_AGGREGATE_ONLY,
        "instance_id": instance_id,
        "observed_at": utc_isoformat(observed_at),
        "current": current_values,
        "daily": daily_values,
    }


def aggregate_batch_id(material: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 batch ID for an aggregate snapshot."""
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def aggregate_payload(
    *, instance_id: str, observed_at: datetime, current: Mapping[str, Any],
    daily: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the complete canonical aggregate metrics payload for one snapshot."""
    material = aggregate_batch_material(
        instance_id=instance_id,
        observed_at=observed_at,
        current=current,
        daily=daily,
    )
    daily_values = [
        {
            "date_utc": item["date_utc"],
            "active_users": item["active_users"],
            "interaction_count": item["interaction_count"],
            "observed_at": _coerce_iso(item["observed_at"]),
        }
        for item in sorted(daily, key=lambda item: item["date_utc"])
    ]
    return {
        "instance_id": instance_id,
        "protocol_version": PROTOCOL_VERSION,
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "privacy_mode": PRIVACY_MODE_AGGREGATE_ONLY,
        "batch_id": aggregate_batch_id(material),
        "observed_at": utc_isoformat(observed_at),
        "current": {
            "registered_users": current["registered_users"],
            "reachable_users": current["reachable_users"],
            "active_24h": current["active_24h"],
            "active_7d": current["active_7d"],
            "active_30d": current["active_30d"],
        },
        "daily": daily_values,
    }


def aggregate_batch_material_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the canonical batch material from a parsed payload."""
    current = payload["current"]
    daily = []
    for item in payload["daily"]:
        daily.append(
            {
                "date_utc": item["date_utc"],
                "active_users": item["active_users"],
                "interaction_count": item["interaction_count"],
                "observed_at": item["observed_at"],
            }
        )
    return aggregate_batch_material(
        instance_id=payload["instance_id"],
        observed_at=datetime.fromisoformat(
            payload["observed_at"].replace("Z", "+00:00")
        ).astimezone(timezone.utc),
        current=current,
        daily=daily,
    )