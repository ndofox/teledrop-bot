#(©) PythonBotz 

from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from bot import Bot
from config import ADMINS, CHANNEL_ID, DISABLE_CHANNEL_BUTTON, OWNER_ID, LOGGER
from database.database import create_link

log = LOGGER(__name__)

@Bot.on_message(filters.private & filters.user(ADMINS) & ~filters.command([
    'start', 'restart', 'users', 'broadcast', 'forward', 'batch', 'genlink',
    'stats', 'revoke', 'help', 'ping', 'info',
]))
async def channel_post(client: Client, message: Message):
    if (message.text or "").startswith("/"):
        await message.reply_text("Command tidak dikenal. Gunakan /help.")
        return
    reply_text = await message.reply_text("Please Wait...!", quote = True)
    try:
        post_message = await message.copy(chat_id = client.db_channel.id, disable_notification=True)
    except FloodWait as e:
        import asyncio
        await asyncio.sleep(e.value)
        post_message = await message.copy(chat_id=client.db_channel.id, disable_notification=True)
    except Exception as e:
        log.exception("Could not copy message to database channel")
        await reply_text.edit_text("Something went Wrong..!")
        return
    token = await create_link([post_message.id], message.from_user.id)
    link = f"https://t.me/{client.username}?start={token}"

    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Share URL", url=f'https://telegram.me/share/url?url={link}'),
         InlineKeyboardButton("View Post", url=link)]])

    await reply_text.edit(f"<b>Link Anda:</b>\n<code>{link}</code>", reply_markup=reply_markup, disable_web_page_preview=True)

    if not DISABLE_CHANNEL_BUTTON:
        await post_message.edit_reply_markup(reply_markup)

@Bot.on_message(filters.channel & filters.incoming & filters.chat(CHANNEL_ID))
async def new_post(client: Client, message: Message):

    if DISABLE_CHANNEL_BUTTON:
        return

    token = await create_link([message.id], OWNER_ID)
    link = f"https://t.me/{client.username}?start={token}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    try:
        await message.edit_reply_markup(reply_markup)
    except Exception:
        log.exception("Could not add share button to channel post")
