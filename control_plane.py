"""Pure protocol helpers for the TeleDrop control-plane agent contract."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping


PROTOCOL_VERSION = "1"
REGISTER_PATH = "/api/v1/agents/register"
HEARTBEAT_PATH = "/api/v1/agents/heartbeat"


def utc_isoformat(value: datetime) -> str:
    """Serialize a datetime as a stable UTC ISO-8601 value."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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