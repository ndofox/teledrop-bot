"""Minimal callback UI with no third-party promotional destinations."""

from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot import Bot
from config import MAIN_CHANNEL_URL, SOURCE_CODE_URL


def _navigation():
    rows = []
    if MAIN_CHANNEL_URL:
        rows.append([InlineKeyboardButton("Channel", url=MAIN_CHANNEL_URL)])
    if SOURCE_CODE_URL:
        rows.append([InlineKeyboardButton("Source code", url=SOURCE_CODE_URL)])
    rows.append([InlineKeyboardButton("Close", callback_data="close")])
    return InlineKeyboardMarkup(rows)


@Bot.on_callback_query()
async def callback_handler(_, query: CallbackQuery):
    if query.data == "close":
        await query.message.delete()
    elif query.data in {"about", "home", "main", "source", "me"}:
        text = "<b>TeleDrop Bot</b>\n\nTautan file dikelola secara privat dan memiliki masa berlaku."
        if query.message.photo:
            await query.message.edit_caption(text, reply_markup=_navigation())
        else:
            await query.message.edit_text(text, reply_markup=_navigation())
    await query.answer()