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


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1")
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
        port=_positive_int("CONTROL_PLANE_PORT", 8090),
        max_clock_skew_seconds=_positive_int("CONTROL_PLANE_MAX_CLOCK_SKEW", 300),
        nonce_ttl_seconds=_positive_int("CONTROL_PLANE_NONCE_TTL", 600),
    )