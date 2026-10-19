from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from bot.db.repo import search_lectures

router = Router()


@router.message(F.text == "🔍 Search")
async def search_prompt(message: Message):
    await message.answer("Send: /search <keyword>\ne.g. /search anatomy heart")


@router.message(Command("search"))
async def search_cmd(message: Message, session):
    q = message.text.replace("/search", "", 1).strip()
    if not q:
        await message.answer("Usage: /search <keyword>")
        return
    results = await search_lectures(session, q)
    if not results:
        await message.answer("No lectures found.")
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(text=f"🎬 {r.title[:50]}", callback_data=f"lec:{r.id}")] for r in results[:15]]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(f"🔍 Results for '{q}':", reply_markup=kb)
