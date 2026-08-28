import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

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
    CONTROL_PLANE_METRICS_DAILY_WINDOW,
    CONTROL_PLANE_METRICS_ENABLED,
    CONTROL_PLANE_METRICS_INTERVAL,
    CONTROL_PLANE_METRICS_OUTBOX_ACCEPTED_RETENTION_DAYS,
    CONTROL_PLANE_METRICS_OUTBOX_CLEANUP_LIMIT,
    CONTROL_PLANE_METRICS_OUTBOX_LEASE_SECONDS,
    CONTROL_PLANE_METRICS_OUTBOX_PERMANENT_RETENTION_DAYS,
    CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS,
    CONTROL_PLANE_METRICS_RETRY_MAX_ATTEMPTS,
    CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS,
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
from control_plane import (
    METRICS_SCHEMA_VERSION,
    PRIVACY_MODE_AGGREGATE_ONLY,
    aggregate_payload,
    canonical_json,
    heartbeat_payload,
    registration_payload,
    utc_isoformat,
)
from control_plane_client import ControlPlaneClient
from database.database import (
    claim_metrics_outbox,
    cleanup_metrics_outbox,
    create_metrics_outbox,
    due_deliveries,
    ensure_indexes,
    get_active_metrics_outbox_delay,
    get_active_metrics_outbox,
    get_metrics_snapshot,
    mark_delivery_attempt,
    mark_delivery_deleted,
    mark_metrics_outbox_accepted,
    mark_metrics_outbox_blocked_auth,
    mark_metrics_outbox_permanent,
    mark_metrics_outbox_retryable,
)
from metrics_policy import (
    OUTBOX_ACTIVE_SLOT,
    metrics_auth_failure,
    metrics_retry_delay_jittered,
    metrics_retry_exhausted,
)
from plugins import web_server


