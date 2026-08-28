"""Entry point for the standalone TeleDrop control-plane server."""

import asyncio
import logging

from aiohttp import web

from control_plane_server.app import create_app
from control_plane_server.config import load_config
from control_plane_server.credentials import CredentialStore
from control_plane_server.repository import ControlPlaneRepository


async def _batch_cleanup_loop(repository, retention_days: int, limit: int) -> None:
    while True:
        try:
            await repository.cleanup_metrics_batches(retention_days, limit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("Metrics batch cleanup failed", exc_info=True)
        await asyncio.sleep(3600)


async def _run() -> None:
    config = load_config()
    repository = ControlPlaneRepository.from_config(config)
    await repository.ensure_indexes()
    app = create_app(
        repository=repository,
        credential_store=CredentialStore(config.agent_secrets),
        max_clock_skew_seconds=config.max_clock_skew_seconds,
        nonce_ttl_seconds=config.nonce_ttl_seconds,
        metrics_daily_max=config.metrics_daily_max,
        max_body_bytes=config.max_body_bytes,
        processing_lease_seconds=config.processing_lease_seconds,
        batch_retention_days=config.batch_retention_days,
        metrics_cleanup_limit=config.metrics_cleanup_limit,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()
    cleanup_task = asyncio.create_task(
        _batch_cleanup_loop(repository, config.batch_retention_days, config.metrics_cleanup_limit)
    )
    logging.getLogger(__name__).info("Control-plane server listening on %s:%s", config.host, config.port)
    try:
        await asyncio.Event().wait()
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await runner.cleanup()
        await repository.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()