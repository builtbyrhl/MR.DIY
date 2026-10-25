from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards.inline import request_access_kb
from bot.keyboards.reply import main_menu_kb
from bot.db.repo import is_premium, last_watched_lecture
from bot.services.access import PENDING, REJECTED, access_status, has_library_access
from bot.services.catalog import ensure_subjects


router = Router()


WELCOME = (
    "🎓 Welcome to *FMGE Lecture Bot*\n\n"
    "Your organised medical lecture library.\n\n"
    "*How it works*\n"
    "1. Choose a subject from the menu.\n"
    "2. Pick a topic or teacher.\n"
    "3. Open a lecture, save it, and track your progress.\n\n"
    "Use the menu below whenever you are ready."
)

ACCESS_WELCOME = (
    "🎓 Welcome to *FMGE Lecture Bot*\n\n"
    "This library is private. Request access and an admin will review your "
    "account.\n\n"
)


def _access_welcome_text(user) -> str:
    status = access_status(user)
    if status == PENDING:
        detail = "⏳ Your access request is pending admin approval."
    elif status == REJECTED:
        detail = (
            "❌ Your previous access request was not approved. "
            "You can request access again."
        )
    else:
        detail = "🔒 Library access has not been approved yet."
    return ACCESS_WELCOME + detail


@router.message(CommandStart())
async def cmd_start(message: Message, session, db_user):
    if not has_library_access(db_user):
        status = access_status(db_user)
        pending = status == PENDING and db_user.access_requested_at is not None
        await message.answer(
            _access_welcome_text(db_user),
            parse_mode="Markdown",
            reply_markup=request_access_kb(pending=pending),
        )
        return

    await ensure_subjects(session)
    premium = await is_premium(db_user)
    text = WELCOME + ("\n\n💎 You have Premium access." if premium else "\n\n🆓 Free plan — upgrade for HD, notes & quizzes.")
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())


@router.message(Command("help"))
@router.message(F.text == "❓ Help")
async def help_msg(message: Message):
    await message.answer(
        "❓ *Help*\n\n"
        "*Browse*\n"
        "• 📚 Subjects — open a subject, then a topic or teacher\n"
        "• 🔍 Search — find a lecture by keyword\n"
        "• ⭐ Bookmarks — your saved lectures\n"
        "• 📈 Progress — see what you have watched\n\n"
        "*Commands*\n"
        "/start — home\n"
        "/subjects — browse subjects\n"
        "/search <keyword> — find lectures\n"
        "/bookmarks — saved lectures\n"
        "/progress — your progress\n"
        "/premium — upgrade\n"
        "/admin — admin panel (admins only)",
        parse_mode="Markdown",
    )


# subjects handled in subjects router
