from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards.reply import main_menu_kb
from bot.db.repo import is_premium, last_watched_lecture
from bot.services.catalog import ensure_subjects


router = Router()


WELCOME = (
    "🎓 Welcome to *FMGE Lecture Bot*!\n\n"
    "Your structured path to cracking FMGE.\n"
    "• Subject-wise lectures\n"
    "• Track progress & streaks\n"
    "• Bookmarks & search\n\n"
    "Use the menu below to get started."
)


@router.message(CommandStart())
async def cmd_start(message: Message, session, db_user):
    await ensure_subjects(session)
    premium = await is_premium(db_user)
    text = WELCOME + ("\n\n💎 You have Premium access." if premium else "\n\n🆓 Free plan — upgrade for HD, notes & quizzes.")
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())


@router.message(F.text == "❓ Help")
async def help_msg(message: Message):
    await message.answer(
        "Commands:\n"
        "/start - home\n"
        "/subjects - browse subjects\n"
        "/search <query> - find lectures\n"
        "/bookmarks - saved lectures\n"
        "/progress - your progress\n"
        "/premium - upgrade\n"
        "/admin - admin panel (admins only)"
    )


# subjects handled in subjects router