def hashlib_sha256(value: str) -> str:
    """Return the hex SHA-256 of an exact canonical payload string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value) -> datetime:
    """Normalize a stored datetime to an aware UTC value."""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        self.control_plane_metrics_task = None
        self.control_plane_registered = asyncio.Event()
        self.control_plane_registration_requested = asyncio.Event()
        self.control_plane_metrics_wake = asyncio.Event()
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
        if self.control_plane.enabled and CONTROL_PLANE_METRICS_ENABLED:
            self.control_plane_metrics_task = asyncio.create_task(self._metrics_loop())

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
                if self.control_plane_registration_requested.is_set():
                    self.control_plane_registration_requested.clear()
                    registered = False
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
                if registered:
                    became_ready = not self.control_plane_registered.is_set()
                    self.control_plane_registered.set()
                    if became_ready:
                        self.control_plane_metrics_wake.set()
                else:
                    self.control_plane_registered.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                registered = False
                self.control_plane_registered.clear()
                self.LOGGER(__name__).exception("Control-plane control loop failed")
            try:
                await asyncio.wait_for(
                    self.control_plane_registration_requested.wait(),
                    timeout=CONTROL_PLANE_HEARTBEAT_INTERVAL,
                )
                self.control_plane_registration_requested.clear()
                registered = False
            except asyncio.TimeoutError:
                pass

    async def _metrics_loop(self):
        """Dedicated metrics worker. Never blocks or influences the heartbeat loop."""
        while True:
            # Consume a wake that caused this cycle before doing the work. A
            # wake arriving during the tick remains set and is observed by the
            # wait below, preventing both lost wakeups and an immediate second
            # tick from one registration transition.
            self.control_plane_metrics_wake.clear()
            try:
                await self._process_metrics_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.LOGGER(__name__).exception("Control-plane metrics cycle failed")
            due_delay = None
            if self.control_plane_registered.is_set():
                due_delay = await get_active_metrics_outbox_delay(
                    CONTROL_PLANE_INSTANCE_ID, datetime.now(timezone.utc)
                )
            timeout = CONTROL_PLANE_METRICS_INTERVAL
            if due_delay is not None:
                timeout = min(timeout, max(0.05, due_delay))
            try:
                await asyncio.wait_for(
                    self.control_plane_metrics_wake.wait(),
                    timeout=timeout,
                )
                self.control_plane_metrics_wake.clear()
            except asyncio.TimeoutError:
                pass

    async def _process_metrics_tick(self):
        instance_id = CONTROL_PLANE_INSTANCE_ID
        now = datetime.now(timezone.utc)
        if not self.control_plane_registered.is_set():
            return
        active = await get_active_metrics_outbox(instance_id)
        if active is None:
            await self._create_metrics_batch(instance_id, now)
            active = await get_active_metrics_outbox(instance_id)
        if active is None:
            return

        if (
            active.get("status") in ("retryable", "blocked_auth")
            and active.get("next_attempt_at")
            and _as_utc(active["next_attempt_at"]) > now
        ):
            return

        await cleanup_metrics_outbox(
            accepted_retention_days=CONTROL_PLANE_METRICS_OUTBOX_ACCEPTED_RETENTION_DAYS,
            permanent_retention_days=CONTROL_PLANE_METRICS_OUTBOX_PERMANENT_RETENTION_DAYS,
            limit=CONTROL_PLANE_METRICS_OUTBOX_CLEANUP_LIMIT,
        )

        claimed = await claim_metrics_outbox(
            active["_id"],
            CONTROL_PLANE_METRICS_OUTBOX_LEASE_SECONDS,
            now,
        )
        if not claimed:
            return

        batch_id = claimed["_id"]
        attempts = int(claimed.get("attempts", 0))
        owner = claimed["sending_owner"]
        claim_generation = int(claimed.get("claim_generation", 0))
        error_class = None
        try:
            result = await self.control_plane.send_metrics(claimed["canonical_payload"])
            if result.ok:
                await mark_metrics_outbox_accepted(
                    batch_id,
                    owner=owner,
                    claim_generation=claim_generation,
                    now=datetime.now(timezone.utc),
                )
                return
            if result.agent_not_registered:
                self.control_plane_registered.clear()
                self.control_plane_registration_requested.set()
                await self._record_metrics_outbox_failure(
                    batch_id,
                    attempts,
                    owner,
                    claim_generation,
                    "retryable_http_404",
                )
                return
            if result.retryable:
                error_class = f"retryable_http_{result.status_code}"
            else:
                error_class = f"permanent_http_{result.status_code}"
        except Exception as exc:
            error_class = exc.__class__.__name__

        await self._record_metrics_outbox_failure(batch_id, attempts, owner, claim_generation, error_class)

    async def _record_metrics_outbox_failure(
        self, batch_id: str, attempts: int, owner: str, claim_generation: int, error_class: str
    ):
        now = datetime.now(timezone.utc)
        attempts += 1
        if metrics_auth_failure(error_class):
            # Quarantine the batch as blocked_auth. The active outbox slot stays
            # occupied so no new snapshot is produced on every interval; the
            # exact payload is retried with a bounded backoff until credentials
            # are fixed (then it is accepted) without a tight retry loop.
            delay = metrics_retry_delay_jittered(
                attempts,
                CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS,
                CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS,
            )
            await mark_metrics_outbox_blocked_auth(
                batch_id,
                owner=owner,
                claim_generation=claim_generation,
                attempts=attempts,
                next_attempt_at=now + timedelta(seconds=delay),
                error_class=error_class,
                now=now,
            )
            return
        if metrics_retry_exhausted(attempts, CONTROL_PLANE_METRICS_RETRY_MAX_ATTEMPTS):
            await mark_metrics_outbox_permanent(
                batch_id,
                owner=owner,
                claim_generation=claim_generation,
                attempts=attempts,
                error_class=error_class,
                now=now,
            )
            return
        delay = metrics_retry_delay_jittered(
            attempts,
            CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS,
            CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS,
        )
        await mark_metrics_outbox_retryable(
            batch_id,
            owner=owner,
            claim_generation=claim_generation,
            next_attempt_at=now + timedelta(seconds=delay),
            attempts=attempts,
            error_class=error_class,
            now=now,
        )

    async def _create_metrics_batch(self, instance_id: str, now):
        snapshot = await get_metrics_snapshot(reference=now, daily_days=CONTROL_PLANE_METRICS_DAILY_WINDOW)
        payload = aggregate_payload(
            instance_id=instance_id,
            observed_at=snapshot["observed_at"],
            current=snapshot["current"],
            daily=snapshot["daily"],
        )
        canonical = canonical_json(payload)
        record = {
            "_id": payload["batch_id"],
            "batch_id": payload["batch_id"],
            "instance_id": instance_id,
            "metrics_schema_version": METRICS_SCHEMA_VERSION,
            "privacy_mode": PRIVACY_MODE_AGGREGATE_ONLY,
            "payload_hash": hashlib_sha256(canonical),
            "canonical_payload": canonical,
            "status": "pending",
            "active_slot": OUTBOX_ACTIVE_SLOT,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "next_attempt_at": now,
            "observed_at": snapshot["observed_at"],
        }
        await create_metrics_outbox(record)

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
        if self.control_plane_metrics_task:
            self.control_plane_metrics_task.cancel()
            await asyncio.gather(self.control_plane_metrics_task, return_exceptions=True)
        if self.cleanup_task:
            self.cleanup_task.cancel()
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        if self.web_runner:
            await self.web_runner.cleanup()
        await self.control_plane.close()
        await super().stop()
        self.LOGGER(__name__).info("Bot stopped")