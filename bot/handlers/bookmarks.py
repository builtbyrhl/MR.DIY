from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from bot.db.repo import list_bookmarks
from bot.keyboards.inline import lectures_kb

router = Router()


async def show_bookmarks(message_or_cb, session, db_user):
    bms = await list_bookmarks(session, db_user.id)
    if not bms:
        text = "No bookmarks yet. Star a lecture to save it."
        if isinstance(message_or_cb, Message):
            await message_or_cb.answer(text)
        else:
            await message_or_cb.message.edit_text(text)
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(text=f"⭐ {b.title[:50]}", callback_data=f"lec:{b.id}")] for b in bms]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = f"⭐ Your bookmarks ({len(bms)})"
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=kb)
    else:
        await message_or_cb.message.edit_text(text, reply_markup=kb)


@router.message(F.text == "⭐ Bookmarks")
async def bookmarks_entry(message: Message, session, db_user):
    await show_bookmarks(message, session, db_user)


@router.message(Command("bookmarks"))
async def bookmarks_cmd(message: Message, session, db_user):
    await show_bookmarks(message, session, db_user)
