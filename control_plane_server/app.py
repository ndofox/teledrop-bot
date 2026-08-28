"""aiohttp application factory for the TeleDrop control-plane server."""

from datetime import datetime, timezone
import logging

from aiohttp import web

from control_plane import HEARTBEAT_PATH, METRICS_PATH, REGISTER_PATH, PROTOCOL_VERSION
from control_plane_server.credentials import CredentialStore
from control_plane_server.security import (
    RequestRejected,
    authenticate_request,
    parse_json_body,
    validate_heartbeat_payload,
    validate_metrics_payload,
    validate_registration_payload,
)


log = logging.getLogger(__name__)


def _error(message: str, status: int, *, error_code: str | None = None) -> web.Response:
    body = {"error": message}
    if error_code is not None:
        body["error_code"] = error_code
    return web.json_response(body, status=status)


async def _read_bounded_body(request: web.Request, max_body_bytes: int) -> bytes | None:
    """Read at most the configured body limit without buffering an oversized body."""
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > max_body_bytes:
                return None
        except ValueError:
            pass

    chunks = []
    total = 0
    while total <= max_body_bytes:
        chunk = await request.content.read(min(8192, max_body_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_body_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(*, repository, credential_store: CredentialStore, max_clock_skew_seconds: int = 300,
               nonce_ttl_seconds: int = 600, metrics_daily_max: int = 30, max_body_bytes: int = 65536,
               processing_lease_seconds: int = 300, batch_retention_days: int = 30,
               metrics_cleanup_limit: int = 100, clock=None) -> web.Application:
    app = web.Application()
    clock_fn = clock or (lambda: datetime.now(timezone.utc))

    async def healthz(request: web.Request) -> web.Response:
        database_ok = None
        try:
            database_ok = await repository.ping()
        except Exception:
            log.exception("Control-plane database health check failed")
        status = "ok" if database_ok else "degraded"
        return web.json_response(
            {
                "service": "TeleDrop Control Plane",
                "status": status,
                "protocol_version": PROTOCOL_VERSION,
                "database": "ok" if database_ok else "unavailable",
            },
            status=200 if database_ok else 503,
        )

    async def process_agent_request(request: web.Request, *, heartbeat: bool = False, metrics: bool = False) -> web.Response:
        if metrics:
            raw_body = await _read_bounded_body(request, max_body_bytes)
        else:
            raw_body = await request.read()
        if metrics and raw_body is None:
            return _error("request body too large", 413)
        try:
            body, payload = parse_json_body(raw_body)
            await authenticate_request(
                headers=request.headers,
                method=request.method,
                path=request.path,
                body=body,
                credential_store=credential_store,
                repository=repository,
                max_clock_skew_seconds=max_clock_skew_seconds,
                nonce_ttl_seconds=nonce_ttl_seconds,
                now=int(clock_fn().timestamp()),
            )
            if metrics:
                validate_metrics_payload(payload, max_daily=metrics_daily_max, now=clock_fn())
            elif heartbeat:
                validate_heartbeat_payload(payload)
            else:
                validate_registration_payload(payload)
        except RequestRejected as exc:
            return _error(str(exc), exc.status)
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception:
            log.exception("Control-plane agent request failed")
            return _error("internal server error", 500)

        now = clock_fn()
        secret_hash = credential_store.secret_hash(payload["instance_id"])
        if secret_hash is None:
            return _error("unauthorized", 401)
        if metrics:
            if not await repository.agent_secret_matches(payload["instance_id"], secret_hash):
                return _error("agent is not registered", 404, error_code="agent_not_registered")
            result = await repository.ingest_metrics(
                payload, now, processing_lease_seconds=processing_lease_seconds
            )
            if result.status == "accepted":
                return web.json_response({"status": "accepted", "batch_id": payload["batch_id"]})
            if result.status == "duplicate":
                return web.json_response({"status": "duplicate", "batch_id": payload["batch_id"]})
            if result.status == "retryable":
                return web.json_response({"status": "retryable"}, status=503)
            return web.json_response({"status": "permanent_conflict"}, status=409)
        if heartbeat:
            if not await repository.agent_secret_matches(payload["instance_id"], secret_hash):
                return _error("agent is not registered", 404, error_code="agent_not_registered")
            accepted = await repository.heartbeat_agent(payload, now)
            if not accepted:
                return _error("agent is not registered", 404, error_code="agent_not_registered")
            return web.json_response({"status": "accepted"})
        await repository.register_agent(payload, secret_hash, now)
        return web.json_response({"status": "registered"})

    async def register(request: web.Request) -> web.Response:
        return await process_agent_request(request, heartbeat=False)

    async def heartbeat(request: web.Request) -> web.Response:
        return await process_agent_request(request, heartbeat=True)

    async def metrics(request: web.Request) -> web.Response:
        return await process_agent_request(request, metrics=True)

    app.router.add_get("/healthz", healthz)
    app.router.add_post(REGISTER_PATH, register)
    app.router.add_post(HEARTBEAT_PATH, heartbeat)
    app.router.add_post(METRICS_PATH, metrics)
    return app