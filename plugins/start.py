"""User delivery and admin operations."""

import asyncio
from datetime import timedelta
from html import escape

from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import Bot
from config import (
    ADMINS, BOT_STATS_TEXT, CUSTOM_CAPTION, FORCE_MSG, FORCE_SUB_CHANNEL1,
    FORCE_SUB_CHANNEL2, FORCE_SUB_CHANNEL3, FORCE_SUB_CHANNEL4, LINK_TTL,
    MAX_BATCH_MESSAGES, PICS, PROTECT_CONTENT, START_MSG, TIME, USER_REPLY_TEXT,
    LOGGER,
)
from database.database import (
    add_user, create_delivery, del_user, find_active_link, full_userbase,
    revoke_link, utc_now,
)
from helper_func import (
    get_exp_time, get_messages, subscribed1, subscribed2, subscribed3, subscribed4,
)
from security import extract_token, token_hash

log = LOGGER(__name__)


def _share_link(client, token: str) -> str:
    return f"https://t.me/{client.username}?start={token}"


def _format_user_message(template: str, user):
    text = template.format(
        first=escape(user.first_name or ""), last=escape(user.last_name or ""), id=user.id,
        mention=user.mention, username=f"@{user.username}" if user.username else "",
    )
    return text.strip() or "TeleDrop bot siap digunakan."


@Bot.on_message(filters.command("start") & filters.private & subscribed1 & subscribed2 & subscribed3 & subscribed4)
async def start_command(client: Client, message: Message):
    await message.reply_chat_action(ChatAction.TYPING)
    await add_user(message.from_user.id)
    payload = message.command[1] if len(message.command) > 1 else ""
    if not payload or payload == "reload":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("About", callback_data="about")]])
        text = _format_user_message(START_MSG, message.from_user)
        if PICS:
            await message.reply_photo(PICS[0], caption=text, reply_markup=markup)
        else:
            await message.reply_text(text, reply_markup=markup)
        return

    token = extract_token(payload)
    link = await find_active_link(token_hash(token)) if token else None
    if not link:
        await message.reply_text("Tautan tidak valid, sudah kedaluwarsa, atau sudah dicabut.")
        return
    ids = link.get("message_ids", [])
    if not ids or len(ids) > MAX_BATCH_MESSAGES or any(not isinstance(item, int) or item < 1 for item in ids):
        log.warning("Rejected malformed link record %s", link.get("_id"))
        await message.reply_text("Tautan file tidak valid.")
        return

    try:
        messages = await get_messages(client, ids)
    except Exception:
        await message.reply_text("File sedang tidak tersedia. Silakan coba lagi nanti.")
        return

    sent_ids = []
    for source in messages:
        caption = source.caption.html if source.caption else ""
        if CUSTOM_CAPTION and source.document:
            caption = CUSTOM_CAPTION.format(
                previouscaption=caption, filename=source.document.file_name or "file"
            )
        try:
            copied = await source.copy(
                chat_id=message.from_user.id, caption=caption, parse_mode=ParseMode.HTML,
                protect_content=PROTECT_CONTENT,
            )
            sent_ids.append(copied.id)
        except FloodWait as exc:
            await asyncio.sleep(exc.value)
            copied = await source.copy(
                chat_id=message.from_user.id, caption=caption, parse_mode=ParseMode.HTML,
                protect_content=PROTECT_CONTENT,
            )
            sent_ids.append(copied.id)
        except Exception:
            log.exception("Could not deliver source message")

    if not sent_ids:
        await message.reply_text("Tidak ada file yang dapat dikirim.")
        return
    if TIME <= 0:
        return

    notification = await message.reply_text(
        f"<i>File akan dihapus dalam {get_exp_time(TIME)}.</i>", disable_web_page_preview=True
    )
    try:
        await create_delivery(
            chat_id=message.from_user.id, message_ids=sent_ids, notification_id=notification.id,
            delete_at=utc_now() + timedelta(seconds=TIME),
        )
    except Exception:
        log.exception("Could not persist auto-delete schedule; deleting delivery immediately")
        for message_id in sent_ids:
            try:
                await client.delete_messages(message.from_user.id, message_id)
            except Exception:
                log.exception("Emergency delivery deletion failed")
        await notification.edit_text("Delivery dibatalkan karena jadwal penghapusan tidak dapat disimpan.")


def _force_sub_buttons(client):
    channel_buttons = []
    for index, channel_id in enumerate(
        [FORCE_SUB_CHANNEL1, FORCE_SUB_CHANNEL2, FORCE_SUB_CHANNEL3, FORCE_SUB_CHANNEL4], 1
    ):
        if channel_id:
            channel_buttons.append(
                InlineKeyboardButton(
                    f"➕ JOIN CHANNEL {index}", url=getattr(client, f"invitelink{index}")
                )
            )

    if len(channel_buttons) == 4:
        return [channel_buttons[:2], channel_buttons[2:]]
    if len(channel_buttons) == 3:
        return [[channel_buttons[0]], channel_buttons[1:]]
    if len(channel_buttons) == 2:
        return [channel_buttons]
    return [[button] for button in channel_buttons]


