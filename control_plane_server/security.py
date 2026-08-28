"""Server-side validation for signed TeleDrop agent requests."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import re
import time
from typing import Any, Mapping

from control_plane import (
    FORBIDDEN_METRICS_FIELDS,
    METRICS_ALLOWED_CURRENT_FIELDS,
    METRICS_ALLOWED_DAILY_FIELDS,
    METRICS_ALLOWED_TOP_FIELDS,
    METRICS_SCHEMA_VERSION,
    PRIVACY_MODE_AGGREGATE_ONLY,
    PROTOCOL_VERSION,
    aggregate_batch_id,
    aggregate_batch_material_from_payload,
    canonical_json,
    request_signature,
)
from control_plane_server.config import INSTANCE_ID_PATTERN


SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_NONCE_LENGTH = 128
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _validate_metric_datetime(value: Any, name: str) -> None:
    try:
        parse_started_at(value)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc


def _validate_non_negative_count(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid {name}")


def _validate_fields_only(value: Mapping[str, Any], allowed: frozenset, name: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"unknown {name} fields")


def _validate_utc_date(value: Any, name: str) -> None:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {name}")
    from datetime import datetime as _dt
    import time as _time

    try:
        _dt.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc


# Metrics observed timestamps must not be unreasonably far in the future.
MAX_METRIC_FUTURE_SECONDS = 900


def validate_metrics_payload(payload: Mapping[str, Any], *, max_daily: int = 30,
                             now: datetime | None = None) -> None:
    from datetime import timedelta

    unknown = set(payload) - set(METRICS_ALLOWED_TOP_FIELDS)
    if unknown:
        raise ValueError("unknown fields")
    forbidden = FORBIDDEN_METRICS_FIELDS & set(payload)
    if forbidden:
        raise ValueError("forbidden fields")

    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise ValueError("invalid instance_id")
    if not isinstance(payload.get("batch_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", payload["batch_id"]):
        raise ValueError("invalid batch_id")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("invalid protocol_version")
    if payload.get("metrics_schema_version") != METRICS_SCHEMA_VERSION:
        raise ValueError("unsupported metrics schema")
    if payload.get("privacy_mode") != PRIVACY_MODE_AGGREGATE_ONLY:
        raise ValueError("unsupported privacy mode")

    try:
        observed_at = parse_started_at(payload.get("observed_at"))
    except ValueError as exc:
        raise ValueError("invalid observed_at") from exc
    if now is not None and observed_at > now + timedelta(seconds=MAX_METRIC_FUTURE_SECONDS):
        raise ValueError("invalid observed_at")

    current = payload.get("current")
    if not isinstance(current, dict):
        raise ValueError("invalid current")
    _validate_fields_only(current, METRICS_ALLOWED_CURRENT_FIELDS, "current")
    for field in METRICS_ALLOWED_CURRENT_FIELDS:
        _validate_non_negative_count(current.get(field), field)

    daily = payload.get("daily")
    if not isinstance(daily, list):
        raise ValueError("invalid daily")
    if not daily:
        raise ValueError("invalid daily")
    if len(daily) > max_daily:
        raise ValueError("daily is too large")
    seen_dates: set[str] = set()
    for item in daily:
        if not isinstance(item, dict) or set(item) != set(METRICS_ALLOWED_DAILY_FIELDS):
            raise ValueError("invalid daily entry")
        _validate_utc_date(item["date_utc"], "date_utc")
        if item["date_utc"] in seen_dates:
            raise ValueError("duplicate daily entry")
        seen_dates.add(item["date_utc"])
        _validate_non_negative_count(item["active_users"], "active_users")
        _validate_non_negative_count(item["interaction_count"], "interaction_count")
        _validate_metric_datetime(item["observed_at"], "observed_at")

    expected_batch_id = aggregate_batch_id(aggregate_batch_material_from_payload(payload))
    if not hmac.compare_digest(expected_batch_id, payload["batch_id"]):
        raise ValueError("invalid batch_id")