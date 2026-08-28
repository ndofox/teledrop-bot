"""Environment configuration for the standalone control-plane server."""

from dataclasses import dataclass, field
import json
import os
import re
from urllib.parse import urlparse


INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ServerConfig:
    database_url: str
    database_name: str
    agent_secrets: dict[str, str] = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8090
    max_clock_skew_seconds: int = 300
    nonce_ttl_seconds: int = 600
    metrics_daily_max: int = 30
    max_body_bytes: int = 65536
    processing_lease_seconds: int = 300
    batch_retention_days: int = 30
    metrics_cleanup_limit: int = 100


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    if value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}")
    return value


def _parse_agent_secrets(raw: str) -> dict[str, str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CONTROL_PLANE_AGENT_SECRETS_JSON must be valid JSON") from exc
    if not isinstance(values, dict) or not values:
        raise RuntimeError("CONTROL_PLANE_AGENT_SECRETS_JSON must be a non-empty object")
    result = {}
    for instance_id, secret in values.items():
        if not isinstance(instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
            raise RuntimeError("Control-plane agent instance IDs are invalid")
        if not isinstance(secret, str) or len(secret) < 16:
            raise RuntimeError(f"Control-plane secret for {instance_id} must contain at least 16 characters")
        result[instance_id] = secret
    return result


def load_config() -> ServerConfig:
    database_url = _required("CONTROL_PLANE_DATABASE_URL")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.hostname:
        raise RuntimeError("CONTROL_PLANE_DATABASE_URL must be a valid MongoDB URL")
    return ServerConfig(
        database_url=database_url,
        database_name=os.environ.get("CONTROL_PLANE_DATABASE_NAME", "teledrop_control").strip()
        or "teledrop_control",
        agent_secrets=_parse_agent_secrets(_required("CONTROL_PLANE_AGENT_SECRETS_JSON")),
        host=os.environ.get("CONTROL_PLANE_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_positive_int("CONTROL_PLANE_PORT", 8090, minimum=1, maximum=65535),
        max_clock_skew_seconds=_positive_int("CONTROL_PLANE_MAX_CLOCK_SKEW", 300, minimum=1, maximum=86400),
        nonce_ttl_seconds=_positive_int("CONTROL_PLANE_NONCE_TTL", 600, minimum=30, maximum=86400),
        metrics_daily_max=_positive_int("CONTROL_PLANE_METRICS_DAILY_MAX", 30, minimum=1, maximum=90),
        max_body_bytes=_positive_int("CONTROL_PLANE_REQUEST_MAX_BODY_BYTES", 65536, minimum=1024, maximum=1048576),
        processing_lease_seconds=_positive_int("CONTROL_PLANE_METRICS_PROCESSING_LEASE_SECONDS", 300, minimum=10, maximum=3600),
        batch_retention_days=_positive_int("CONTROL_PLANE_METRICS_BATCH_RETENTION_DAYS", 30, minimum=1, maximum=365),
        metrics_cleanup_limit=_positive_int("CONTROL_PLANE_METRICS_CLEANUP_LIMIT", 100, minimum=1, maximum=100000),
    )