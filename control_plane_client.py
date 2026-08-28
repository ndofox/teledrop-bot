"""Optional async agent client for the central TeleDrop control plane."""

import asyncio
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import aiohttp

from control_plane import HEARTBEAT_PATH, METRICS_PATH, REGISTER_PATH, canonical_json, signed_headers


@dataclass(frozen=True)
class MetricsSendResult:
    """Classify one aggregate metrics send for retry/idempotency decisions."""

    status_code: int
    ok: bool = False
    retryable: bool = False
    permanent: bool = False
    duplicate: bool = False
    agent_not_registered: bool = False


class ControlPlaneClient:
    """Send signed registration and heartbeat requests without blocking Pyrogram."""

    def __init__(self, base_url: str, instance_id: str, secret: str, timeout_seconds: int):
        self.base_url = base_url.rstrip("/")
        self.instance_id = instance_id
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.instance_id and self._secret)

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def post(self, path: str, payload: Mapping[str, Any]) -> bool:
        if not self.enabled:
            return False
        body = canonical_json(payload)
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        headers = signed_headers(self._secret, timestamp, nonce, "POST", path, body)
        try:
            session = await self._session_or_create()
            async with session.post(self.base_url + path, data=body.encode("utf-8"), headers=headers) as response:
                if response.status < 200 or response.status >= 300:
                    self.last_error = f"HTTP {response.status}"
                    return False
            self.last_success_at = datetime.now(timezone.utc)
            self.last_error = None
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.last_error = "request failed"
            return False

    async def send_metrics(self, body: str) -> MetricsSendResult:
        """Send an exact canonical aggregate body and classify the outcome."""
        if not self.enabled:
            return MetricsSendResult(status_code=0, permanent=True)
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        headers = signed_headers(self._secret, timestamp, nonce, "POST", METRICS_PATH, body)
        try:
            session = await self._session_or_create()
            async with session.post(
                self.base_url + METRICS_PATH, data=body.encode("utf-8"), headers=headers
            ) as response:
                status = response.status
                ok = False
                duplicate = False
                try:
                    data = await response.json()
                    if isinstance(data, dict):
                        ok = data.get("status") in ("accepted", "duplicate")
                        duplicate = data.get("status") == "duplicate"
                        agent_not_registered = data.get("error_code") == "agent_not_registered"
                    else:
                        agent_not_registered = False
                except (ValueError, TypeError):
                    agent_not_registered = False
                if ok:
                    self.last_success_at = datetime.now(timezone.utc)
                    self.last_error = None
                    return MetricsSendResult(status_code=status, ok=True, duplicate=duplicate)
                self.last_error = f"HTTP {status}"
                if status == 404 and agent_not_registered:
                    return MetricsSendResult(status_code=status, retryable=True, agent_not_registered=True)
                if status in (429, 500, 502, 503, 504):
                    return MetricsSendResult(status_code=status, retryable=True)
                return MetricsSendResult(status_code=status, permanent=True)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.last_error = "request failed"
            return MetricsSendResult(status_code=0, retryable=True)

    async def register(self, payload: Mapping[str, Any]) -> bool:
        return await self.post(REGISTER_PATH, payload)

    async def heartbeat(self, payload: Mapping[str, Any]) -> bool:
        return await self.post(HEARTBEAT_PATH, payload)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "instance_id": self.instance_id or None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }