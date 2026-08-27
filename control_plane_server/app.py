"""aiohttp application factory for the TeleDrop control-plane server."""

from datetime import datetime, timezone
import logging

from aiohttp import web

from control_plane import HEARTBEAT_PATH, REGISTER_PATH, PROTOCOL_VERSION
from control_plane_server.credentials import CredentialStore
from control_plane_server.security import (
    RequestRejected,
    authenticate_request,
    parse_json_body,
    validate_heartbeat_payload,
    validate_registration_payload,
)


log = logging.getLogger(__name__)


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def create_app(*, repository, credential_store: CredentialStore, max_clock_skew_seconds: int = 300,
               nonce_ttl_seconds: int = 600, clock=None) -> web.Application:
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

    async def process_agent_request(request: web.Request, *, heartbeat: bool) -> web.Response:
        raw_body = await request.read()
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
            if heartbeat:
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
        if heartbeat:
            if not await repository.agent_secret_matches(payload["instance_id"], secret_hash):
                return _error("agent is not registered", 404)
            accepted = await repository.heartbeat_agent(payload, now)
            if not accepted:
                return _error("agent is not registered", 404)
            return web.json_response({"status": "accepted"})
        await repository.register_agent(payload, secret_hash, now)
        return web.json_response({"status": "registered"})

    async def register(request: web.Request) -> web.Response:
        return await process_agent_request(request, heartbeat=False)

    async def heartbeat(request: web.Request) -> web.Response:
        return await process_agent_request(request, heartbeat=True)

    app.router.add_get("/healthz", healthz)
    app.router.add_post(REGISTER_PATH, register)
    app.router.add_post(HEARTBEAT_PATH, heartbeat)
    return app