@Bot.on_message(
    filters.command("start")
    & filters.private
    & ~(subscribed1 & subscribed2 & subscribed3 & subscribed4)
)
async def not_joined(client: Client, message: Message):
    buttons = _force_sub_buttons(client)
    payload = message.command[1] if len(message.command) > 1 else "reload"
    token = extract_token(payload)
    buttons.append([
        InlineKeyboardButton(
            "✅ SUDAH JOIN, COBA LAGI.", url=_share_link(client, token or "reload")
        )
    ])
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    text = _format_user_message(FORCE_MSG, message.from_user)
    if PICS:
        await message.reply_photo(PICS[0], caption=text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


@Bot.on_message(filters.command("revoke") & filters.private & filters.user(ADMINS))
async def revoke_command(_, message: Message):
    if len(message.command) != 2:
        await message.reply_text("Gunakan: /revoke <token atau URL>")
        return
    token = extract_token(message.command[1])
    if not token or not await revoke_link(token_hash(token), message.from_user.id):
        await message.reply_text("Token tidak ditemukan atau sudah dicabut.")
        return
    await message.reply_text("Token berhasil dicabut.")


@Bot.on_message(filters.command("users") & filters.private & filters.user(ADMINS))
async def get_users(_, message: Message):
    status = await message.reply_text("Memproses...")
    await status.edit_text(f"{len(await full_userbase())} user tersimpan")


@Bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    text = (
        "<b>Panduan File Share</b>\n\n"
        "/start — mulai bot atau ambil file dari link\n"
        "/help — tampilkan panduan ini\n\n"
        "<b>Command admin</b>\n"
        "/genlink — buat link untuk satu post\n"
        "/batch — buat link untuk beberapa post\n"
        "/revoke &lt;token atau URL&gt; — cabut link\n"
        "/users — lihat jumlah user\n"
        "/stats — lihat uptime bot\n"
        "/ping — cek bot aktif\n"
        "/info — lihat konfigurasi runtime\n"
        "/broadcast &lt;teks&gt; — kirim pesan baru ke semua user\n"
        "/forward — forward pesan yang di-reply ke semua user\n"
        "/restart — minta restart dari process supervisor"
    )
    if message.from_user.id not in ADMINS:
        text = text.split("\n\n<b>Command admin</b>", 1)[0]
    await message.reply_text(text)


@Bot.on_message(filters.command("ping") & filters.private & filters.user(ADMINS))
async def ping(_, message: Message):
    await message.reply_text("Pong! Bot aktif.")


@Bot.on_message(filters.command("info") & filters.private & filters.user(ADMINS))
async def info(client: Bot, message: Message):
    await message.reply_text(
        "<b>Runtime info</b>\n"
        f"Bot: @{client.username}\n"
        f"Admin ID: <code>{message.from_user.id}</code>\n"
        f"Database channel: <code>{client.db_channel.id}</code>\n"
        f"Link TTL: {LINK_TTL} detik\n"
        f"Auto-delete delivery: {TIME} detik\n"
        f"Protect content: {PROTECT_CONTENT}"
    )


async def _send_to_users(client: Bot, message: Message, forward: bool):
    source = message.reply_to_message
    direct_text = None
    if not source and not forward:
        parts = (message.text or "").split(maxsplit=1)
        direct_text = parts[1].strip() if len(parts) == 2 else None

    if not source and not direct_text:
        if forward:
            await message.reply_text("Reply pesan yang ingin di-forward dengan /forward.")
        else:
            await message.reply_text(
                "Gunakan /broadcast <teks>, atau reply sebuah pesan dengan /broadcast."
            )
        return
    users = await full_userbase()
    status = await message.reply_text(f"Mengirim ke {len(users)} user...")
    successful = blocked = deleted = failed = 0
    for chat_id in users:
        try:
            if direct_text:
                await client.send_message(chat_id, direct_text)
            elif forward:
                await source.forward(chat_id)
            else:
                await source.copy(chat_id)
            successful += 1
            await asyncio.sleep(0.5)
        except FloodWait as exc:
            await asyncio.sleep(exc.value)
            try:
                if direct_text:
                    await client.send_message(chat_id, direct_text)
                elif forward:
                    await source.forward(chat_id)
                else:
                    await source.copy(chat_id)
                successful += 1
            except UserIsBlocked:
                await del_user(chat_id)
                blocked += 1
            except InputUserDeactivated:
                await del_user(chat_id)
                deleted += 1
            except Exception:
                failed += 1
                log.exception("Broadcast retry failed for a user")
        except UserIsBlocked:
            await del_user(chat_id)
            blocked += 1
        except InputUserDeactivated:
            await del_user(chat_id)
            deleted += 1
        except Exception:
            failed += 1
            log.exception("Broadcast operation failed for a user")
    await status.edit_text(
        f"Selesai. total={len(users)} berhasil={successful} blocked={blocked} deleted={deleted} gagal={failed}"
    )


@Bot.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast(client: Bot, message: Message):
    await _send_to_users(client, message, forward=False)


@Bot.on_message(filters.command("forward") & filters.private & filters.user(ADMINS))
async def forward(client: Bot, message: Message):
    await _send_to_users(client, message, forward=True)


@Bot.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats(client: Bot, message: Message):
    seconds = int((utc_now() - client.uptime).total_seconds())
    await message.reply_text(BOT_STATS_TEXT.format(uptime=get_exp_time(seconds)))


@Bot.on_message(filters.command("restart") & filters.private & filters.user(ADMINS))
async def restart_notice(_, message: Message):
    await message.reply_text("Restart dilakukan oleh process supervisor.")


@Bot.on_message(filters.private & filters.incoming)
async def useless(_, message: Message):
    if USER_REPLY_TEXT and not (message.text or "").startswith("/"):
        await message.reply_text(USER_REPLY_TEXT)