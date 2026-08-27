import asyncio
from datetime import datetime, timezone

from aiohttp import web
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from config import (
    API_HASH,
    APP_ID,
    APP_VERSION,
    CHANNEL_ID,
    CLEANUP_INTERVAL,
    CONTROL_PLANE_HEARTBEAT_INTERVAL,
    CONTROL_PLANE_INSTANCE_ID,
    CONTROL_PLANE_SECRET,
    CONTROL_PLANE_TIMEOUT,
    CONTROL_PLANE_URL,
    FORCE_SUB_CHANNEL1,
    FORCE_SUB_CHANNEL2,
    FORCE_SUB_CHANNEL3,
    FORCE_SUB_CHANNEL4,
    LOGGER,
    OWNER_ID,
    PORT,
    TG_BOT_TOKEN,
    TG_BOT_WORKERS,
    TELEGRAM_API_TIMEOUT,
)
from control_plane import heartbeat_payload, registration_payload, utc_isoformat
from control_plane_client import ControlPlaneClient
from database.database import due_deliveries, ensure_indexes, mark_delivery_attempt, mark_delivery_deleted
from plugins import web_server


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="Bot",
            api_hash=API_HASH,
            api_id=APP_ID,
            plugins={"root": "plugins"},
            workers=TG_BOT_WORKERS,
            bot_token=TG_BOT_TOKEN,
        )
        self.LOGGER = LOGGER
        self.cleanup_task = None
        self.web_runner = None
        self.control_plane_task = None
        self.control_plane = ControlPlaneClient(
            CONTROL_PLANE_URL,
            CONTROL_PLANE_INSTANCE_ID,
            CONTROL_PLANE_SECRET,
            CONTROL_PLANE_TIMEOUT,
        )
        self.telegram_bot_id = None

    async def start(self):
        await super().start()
        bot_user = await asyncio.wait_for(self.get_me(), timeout=TELEGRAM_API_TIMEOUT)
        self.uptime = datetime.now(timezone.utc)
        self.telegram_bot_id = bot_user.id
        await ensure_indexes()

        for index, channel_id in enumerate(
            [FORCE_SUB_CHANNEL1, FORCE_SUB_CHANNEL2, FORCE_SUB_CHANNEL3, FORCE_SUB_CHANNEL4], 1
        ):
            if not channel_id:
                continue
            try:
                chat = await asyncio.wait_for(
                    self.get_chat(channel_id), timeout=TELEGRAM_API_TIMEOUT
                )
                link = chat.invite_link or await asyncio.wait_for(
                    self.export_chat_invite_link(channel_id), timeout=TELEGRAM_API_TIMEOUT
                )
                setattr(self, f"invitelink{index}", link)
            except Exception as exc:
                self.LOGGER(__name__).critical(
                    "Cannot initialize force-sub channel %s: %s", channel_id, exc
                )
                raise RuntimeError(f"Cannot initialize force-sub channel {channel_id}") from exc

        try:
            self.db_channel = await asyncio.wait_for(
                self.get_chat(CHANNEL_ID), timeout=TELEGRAM_API_TIMEOUT
            )
        except Exception as exc:
            self.LOGGER(__name__).critical("Cannot access database channel %s: %s", CHANNEL_ID, exc)
            raise RuntimeError(f"Cannot access database channel {CHANNEL_ID}") from exc

        self.set_parse_mode(ParseMode.HTML)
        self.username = bot_user.username
        self.LOGGER(__name__).info("Bot started as @%s", self.username)

        self.web_runner = web.AppRunner(await web_server(self))
        await self.web_runner.setup()
        await web.TCPSite(self.web_runner, "0.0.0.0", PORT).start()
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self.control_plane.enabled:
            self.control_plane_task = asyncio.create_task(self._control_plane_loop())

        try:
            await self.send_message(OWNER_ID, text="<b>Bot started.</b>")
        except Exception:
            self.LOGGER(__name__).warning("Could not send startup notification", exc_info=True)

    def _control_plane_registration_payload(self):
        return registration_payload(
            instance_id=CONTROL_PLANE_INSTANCE_ID,
            telegram_bot_id=self.telegram_bot_id,
            username=self.username,
            version=APP_VERSION,
            started_at=self.uptime,
        )

    def _control_plane_heartbeat_payload(self, status: str = "online"):
        return heartbeat_payload(
            instance_id=CONTROL_PLANE_INSTANCE_ID,
            telegram_bot_id=self.telegram_bot_id,
            username=self.username,
            version=APP_VERSION,
            started_at=self.uptime,
            uptime_seconds=int((datetime.now(timezone.utc) - self.uptime).total_seconds()),
            status=status,
        )

    async def _control_plane_loop(self):
        registered = False
        while True:
            try:
                payload = (
                    self._control_plane_heartbeat_payload()
                    if registered
                    else self._control_plane_registration_payload()
                )
                registered = await (
                    self.control_plane.heartbeat(payload)
                    if registered
                    else self.control_plane.register(payload)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.LOGGER(__name__).exception("Control-plane telemetry cycle failed")
            await asyncio.sleep(CONTROL_PLANE_HEARTBEAT_INTERVAL)

    def health_snapshot(self):
        uptime_seconds = int((datetime.now(timezone.utc) - self.uptime).total_seconds()) if hasattr(self, "uptime") else 0
        return {
            "instance_id": CONTROL_PLANE_INSTANCE_ID or None,
            "app_version": APP_VERSION,
            "telegram_bot_id": self.telegram_bot_id,
            "username": getattr(self, "username", None),
            "started_at": utc_isoformat(self.uptime) if hasattr(self, "uptime") else None,
            "uptime_seconds": max(0, uptime_seconds),
            "control_plane": self.control_plane.health_snapshot(),
        }

    async def _cleanup_loop(self):
        while True:
            try:
                for delivery in await due_deliveries():
                    try:
                        deletion_succeeded = True
                        for message_id in delivery.get("message_ids", []):
                            try:
                                await self.delete_messages(delivery["chat_id"], message_id)
                            except FloodWait as exc:
                                await asyncio.sleep(exc.value)
                                try:
                                    await self.delete_messages(delivery["chat_id"], message_id)
                                except Exception:
                                    deletion_succeeded = False
                                    self.LOGGER(__name__).warning(
                                        "Could not delete delivered message after FloodWait",
                                        exc_info=True,
                                    )
                            except Exception:
                                deletion_succeeded = False
                                self.LOGGER(__name__).warning(
                                    "Could not delete delivered message", exc_info=True
                                )
                        if deletion_succeeded:
                            try:
                                await self.edit_message_text(
                                    delivery["chat_id"],
                                    delivery["notification_id"],
                                    "<b>File delivery expired and has been deleted.</b>",
                                )
                            except Exception:
                                pass
                            await mark_delivery_deleted(delivery["_id"])
                        else:
                            if await mark_delivery_attempt(delivery["_id"]):
                                self.LOGGER(__name__).error(
                                    "Delivery cleanup exhausted retries for %s", delivery["_id"]
                                )
                    except Exception:
                        if await mark_delivery_attempt(delivery["_id"]):
                            self.LOGGER(__name__).error(
                                "Persistent delivery cleanup exhausted retries for %s",
                                delivery["_id"],
                            )
                        self.LOGGER(__name__).exception("Persistent delivery cleanup failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.LOGGER(__name__).exception("Delivery cleanup cycle failed")
            await asyncio.sleep(CLEANUP_INTERVAL)

    async def stop(self, *args):
        if self.control_plane_task:
            self.control_plane_task.cancel()
            await asyncio.gather(self.control_plane_task, return_exceptions=True)
            try:
                await self.control_plane.heartbeat(self._control_plane_heartbeat_payload("stopping"))
            except Exception:
                self.LOGGER(__name__).warning("Could not send control-plane stopping status", exc_info=True)
        if self.cleanup_task:
            self.cleanup_task.cancel()
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        if self.web_runner:
            await self.web_runner.cleanup()
        await self.control_plane.close()
        await super().stop()
        self.LOGGER(__name__).info("Bot stopped")