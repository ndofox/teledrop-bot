"""Admin flows for generating links from posts already in the database channel."""

import asyncio

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from bot import Bot
from config import ADMINS
from config import MAX_BATCH_MESSAGES, LOGGER
from database.database import create_link
from helper_func import ask_message, get_message_id

log = LOGGER(__name__)

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command('batch'))
async def batch(client: Client, message: Message):
    while True:
        try:
            first_message = await ask_message(client, message.from_user.id, "Forward the First Message from DB Channel (with Quotes)..\n\nor Send the DB Channel Post Link", response_filters=(filters.forwarded | (filters.text & ~filters.forwarded)), timeout=60)
        except asyncio.TimeoutError:
            await message.reply_text("Waktu habis. Jalankan /batch lagi jika masih ingin membuat batch link.")
            return
        except Exception:
            log.exception("Could not collect first batch message")
            await message.reply_text("Gagal membaca post pertama. Jalankan /batch lagi.")
            return
        f_msg_id = await get_message_id(client, first_message)
        if f_msg_id:
            break
        else:
            await first_message.reply("❌ Error\n\nthis Forwarded Post is not from my DB Channel or this Link is taken from DB Channel", quote = True)
            continue

    while True:
        try:
            second_message = await ask_message(client, message.from_user.id, "Forward the Last Message from DB Channel (with Quotes)..\nor Send the DB Channel Post link", response_filters=(filters.forwarded | (filters.text & ~filters.forwarded)), timeout=60)
        except asyncio.TimeoutError:
            await message.reply_text("Waktu habis. Jalankan /batch lagi jika masih ingin membuat batch link.")
            return
        except Exception:
            log.exception("Could not collect last batch message")
            await message.reply_text("Gagal membaca post terakhir. Jalankan /batch lagi.")
            return
        s_msg_id = await get_message_id(client, second_message)
        if s_msg_id:
            break
        else:
            await second_message.reply("❌ Error\n\nthis Forwarded Post is not from my DB Channel or this Link is taken from DB Channel", quote = True)
            continue


    step = 1 if f_msg_id <= s_msg_id else -1
    ids = list(range(f_msg_id, s_msg_id + step, step))
    if len(ids) > MAX_BATCH_MESSAGES:
        await second_message.reply_text(f"Batch melebihi batas {MAX_BATCH_MESSAGES} pesan.")
        return
    token = await create_link(ids, message.from_user.id)
    link = f"https://t.me/{client.username}?start={token}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    await second_message.reply_text(f"<b>Here is your link</b>\n\n{link}", quote=True, reply_markup=reply_markup)


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command('genlink'))
async def link_generator(client: Client, message: Message):
    while True:
        try:
            channel_message = await ask_message(client, message.from_user.id, "Forward Message from the DB Channel (with Quotes)..\nor Send the DB Channel Post link", response_filters=(filters.forwarded | (filters.text & ~filters.forwarded)), timeout=60)
        except asyncio.TimeoutError:
            await message.reply_text("Waktu habis. Jalankan /genlink lagi jika masih ingin membuat link.")
            return
        except Exception:
            log.exception("Could not collect message for link generation")
            await message.reply_text("Gagal membaca post. Jalankan /genlink lagi.")
            return
        msg_id = await get_message_id(client, channel_message)
        if msg_id:
            break
        else:
            await channel_message.reply("❌ Error\n\nthis Forwarded Post is not from my DB Channel or this Link is not taken from DB Channel", quote = True)
            continue

    token = await create_link([msg_id], message.from_user.id)
    link = f"https://t.me/{client.username}?start={token}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    await channel_message.reply_text(f"<b>Here is your link</b>\n\n{link}", quote=True, reply_markup=reply_markup)
