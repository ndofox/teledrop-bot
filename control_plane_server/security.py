"""Server-side validation for signed TeleDrop agent requests."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import re
import time
from typing import Any, Mapping

from control_plane import PROTOCOL_VERSION, canonical_json, request_signature


SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_NONCE_LENGTH = 128


@dataclass(frozen=True)
class AuthenticationResult:
    instance_id: str
    nonce: str
    timestamp: int


class RequestRejected(Exception):
    """Expected authentication or request-validation failure."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def parse_json_body(raw_body: bytes) -> tuple[str, dict[str, Any]]:
    try:
        body = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestRejected("invalid request body", 400) from exc
    try:
        import json

        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise RequestRejected("invalid request body", 400) from exc
    if not isinstance(payload, dict) or canonical_json(payload) != body:
        raise RequestRejected("invalid request body", 400)
    return body, payload


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name, "").strip()
    if not value:
        raise RequestRejected("missing authentication header")
    return value


async def authenticate_request(
    *, headers: Mapping[str, str], method: str, path: str, body: str,
    credential_store, repository, max_clock_skew_seconds: int, nonce_ttl_seconds: int,
    now: int | None = None,
) -> AuthenticationResult:
    if _header(headers, "X-TeleDrop-Protocol") != PROTOCOL_VERSION:
        raise RequestRejected("unsupported protocol")
    timestamp_raw = _header(headers, "X-TeleDrop-Timestamp")
    nonce = _header(headers, "X-TeleDrop-Nonce")
    signature = _header(headers, "X-TeleDrop-Signature")
    if len(nonce) > MAX_NONCE_LENGTH or not re.fullmatch(r"[A-Za-z0-9_-]+", nonce):
        raise RequestRejected("invalid authentication header")
    if not timestamp_raw.isdigit():
        raise RequestRejected("invalid authentication header")
    timestamp = int(timestamp_raw)
    reference = int(time.time()) if now is None else now
    if abs(reference - timestamp) > max_clock_skew_seconds:
        raise RequestRejected("stale request")
    if not SIGNATURE_PATTERN.fullmatch(signature):
        raise RequestRejected("invalid authentication header")

    import json

    try:
        instance_id = json.loads(body)["instance_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestRejected("invalid request body", 400) from exc
    if not isinstance(instance_id, str):
        raise RequestRejected("invalid request body", 400)
    secret = credential_store.secret_for(instance_id)
    if secret is None:
        raise RequestRejected("unauthorized")
    expected = request_signature(secret, timestamp_raw, nonce, method, path, body)
    if not hmac.compare_digest(expected, signature):
        raise RequestRejected("unauthorized")
    if not await repository.consume_nonce(instance_id, nonce, nonce_ttl_seconds, reference):
        raise RequestRejected("replayed request")
    return AuthenticationResult(instance_id=instance_id, nonce=nonce, timestamp=timestamp)


def parse_started_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("started_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("started_at must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        raise ValueError("started_at must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_common_payload(payload: Mapping[str, Any]) -> None:
    from control_plane_server.config import INSTANCE_ID_PATTERN

    instance_id = payload.get("instance_id")
    bot_id = payload.get("telegram_bot_id")
    version = payload.get("version")
    protocol_version = payload.get("protocol_version")
    if not isinstance(instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise ValueError("invalid instance_id")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or bot_id < 1:
        raise ValueError("invalid telegram_bot_id")
    if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 64):
        raise ValueError("invalid version")
    if protocol_version != PROTOCOL_VERSION:
        raise ValueError("invalid protocol_version")
    username = payload.get("username")
    if username is not None and (not isinstance(username, str) or len(username) > 64):
        raise ValueError("invalid username")
    parse_started_at(payload.get("started_at"))


def validate_registration_payload(payload: Mapping[str, Any]) -> None:
    validate_common_payload(payload)


def validate_heartbeat_payload(payload: Mapping[str, Any]) -> None:
    validate_common_payload(payload)
    uptime = payload.get("uptime_seconds")
    status = payload.get("status")
    if not isinstance(uptime, int) or isinstance(uptime, bool) or uptime < 0:
        raise ValueError("invalid uptime_seconds")
    if status not in {"online", "stopping", "offline"}:
        raise ValueError("invalid status")