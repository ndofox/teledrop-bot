"""Entry point for the standalone TeleDrop control-plane server."""

import asyncio
import logging

from aiohttp import web

from control_plane_server.app import create_app
from control_plane_server.config import load_config
from control_plane_server.credentials import CredentialStore
from control_plane_server.repository import ControlPlaneRepository


async def _run() -> None:
    config = load_config()
    repository = ControlPlaneRepository.from_config(config)
    await repository.ensure_indexes()
    app = create_app(
        repository=repository,
        credential_store=CredentialStore(config.agent_secrets),
        max_clock_skew_seconds=config.max_clock_skew_seconds,
        nonce_ttl_seconds=config.nonce_ttl_seconds,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()
    logging.getLogger(__name__).info("Control-plane server listening on %s:%s", config.host, config.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await repository.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()