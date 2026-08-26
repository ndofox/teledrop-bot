"""Small, side-effect-free helpers used by the Telegram handlers."""

import asyncio
import re

from pyrogram import filters
from pyrogram import StopPropagation
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.handlers import MessageHandler

from config import (
    ADMINS,
    FORCE_SUB_CHANNEL1,
    FORCE_SUB_CHANNEL2,
    FORCE_SUB_CHANNEL3,
    FORCE_SUB_CHANNEL4,
    LOGGER,
    TELEGRAM_API_TIMEOUT,
)

log = LOGGER(__name__)
_CHANNEL_LINK = re.compile(r"^https://t\.me/(?:c/)?([^/]+)/([1-9][0-9]*)$")


async def ask_message(client, chat_id: int, text: str, response_filters=None, timeout: int | None = None):
    """Send a prompt and wait for one matching message without monkey-patching Pyrogram."""
    loop = asyncio.get_running_loop()
    response = loop.create_future()

    message_filters = filters.chat(chat_id) & filters.user(chat_id)
    if response_filters is not None:
        message_filters &= response_filters

    async def receive_response(_, message):
        if not response.done():
            response.set_result(message)
        raise StopPropagation

    handler = MessageHandler(receive_response, message_filters)
    client.add_handler(handler, group=-1)
    try:
        await client.send_message(chat_id, text)
        return await asyncio.wait_for(response, timeout=timeout)
    finally:
        client.remove_handler(handler, group=-1)


async def _is_subscribed(channel_id: int, client, update) -> bool:
    if not channel_id:
        return True
    user_id = update.from_user.id
    if user_id in ADMINS:
        return True
    try:
        member = await asyncio.wait_for(
            client.get_chat_member(channel_id, user_id), timeout=TELEGRAM_API_TIMEOUT
        )
    except UserNotParticipant:
        return False
    except asyncio.TimeoutError:
        log.warning("Subscription check timed out for channel %s", channel_id)
        return False
    except Exception:
        log.exception("Subscription check failed for channel %s", channel_id)
        return False
    return member.status in {
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }


async def is_subscribed1(_, client, update):
    return await _is_subscribed(FORCE_SUB_CHANNEL1, client, update)


async def is_subscribed2(_, client, update):
    return await _is_subscribed(FORCE_SUB_CHANNEL2, client, update)


async def is_subscribed3(_, client, update):
    return await _is_subscribed(FORCE_SUB_CHANNEL3, client, update)


async def is_subscribed4(_, client, update):
    return await _is_subscribed(FORCE_SUB_CHANNEL4, client, update)


async def get_messages(client, message_ids: list[int]):
    messages = []
    for offset in range(0, len(message_ids), 200):
        chunk = message_ids[offset : offset + 200]
        try:
            result = await client.get_messages(chat_id=client.db_channel.id, message_ids=chunk)
        except FloodWait as exc:
            await asyncio.sleep(exc.value)
            result = await client.get_messages(chat_id=client.db_channel.id, message_ids=chunk)
        except Exception:
            log.exception("Could not read messages from database channel")
            raise
        if not isinstance(result, list):
            result = [result]
        messages.extend(message for message in result if message is not None)
    return messages


def get_message_id(client, message) -> int | None:
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat:
        if forwarded_chat.id != client.db_channel.id:
            return None
        return getattr(message, "forward_from_message_id", None)
    if getattr(message, "forward_sender_name", None):
        return None
    text = (getattr(message, "text", None) or "").strip()
    match = _CHANNEL_LINK.fullmatch(text)
    if not match:
        return None
    channel, raw_id = match.groups()
    if channel.isdigit():
        valid_channel = f"-100{channel}" == str(client.db_channel.id)
    else:
        valid_channel = channel.lower() == (client.db_channel.username or "").lower()
    return int(raw_id) if valid_channel else None


def get_readable_time(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return ":".join(parts)


def get_exp_time(seconds: int) -> str:
    return get_readable_time(seconds).replace(":", " ")


subscribed1 = filters.create(is_subscribed1)
subscribed2 = filters.create(is_subscribed2)
subscribed3 = filters.create(is_subscribed3)
subscribed4 = filters.create(is_subscribed4)